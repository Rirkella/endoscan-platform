"""Config-driven ER endpoint pipeline runner (NOT a notebook).

End-to-end flow (path A — fixtures only at M3):
  M2 tools (build candidate table) -> dataset_quality_report -> quality_gates
  verdict -> train ONLY if the verdict PASSES and an explicit human-approval flag
  is set -> evaluate -> write artifacts -> register into the M1 registry.

The committed ``config.yaml`` uses the fixture adapter, the strict real gate
(``registry/data/quality_gates.yaml``) and ``approved: false``, so running it on
fixtures is intentionally BLOCKED. The train path is exercised only by the
relaxed-gate TEST config under ``tests/fixtures/training/``.

Run:  uv run python pipelines/endpoints/ER/run.py [--config <path>]
"""

from __future__ import annotations

import argparse
import json
import pickle
import shutil
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from endoscan_core.datasets import (
    FixtureSourceAdapter,
    GateThresholds,
    SourcesAllowList,
    candidate_table_builder,
    compound_mapper,
    dataset_quality_report,
    label_retriever,
    load_sources,
    overlap_computer,
    quality_gates,
    signature_retriever,
    source_selector,
)
from endoscan_core.features import build_feature_schema
from endoscan_core.registry import EndpointEntry, EndpointStatus, register_endpoint
from endoscan_core.registry.store import find_repo_root
from endoscan_core.registry.templates import render_template
from endoscan_core.training import train_endpoint
from endoscan_core.training.train_endpoint import TrainResult


# --------------------------------------------------------------------------- config
class DataConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str
    adapter: str = "fixture"  # path A: fixture only (real adapter is a later issue)
    fixtures_dir: str
    n_groups: int = 2
    seed: int = 0


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thresholds_path: str


class ApprovalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool = False
    approved_by: str = ""
    note: str = ""


class CVConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n_splits: int = 5


class TrainingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[str] = Field(
        default_factory=lambda: list(("elastic_net_logreg", "gradient_boosting"))
    )
    selection_metric: str = "auroc"
    # Structured metric floors for validated_mvp; AUROC-only at M3 (extensible).
    validated_mvp_floors: dict[str, float] = Field(default_factory=lambda: {"auroc": 0.75})


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: str
    biological_target: str
    version: str = "0.1.0"
    data: DataConfig
    gate: GateConfig
    approval: ApprovalConfig
    cv: CVConfig = Field(default_factory=CVConfig)
    training: TrainingConfig = Field(default_factory=TrainingConfig)


class PipelineResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: str
    gate_passed: bool
    approved: bool
    trained: bool
    registered: bool
    status: str | None = None
    selected_model: str | None = None
    n_overlap: int = 0
    gate_summary: str = ""
    metrics_path: str | None = None
    model_card_path: str | None = None
    dataset_card_path: str | None = None


# --------------------------------------------------------------------------- helpers
def load_config(path: Path) -> PipelineConfig:
    return PipelineConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def load_thresholds(path: Path) -> GateThresholds:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return GateThresholds.model_validate(raw["thresholds"])


def _status_for(metrics, floors: dict[str, float]) -> EndpointStatus:
    """validated_mvp iff every configured metric floor is met, else experimental."""
    meets_all = all(getattr(metrics, name) >= floor for name, floor in floors.items())
    return EndpointStatus.validated_mvp if meets_all else EndpointStatus.experimental


def _write_metrics(result: TrainResult, n_compounds: int) -> dict:
    metrics = result.metrics
    return {
        "auroc": metrics.auroc,
        "auprc": metrics.auprc,
        "balanced_accuracy": metrics.balanced_accuracy,
        "f1": metrics.f1,
        "brier_score": metrics.brier_score,
        "confusion_matrix": metrics.confusion_matrix.model_dump(),
        "n_samples": metrics.n_samples,
        "n_compounds": n_compounds,
        "cv": {"strategy": "GroupKFold", "n_splits": result.n_splits},
        "selected_model": result.selected_model_name,
        "threshold": metrics.threshold,
    }


def _render_model_card(
    config: PipelineConfig,
    status: EndpointStatus,
    result: TrainResult,
    report,
    sources: list[str],
    n_features: int,
) -> str:
    metrics = result.metrics
    return render_template(
        "model_card.md",
        {
            "endpoint_id": config.endpoint_id,
            "biological_target": config.biological_target,
            "input_type": "transcriptomics",
            "version": config.version,
            "status": status.value,
            "sources": ", ".join(sources),
            "compound_counts": (
                f"overlap={report.n_overlap}, "
                f"compounds_per_class={report.per_class_compound_counts}"
            ),
            "feature_summary": f"{n_features} landmark genes",
            "model_type": result.selected_model_name,
            "training_summary": (
                f"GroupKFold (compound-level), n_splits={result.n_splits}; selected by OOF AUROC"
            ),
            "metrics_summary": (
                f"AUROC={metrics.auroc:.3f}, AUPRC={metrics.auprc:.3f}, "
                f"balanced_acc={metrics.balanced_accuracy:.3f}, F1={metrics.f1:.3f}, "
                f"Brier={metrics.brier_score:.3f}"
            ),
            "recommended_use": (
                "Research prioritization of estrogen-receptor activity from signatures."
            ),
            "not_recommended_use": "Any regulatory, clinical, or diagnostic decision.",
            "limitations": (
                "Pre-screening / prioritization only. Built from a curated allow-list; "
                "metrics are cross-validated on a limited compound-level dataset and are NOT "
                "regulatory-grade validation."
            ),
        },
    )


