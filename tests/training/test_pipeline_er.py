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


def test_permissive_floors_promote_to_validated_mvp(
    er_run: ModuleType, allow_list: SourcesAllowList, tmp_output_root: Path
) -> None:
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
    assert result.status == "validated_mvp"
    entry = get_endpoint("ER", repo_root=tmp_output_root)
    assert entry.status is EndpointStatus.validated_mvp
    # validated_mvp registered => the registry artifact-existence gate passed.
    for rel in (entry.model_path, entry.feature_schema_path, entry.metrics_path):
        assert (tmp_output_root / rel).is_file()


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


def test_real_registry_index_stays_empty() -> None:
    index = json.loads((REPO_ROOT / "registry" / "models" / "endpoints.json").read_text())
    assert index == {"endpoints": []}
