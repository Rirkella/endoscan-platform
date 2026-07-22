"""End-to-end ER pipeline on the staged nested fixture + gate/approval blocking."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from endoscan_core.datasets import SourcesAllowList
from endoscan_core.registry import EndpointNotFoundError, EndpointStatus, get_endpoint
from endoscan_core.training import MODEL_NAMES

REPO_ROOT = Path(__file__).resolve().parents[2]
TRAINING_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "training"
STAGED_DIR = TRAINING_FIXTURES / "staged" / "er"
RELAXED_GATES = TRAINING_FIXTURES / "quality_gates_relaxed.yaml"
STRICT_GATES = REPO_ROOT / "registry" / "data" / "quality_gates.yaml"


def _config(
    er_run: ModuleType,
    *,
    approved: bool = True,
    floors: dict | None = None,
    ceilings: dict | None = None,
) -> object:
    raw = yaml.safe_load((TRAINING_FIXTURES / "er_pipeline.test.yaml").read_text())
    raw["approval"]["approved"] = approved
    if floors is not None:
        raw["training"]["validated_mvp_floors"] = floors
    if ceilings is not None:
        raw["training"]["validated_mvp_ceilings"] = ceilings
    return er_run.PipelineConfig.model_validate(raw)


def _run(er_run, config, *, allow_list, thresholds, output_root):
    return er_run.run_pipeline(
        config,
        allow_list=allow_list,
        data_dir=STAGED_DIR,
        thresholds=thresholds,
        output_root=output_root,
    )


def test_end_to_end_registers_experimental(
    er_run: ModuleType, allow_list: SourcesAllowList, tmp_output_root: Path
) -> None:
    result = _run(
        er_run,
        _config(er_run, approved=True),
        allow_list=allow_list,
        thresholds=er_run.load_thresholds(RELAXED_GATES),
        output_root=tmp_output_root,
    )
    assert result.gate_passed is True
    assert result.trained is True
    assert result.registered is True
    assert result.status == "experimental"  # weak-signal staged fixture
    assert result.evaluation_mode == "nested"
    assert result.selected_model in MODEL_NAMES

    entry = get_endpoint("ER", repo_root=tmp_output_root)
    assert entry.status is EndpointStatus.experimental
    for rel in (
        entry.model_path,
        entry.feature_schema_path,
        entry.metrics_path,
        entry.model_card_path,
        entry.dataset_card_path,
    ):
        assert (tmp_output_root / rel).is_file()
    # The model-selection report is written alongside the artifacts.
    assert (tmp_output_root / "models" / "ER" / "model_selection.json").is_file()
    assert (tmp_output_root / "models" / "ER" / "model_selection.md").is_file()


def test_metrics_are_the_honest_nested_estimate(
    er_run: ModuleType, allow_list: SourcesAllowList, tmp_output_root: Path
) -> None:
    result = _run(
        er_run,
        _config(er_run, approved=True),
        allow_list=allow_list,
        thresholds=er_run.load_thresholds(RELAXED_GATES),
        output_root=tmp_output_root,
    )
    metrics = json.loads((tmp_output_root / result.metrics_path).read_text())
    assert metrics["evaluation"]["mode"] == "nested"
    assert metrics["evaluation"]["outer_splits"] == 5
    assert metrics["selected_model"] == result.selected_model
    assert "honest" in metrics["estimate"]
    assert metrics["auroc"] < 0.75  # weak signal


def test_underpowered_run_blocked_from_validated_mvp_by_min_evidence(
    er_run: ModuleType, allow_list: SourcesAllowList, tmp_output_root: Path
) -> None:
    # Even with floors=0.0 (CI trivially clears them), the tiny staged fixture (~8 positives)
    # cannot earn validated_mvp: the sample-aware min-evidence gate demotes it to
    # experimental. This is the strengthened rule working — no lucky/underpowered badge.
    config = _config(
        er_run,
        approved=True,
        floors={"auroc": 0.0, "auprc": 0.0, "balanced_accuracy": 0.0},
        ceilings={},
    )
    result = _run(
        er_run,
        config,
        allow_list=allow_list,
        thresholds=er_run.load_thresholds(RELAXED_GATES),
        output_root=tmp_output_root,
    )
    assert result.status == "experimental"
    entry = get_endpoint("ER", repo_root=tmp_output_root)
    assert entry.status is EndpointStatus.experimental
    # It still REGISTERED (experimental registers too) — artifacts + the evidence blocks exist.
    for rel in (entry.model_path, entry.feature_schema_path, entry.metrics_path):
        assert (tmp_output_root / rel).is_file()
    metrics = json.loads((tmp_output_root / entry.metrics_path).read_text())
    assert metrics["evidence"]["n_positives_total"] < 30  # underpowered -> gate binds
    assert "uncertainty" in metrics and "per_fold_metrics" in metrics


def test_strict_gate_blocks_training(
    er_run: ModuleType, allow_list: SourcesAllowList, tmp_output_root: Path
) -> None:
    result = _run(
        er_run,
        _config(er_run, approved=True),
        allow_list=allow_list,
        thresholds=er_run.load_thresholds(STRICT_GATES),
        output_root=tmp_output_root,
    )
    assert result.gate_passed is False
    assert result.trained is False
    assert result.registered is False
    assert not (tmp_output_root / "models" / "ER" / "model.pkl").exists()
    with pytest.raises(EndpointNotFoundError):
        get_endpoint("ER", repo_root=tmp_output_root)


def test_missing_approval_blocks_training(
    er_run: ModuleType, allow_list: SourcesAllowList, tmp_output_root: Path
) -> None:
    result = _run(
        er_run,
        _config(er_run, approved=False),
        allow_list=allow_list,
        thresholds=er_run.load_thresholds(RELAXED_GATES),
        output_root=tmp_output_root,
    )
    assert result.gate_passed is True
    assert result.approved is False
    assert result.trained is False
    assert result.registered is False
    with pytest.raises(EndpointNotFoundError):
        get_endpoint("ER", repo_root=tmp_output_root)


def test_real_registry_registers_er_experimental() -> None:
    # ER is the first real endpoint registered by the reviewed real-data workflow, at
    # `experimental` status (honest demonstration, not a validated predictor). The registry
    # holds the AR endpoint (added separately); this test asserts ER's entry, not its size.
    index = json.loads((REPO_ROOT / "registry" / "models" / "endpoints.json").read_text())
    ids = [e["endpoint_id"] for e in index["endpoints"]]
    assert "ER" in ids
    er = next(e for e in index["endpoints"] if e["endpoint_id"] == "ER")
    assert er["status"] == "experimental"
    assert er["input_type"] == "transcriptomics"
    assert er["source_refs"] == ["cerapp", "lincs", "pubchem"]
