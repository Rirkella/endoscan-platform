"""Config-driven ER endpoint pipeline runner (NOT a notebook).

End-to-end flow:
  dataset layer tools (build candidate table) -> dataset_quality_report -> quality_gates
  verdict -> train ONLY if the verdict PASSES and an explicit human-approval flag
  is set -> HONEST evaluation (nested grouped CV or compound-level held-out) ->
  scorecard model selection -> write artifacts -> register in the endpoint registry.

Data is read through the adapter seam: ``fixture`` (CSV fixtures, CI) or
``staged`` (local Parquet/CSV extracts staged by a human for the one-time real
run). There are no live downloads; ``RealDownloadAdapter`` stays a stub.

metrics.json reports the HONEST nested-outer (or held-out) estimate for the chosen
model — never resubstitution/inner-CV — and the validated_mvp floors are checked
against that estimate. No regulatory-grade claims are made anywhere.

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
    SourceAdapter,
    SourcesAllowList,
    StagedSourceAdapter,
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
from endoscan_core.registry import EndpointEntry, EndpointStatus, register_or_update_endpoint
from endoscan_core.registry.store import find_repo_root
from endoscan_core.registry.templates import render_template
from endoscan_core.training import (
    EvalMetrics,
    build_model,
    fit_balanced,
    holdout_group_eval,
    nested_group_cv,
    render_selection_markdown,
    score_candidates,
    select_model,
    selection_report,
    unmet_validated_mvp_reasons,
)
from endoscan_core.training.train_endpoint import MODEL_NAMES


# --------------------------------------------------------------------------- config
class DataConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str
    adapter: str = "fixture"  # "fixture" (CI) | "staged" (real, local extracts)
    fixtures_dir: str | None = None
    staged_dir: str | None = None
    n_groups: int = 5
    seed: int = 0
    # Label sources that MUST be staged for this endpoint (others are optional: a
    # genuinely-absent optional label file contributes zero rows instead of crashing).
    # ER trains on CERAPP experimental calls; toxcast/tox21 stay approved in the
    # allow-list for future endpoints but are optional here.
    required_label_sources: list[str] = Field(default_factory=list)


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thresholds_path: str


class ApprovalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool = False
    approved_by: str = ""
    note: str = ""


class EvaluationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "nested"  # "nested" | "holdout"
    outer_splits: int = 5
    inner_splits: int = 3
    test_size: float = 0.25  # held-out mode only


class TrainingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[str] = Field(default_factory=lambda: list(MODEL_NAMES))
    selection_tolerance: float = 0.02
    # Multi-metric floors (>=) and optional ceilings (<=), checked against the
    # HONEST estimate. Values are configurable defaults; finalize vs real
    # prevalence in reviewed real-data workflow (AUPRC baseline = positive prevalence).
    validated_mvp_floors: dict[str, float] = Field(
        default_factory=lambda: {"auroc": 0.75, "auprc": 0.50, "balanced_accuracy": 0.65}
    )
    validated_mvp_ceilings: dict[str, float] = Field(default_factory=lambda: {"brier_score": 0.20})


class PipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: str
    biological_target: str
    version: str = "0.1.0"
    # Conservative, endpoint-specific scope of the claim (model-card SCOPE OF CLAIM).
    # Parameterized so it is not hardcoded to ER; a generic conservative default is used
    # when unset.
    claim_scope: str | None = None
    # Model-card recommended / not-recommended use lines. Optional and endpoint-specific:
    # when unset they fall back to the ER defaults below, so ER's rendered card is
    # byte-identical, while a non-ER endpoint (e.g. AR) supplies its own wording instead of
    # inheriting "estrogen-receptor" text.
    recommended_use: str | None = None
    not_recommended_use: str | None = None
    data: DataConfig
    gate: GateConfig
    approval: ApprovalConfig
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
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
    evaluation_mode: str | None = None
    n_overlap: int = 0
    gate_summary: str = ""
    metrics_path: str | None = None
    model_card_path: str | None = None
    dataset_card_path: str | None = None
    model_selection_path: str | None = None


# --------------------------------------------------------------------------- helpers
def load_config(path: Path) -> PipelineConfig:
    return PipelineConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def load_thresholds(path: Path) -> GateThresholds:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return GateThresholds.model_validate(raw["thresholds"])


def make_adapter(config: PipelineConfig, data_dir: Path) -> SourceAdapter:
    if config.data.adapter == "staged":
        return StagedSourceAdapter(data_dir)
    if config.data.adapter == "fixture":
        return FixtureSourceAdapter(data_dir)
    raise ValueError(f"Unknown data.adapter {config.data.adapter!r}")


def _metrics_point(metrics: EvalMetrics) -> dict[str, float]:
    """The metric VALUES the floors/ceilings are checked against (EvalMetrics -> dict)."""
    return {
        "auroc": metrics.auroc,
        "auprc": metrics.auprc,
        "balanced_accuracy": metrics.balanced_accuracy,
        "f1": metrics.f1,
        "brier_score": metrics.brier_score,
    }


def _status_for(
    metrics: EvalMetrics,
    floors: dict[str, float],
    ceilings: dict[str, float],
    *,
    uncertainty: dict | None = None,
    evidence: dict | None = None,
) -> EndpointStatus:
    """validated_mvp iff the strengthened evidence rule is met, else experimental.

    Delegates to the SINGLE shared decision (``unmet_validated_mvp_reasons``): the
    min-evidence gate + CI-lower-bound floor check (same floor VALUES). Absent
    uncertainty/evidence -> reasons non-empty -> experimental (graceful degrade)."""
    reasons = unmet_validated_mvp_reasons(
        _metrics_point(metrics), floors, ceilings, uncertainty=uncertainty, evidence=evidence
    )
    return EndpointStatus.validated_mvp if not reasons else EndpointStatus.experimental


def _metrics_dict(
    metrics: EvalMetrics,
    n_compounds: int,
    selected_model: str,
    evaluation: dict,
    *,
    validated_mvp_floors: dict[str, float],
    validated_mvp_ceilings: dict[str, float],
    claim_scope: str | None,
    uncertainty: dict | None = None,
    per_fold_metrics: list[dict] | None = None,
    evidence: dict | None = None,
) -> dict:
    out = {
        "auroc": metrics.auroc,
        "auprc": metrics.auprc,
        "balanced_accuracy": metrics.balanced_accuracy,
        "f1": metrics.f1,
        "brier_score": metrics.brier_score,
        "confusion_matrix": metrics.confusion_matrix.model_dump(),
        "n_samples": metrics.n_samples,
        "n_compounds": n_compounds,
        "selected_model": selected_model,
        "threshold": metrics.threshold,
        "evaluation": evaluation,
        # Structured fields the inference/limitations layer reads (no card parsing):
        # the floors/ceilings the run was judged against and the endpoint's claim scope.
        "validated_mvp_floors": dict(validated_mvp_floors),
        "validated_mvp_ceilings": dict(validated_mvp_ceilings),
        "claim_scope": claim_scope,
        "estimate": "honest (nested-outer or held-out); not resubstitution/inner-CV",
    }
    # ADDED ALONGSIDE the pooled scalar metrics (existing keys above are unchanged): the
    # bootstrap-CI / per-fold / sample-evidence blocks the strengthened status rule uses.
    # The inference layer reads these to evidence validated_mvp under uncertainty.
    if uncertainty is not None:
        out["uncertainty"] = uncertainty
    if per_fold_metrics is not None:
        out["per_fold_metrics"] = per_fold_metrics
    if evidence is not None:
        out["evidence"] = evidence
    return out


_GENERIC_CLAIM_SCOPE = (
    "the configured endpoint only; NOT a pan-endocrine, regulatory, clinical, or "
    "diagnostic claim"
)

# Model-card use-line defaults. These are the ORIGINAL ER strings; an endpoint whose
# config leaves the fields unset renders exactly these (ER's card stays byte-identical).
_DEFAULT_RECOMMENDED_USE = "Research prioritization of estrogen-receptor activity from signatures."
_DEFAULT_NOT_RECOMMENDED_USE = "Any regulatory, clinical, or diagnostic decision."


def missed_floors(
    metrics: EvalMetrics,
    floors: dict[str, float],
    ceilings: dict[str, float],
    *,
    uncertainty: dict | None = None,
    evidence: dict | None = None,
) -> list[str]:
    """The reasons validated_mvp is NOT earned, phrased honestly.

    Delegates to the SAME shared decision the inference layer
    (``inference.limitations.missed_criteria``) uses, so trainer status and served
    limitations can never diverge (the strengthened PR #24 agreement invariant): the
    min-evidence gate, the require-a-CI rule, and the CI-lower-bound floor / CI-upper-bound
    ceiling checks (same floor VALUES).
    """
    return unmet_validated_mvp_reasons(
        _metrics_point(metrics), floors, ceilings, uncertainty=uncertainty, evidence=evidence
    )


def build_limitations_section(
    *,
    status: EndpointStatus,
    metrics: EvalMetrics,
    report,
    evaluation: dict,
    floors: dict[str, float],
    ceilings: dict[str, float],
    selected_model: str,
    claim_scope: str | None,
    uncertainty: dict | None = None,
    evidence: dict | None = None,
) -> str:
    """Build the parameterized, honest model-card Limitations block from real values."""
    per_class = report.per_class_compound_counts
    n_pos = int(per_class.get(1, 0))
    n_neg = int(per_class.get(0, 0))
    n_total = n_pos + n_neg
    prevalence = (n_pos / n_total) if n_total else 0.0

    # STATUS line.
    if status == EndpointStatus.experimental:
        missed = missed_floors(
            metrics, floors, ceilings, uncertainty=uncertainty, evidence=evidence
        )
        status_line = (
            "**Status — experimental:** a real-data methodology demonstration, NOT a "
            "validated predictor."
        )
        if missed:
            status_line += " Unmet validated_mvp criteria: " + "; ".join(missed) + "."
    else:
        status_line = (
            f"**Status — {status.value}:** meets the configured " "validated_mvp floors/ceilings."
        )

    # STATISTICAL POWER.
    outer_splits = evaluation.get("outer_splits")
    power = (
        f"**Statistical power:** {n_pos} positives / {n_total} compounds "
        f"(prevalence {prevalence:.3f})."
    )
    if outer_splits:
        power += (
            f" Only ~{n_pos / outer_splits:.0f} positives per held-out fold "
            f"(n_pos / {outer_splits})."
        )

    # MODEL SELECTION STABILITY.
    per_fold = list(evaluation.get("per_fold_selected") or [])
    if len(set(per_fold)) > 1:
        stability = (
            f"**Model-selection stability:** family selection was UNSTABLE across folds "
            f"(per-fold picks: {per_fold}); the registered model ({selected_model}) is the "
            "automated simplicity-aware scorecard pick on all data."
        )
    elif per_fold:
        stability = (
            f"**Model-selection stability:** selection was consistent across folds "
            f"({selected_model}); chosen by the automated simplicity-aware scorecard."
        )
    else:
        stability = (
            f"**Model-selection stability:** {selected_model}, the automated "
            "simplicity-aware scorecard pick."
        )

    # COVERAGE.
    coverage = (
        "**Coverage:** built from the intersection of approved labels and "
        "transcriptomic signatures (compound-level); it is coverage-limited and NOT "
        "representative of the full chemical space."
    )

    # SCOPE OF CLAIM.
    scope = f"**Scope of claim:** {claim_scope or _GENERIC_CLAIM_SCOPE}."

    disclaimer = (
        "Pre-screening / prioritization only; metrics are the honest compound-level "
        "cross-validated estimate, NOT regulatory-grade validation, and must be "
        "confirmed experimentally."
    )
    return "\n\n".join([status_line, power, stability, coverage, scope, disclaimer])


def _render_model_card(
    config: PipelineConfig,
    status: EndpointStatus,
    selected_model: str,
    metrics: EvalMetrics,
    report,
    n_features: int,
    evaluation: dict,
    sources: list[str],
    *,
    uncertainty: dict | None = None,
    evidence: dict | None = None,
) -> str:
    evaluation_mode = evaluation["mode"]
    per_fold = list(evaluation.get("per_fold_selected") or [])
    unstable = len(set(per_fold)) > 1
    training_summary = (
        f"compound-level {evaluation_mode} evaluation; model chosen by a "
        f"simplicity-aware scorecard over {len(config.training.models)} sklearn models"
    )
    if unstable:
        training_summary += ", family selection unstable across folds (see Limitations)"
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
            "model_type": selected_model,
            "training_summary": training_summary,
            "metrics_summary": (
                f"AUROC={metrics.auroc:.3f}, AUPRC={metrics.auprc:.3f}, "
                f"balanced_acc={metrics.balanced_accuracy:.3f}, F1={metrics.f1:.3f}, "
                f"Brier={metrics.brier_score:.3f} (honest {evaluation_mode} estimate)"
            ),
            "recommended_use": config.recommended_use or _DEFAULT_RECOMMENDED_USE,
            "not_recommended_use": config.not_recommended_use or _DEFAULT_NOT_RECOMMENDED_USE,
            "limitations": build_limitations_section(
                status=status,
                metrics=metrics,
                report=report,
                evaluation=evaluation,
                floors=config.training.validated_mvp_floors,
                ceilings=config.training.validated_mvp_ceilings,
                selected_model=selected_model,
                claim_scope=config.claim_scope,
                uncertainty=uncertainty,
                evidence=evidence,
            ),
        },
    )


# --------------------------------------------------------------------------- runner
def run_pipeline(
    config: PipelineConfig,
    *,
    allow_list: SourcesAllowList,
    data_dir: Path,
    thresholds: GateThresholds,
    output_root: Path,
) -> PipelineResult:
    """Run the ER pipeline end-to-end. Trains only on gate-PASS and approval."""
    target = config.data.target
    adapter = make_adapter(config, data_dir)

    label_sources = source_selector(target, allow_list, types=["labels"])
    sig_sources = source_selector(target, allow_list, types=["signatures"])
    map_source = source_selector(target, allow_list, types=["mapping"])[0]

    # A genuinely-ABSENT label extract contributes zero rows (not a crash); a
    # present-but-malformed one still raises when label_retriever parses it. Required
    # label sources (per endpoint config) must be present — a missing one is an
    # operator/staging error with a clear message, not a silent gate fail.
    present_ids = {s.id for s in label_sources if adapter.has_source(s)}
    status = ", ".join(
        f"{s.id}={'present' if s.id in present_ids else 'absent'}" for s in label_sources
    )
    print(f"label sources: {status}")
    required = set(config.data.required_label_sources)
    missing_required = sorted(
        s.id for s in label_sources if s.id in required and s.id not in present_ids
    )
    if missing_required:
        raise ValueError(
            f"required label source(s) not staged for {target}: {missing_required}. "
            "Stage the extract(s) or adjust data.required_label_sources."
        )
    present_label_sources = [s for s in label_sources if s.id in present_ids]

    labels = label_retriever(target, present_label_sources, adapter, allow_list=allow_list)
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

    X, y = table.X, table.y
    groups = table.metadata["split_group"].tolist()
    seed = config.data.seed
    tol = config.training.selection_tolerance
    models = config.training.models

    # 1) HONEST estimate of the selection procedure (removes selection optimism).
    if config.evaluation.mode == "holdout":
        ho = holdout_group_eval(
            X,
            y,
            groups,
            models,
            test_size=config.evaluation.test_size,
            inner_splits=config.evaluation.inner_splits,
            seed=seed,
            tolerance=tol,
        )
        honest = ho.metrics
        uncertainty, per_fold_metrics, evidence = ho.uncertainty, ho.per_fold_metrics, ho.evidence
        evaluation = {
            "mode": "holdout",
            "test_size": config.evaluation.test_size,
            "n_test": ho.n_test,
            "holdout_selected_model": ho.selected_model,
        }
    else:
        nested = nested_group_cv(
            X,
            y,
            groups,
            models,
            outer_splits=config.evaluation.outer_splits,
            inner_splits=config.evaluation.inner_splits,
            seed=seed,
            tolerance=tol,
        )
        honest = nested.outer_metrics
        uncertainty = nested.uncertainty
        per_fold_metrics = nested.per_fold_metrics
        evidence = nested.evidence
        evaluation = {
            "mode": "nested",
            "outer_splits": nested.outer_splits,
            "inner_splits": nested.inner_splits,
            "per_fold_selected": nested.selected_models,
        }

    # 2) Final model: scorecard over ALL data + simplicity-aware selection, refit.
    full_scores = score_candidates(X, y, groups, models, config.evaluation.inner_splits, seed)
    final_model_name = select_model(full_scores, tol)
    final_model = build_model(final_model_name, seed=seed)
    fit_balanced(final_model, X, y)  # final refit on all rows (balanced)

    # Status under the STRENGTHENED rule: min-evidence gate + CI-lower-bound >= floor
    # (same floor VALUES). Uncertainty/evidence come from the honest nested/holdout result.
    status = _status_for(
        honest,
        config.training.validated_mvp_floors,
        config.training.validated_mvp_ceilings,
        uncertainty=uncertainty,
        evidence=evidence,
    )

    # 3) Write artifacts. Binary model.pkl is DVC-tracked; the rest is git text.
    model_dir = output_root / "models" / config.endpoint_id
    model_dir.mkdir(parents=True, exist_ok=True)
    with (model_dir / "model.pkl").open("wb") as handle:
        pickle.dump(final_model, handle, protocol=5)

    schema = build_feature_schema(X)
    (model_dir / "feature_schema.json").write_text(
        schema.model_dump_json(indent=2), encoding="utf-8"
    )
    n_compounds = int(table.metadata["compound_key"].nunique())
    (model_dir / "metrics.json").write_text(
        json.dumps(
            _metrics_dict(
                honest,
                n_compounds,
                final_model_name,
                evaluation,
                validated_mvp_floors=config.training.validated_mvp_floors,
                validated_mvp_ceilings=config.training.validated_mvp_ceilings,
                claim_scope=config.claim_scope,
                uncertainty=uncertainty,
                per_fold_metrics=per_fold_metrics,
                evidence=evidence,
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    shutil.copyfile(report.dataset_card_path, model_dir / "dataset_card.md")

    report_dict = selection_report(
        full_scores,
        final_model_name,
        tol,
        extra={"nested_per_fold_selected": evaluation.get("per_fold_selected")},
    )
    (model_dir / "model_selection.json").write_text(
        json.dumps(report_dict, indent=2), encoding="utf-8"
    )
    (model_dir / "model_selection.md").write_text(
        render_selection_markdown(report_dict), encoding="utf-8"
    )

    # Registry honesty: provenance lists only the label sources that ACTUALLY
    # contributed at least one label record (e.g. CERAPP), not absent/empty optional
    # ones. Signature + mapping sources always contribute on a successful run.
    contributing_labels = sorted({record.assay_source for record in labels.records})
    sources = [*contributing_labels, *[s.id for s in sig_sources], map_source.id]
    card = _render_model_card(
        config,
        status,
        final_model_name,
        honest,
        report,
        len(table.feature_names),
        evaluation,
        sources,
        uncertainty=uncertainty,
        evidence=evidence,
    )
    (model_dir / "model_card.md").write_text(card, encoding="utf-8")

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
    # Re-registration path: overwrites an already-registered (non-frozen) endpoint in place
    # so a re-promote needs no manual endpoints.json editing. Only reached after gate PASS +
    # approval (the chokepoint is unchanged); a FROZEN endpoint (ER) is refused.
    register_or_update_endpoint(entry, repo_root=output_root)

    return base.model_copy(
        update={
            "trained": True,
            "registered": True,
            "status": status.value,
            "selected_model": final_model_name,
            "evaluation_mode": evaluation["mode"],
            "metrics_path": entry.metrics_path,
            "model_card_path": entry.model_card_path,
            "model_selection_path": f"{rel}/model_selection.json",
        }
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the ER endpoint pipeline.")
    parser.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"))
    args = parser.parse_args(argv)

    repo_root = find_repo_root()
    config = load_config(Path(args.config))
    allow_list = load_sources(repo_root=repo_root)

    if config.data.adapter == "staged":
        if not config.data.staged_dir:
            raise ValueError("data.staged_dir is required when data.adapter == 'staged'")
        data_dir = repo_root / config.data.staged_dir
    else:
        if not config.data.fixtures_dir:
            raise ValueError("data.fixtures_dir is required when data.adapter == 'fixture'")
        data_dir = repo_root / config.data.fixtures_dir

    thresholds = load_thresholds(repo_root / config.gate.thresholds_path)
    result = run_pipeline(
        config,
        allow_list=allow_list,
        data_dir=data_dir,
        thresholds=thresholds,
        output_root=repo_root,
    )
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