# --------------------------------------------------------------------------- runner
def run_pipeline(
    config: PipelineConfig,
    *,
    allow_list: SourcesAllowList,
    fixtures_dir: Path,
    thresholds: GateThresholds,
    output_root: Path,
) -> PipelineResult:
    """Run the ER pipeline end-to-end. Trains only on gate-PASS and approval."""
    target = config.data.target
    adapter = FixtureSourceAdapter(fixtures_dir)

    label_sources = source_selector(target, allow_list, types=["labels"])
    sig_sources = source_selector(target, allow_list, types=["signatures"])
    map_source = source_selector(target, allow_list, types=["mapping"])[0]

    labels = label_retriever(target, label_sources, adapter, allow_list=allow_list)
    signatures = signature_retriever(sig_sources, adapter, allow_list=allow_list)
    ids = [(r.compound_id, r.compound_id_type) for r in labels.records]
    ids += [(s.compound_id, s.compound_id_type) for s in signatures.records]
    mapping = compound_mapper(ids, map_source, adapter, allow_list=allow_list)
    overlap = overlap_computer(labels, signatures, mapping)
    table = candidate_table_builder(
        labels, signatures, overlap, mapping, n_groups=config.data.n_groups, seed=config.data.seed
    )

    cards_dir = output_root / "registry" / "data" / "dataset_cards"
    report = dataset_quality_report(
        target,
        table,
        labels,
        signatures,
        overlap.n_labeled,
        overlap.n_with_signature,
        overlap.n_overlap,
        cards_dir,
        write_card=True,
    )
    verdict = quality_gates(report, thresholds)

    base = PipelineResult(
        endpoint_id=config.endpoint_id,
        gate_passed=verdict.passed,
        approved=config.approval.approved,
        trained=False,
        registered=False,
        n_overlap=overlap.n_overlap,
        gate_summary=verdict.summary,
        dataset_card_path=report.dataset_card_path,
    )

    # HARD BOUNDARY: train only when the gate passes AND a human approved.
    if not (verdict.passed and config.approval.approved):
        return base

    result = train_endpoint(
        table.X,
        table.y,
        table.metadata["split_group"].tolist(),
        model_names=config.training.models,
        n_splits_requested=config.cv.n_splits,
        seed=config.data.seed,
    )
    status = _status_for(result.metrics, config.training.validated_mvp_floors)

    model_dir = output_root / "models" / config.endpoint_id
    model_dir.mkdir(parents=True, exist_ok=True)
    with (model_dir / "model.pkl").open("wb") as handle:
        pickle.dump(result.fitted_model, handle, protocol=5)
    schema = build_feature_schema(table.X)
    (model_dir / "feature_schema.json").write_text(
        schema.model_dump_json(indent=2), encoding="utf-8"
    )
    n_compounds = int(table.metadata["compound_key"].nunique())
    (model_dir / "metrics.json").write_text(
        json.dumps(_write_metrics(result, n_compounds), indent=2), encoding="utf-8"
    )
    shutil.copyfile(report.dataset_card_path, model_dir / "dataset_card.md")
    sources = [s.id for s in [*label_sources, *sig_sources, map_source]]
    (model_dir / "model_card.md").write_text(
        _render_model_card(config, status, result, report, sources, len(table.feature_names)),
        encoding="utf-8",
    )

    rel = f"models/{config.endpoint_id}"
    entry = EndpointEntry(
        endpoint_id=config.endpoint_id,
        biological_target=config.biological_target,
        input_type="transcriptomics",
        model_path=f"{rel}/model.pkl",
        feature_schema_path=f"{rel}/feature_schema.json",
        metrics_path=f"{rel}/metrics.json",
        explainer_path=None,
        model_card_path=f"{rel}/model_card.md",
        dataset_card_path=f"{rel}/dataset_card.md",
        status=status,
        version=config.version,
        created_at=datetime.now(UTC),
        source_refs=sources,
    )
    register_endpoint(entry, repo_root=output_root)

    return base.model_copy(
        update={
            "trained": True,
            "registered": True,
            "status": status.value,
            "selected_model": result.selected_model_name,
            "metrics_path": entry.metrics_path,
            "model_card_path": entry.model_card_path,
        }
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the ER endpoint pipeline.")
    parser.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"))
    args = parser.parse_args(argv)

    repo_root = find_repo_root()
    config = load_config(Path(args.config))
    allow_list = load_sources(repo_root=repo_root)
    fixtures_dir = repo_root / config.data.fixtures_dir
    thresholds = load_thresholds(repo_root / config.gate.thresholds_path)

    result = run_pipeline(
        config,
        allow_list=allow_list,
        fixtures_dir=fixtures_dir,
        thresholds=thresholds,
        output_root=repo_root,
    )
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
