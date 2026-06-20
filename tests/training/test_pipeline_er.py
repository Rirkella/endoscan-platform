"""End-to-end ER pipeline on fixtures + gate/approval blocking (offline)."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from endoscan_core.datasets import SourcesAllowList
from endoscan_core.registry import EndpointNotFoundError, EndpointStatus, get_endpoint

REPO_ROOT = Path(__file__).resolve().parents[2]
TRAINING_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "training"
TRAINING_DATASETS = TRAINING_FIXTURES / "datasets"
RELAXED_GATES = TRAINING_FIXTURES / "quality_gates_relaxed.yaml"
STRICT_GATES = REPO_ROOT / "registry" / "data" / "quality_gates.yaml"


def _config(er_run: ModuleType, *, approved: bool) -> object:
    raw = yaml.safe_load((TRAINING_FIXTURES / "er_pipeline.test.yaml").read_text())
    raw["approval"]["approved"] = approved
    return er_run.PipelineConfig.model_validate(raw)


def test_end_to_end_registers_experimental_in_tmp_registry(
    er_run: ModuleType, allow_list: SourcesAllowList, tmp_output_root: Path
) -> None:
    result = er_run.run_pipeline(
        _config(er_run, approved=True),
        allow_list=allow_list,
        fixtures_dir=TRAINING_DATASETS,
        thresholds=er_run.load_thresholds(RELAXED_GATES),
        output_root=tmp_output_root,
    )

    assert result.gate_passed is True
    assert result.approved is True
    assert result.trained is True
    assert result.registered is True
    # Weak-signal fixtures => below the validated_mvp AUROC floor.
    assert result.status == "experimental"
    assert result.selected_model in ("elastic_net_logreg", "gradient_boosting")

    entry = get_endpoint("ER", repo_root=tmp_output_root)
    assert entry.status is EndpointStatus.experimental
    assert entry.input_type == "transcriptomics"
    for rel in (
        entry.model_path,
        entry.feature_schema_path,
        entry.metrics_path,
        entry.model_card_path,
        entry.dataset_card_path,
    ):
        assert (tmp_output_root / rel).is_file()

    metrics = json.loads((tmp_output_root / entry.metrics_path).read_text())
    assert metrics["cv"]["strategy"] == "GroupKFold"
    assert metrics["selected_model"] == result.selected_model
    assert metrics["auroc"] < 0.75

    card = (tmp_output_root / entry.model_card_path).read_text()
    assert "{{" not in card  # fully rendered
    assert "not regulatory-grade" in card.lower()


def test_end_to_end_promotes_validated_mvp_with_permissive_floor(
    er_run: ModuleType, allow_list: SourcesAllowList, tmp_output_root: Path
) -> None:
    # Permissive floor (auroc >= 0.0) so the weak-signal metrics clear it; this
    # exercises the validated_mvp status branch + the registry's artifact gate.
    raw = yaml.safe_load((TRAINING_FIXTURES / "er_pipeline.test.yaml").read_text())
    raw["approval"]["approved"] = True
    raw["training"]["validated_mvp_floors"] = {"auroc": 0.0}
    config = er_run.PipelineConfig.model_validate(raw)

    result = er_run.run_pipeline(
        config,
        allow_list=allow_list,
        fixtures_dir=TRAINING_DATASETS,
        thresholds=er_run.load_thresholds(RELAXED_GATES),
        output_root=tmp_output_root,
    )

    assert result.trained is True
    assert result.registered is True
    assert result.status == "validated_mvp"

    entry = get_endpoint("ER", repo_root=tmp_output_root)
    assert entry.status is EndpointStatus.validated_mvp
    # validated_mvp registered => the registry artifact-existence gate passed.
    for rel in (
        entry.model_path,
        entry.feature_schema_path,
        entry.metrics_path,
        entry.model_card_path,
        entry.dataset_card_path,
    ):
        assert (tmp_output_root / rel).is_file()

    # The real registry is untouched.
    real = json.loads((REPO_ROOT / "registry" / "models" / "endpoints.json").read_text())
    assert real == {"endpoints": []}


def test_strict_gate_blocks_training(
    er_run: ModuleType, allow_list: SourcesAllowList, tmp_output_root: Path
) -> None:
    result = er_run.run_pipeline(
        _config(er_run, approved=True),
        allow_list=allow_list,
        fixtures_dir=TRAINING_DATASETS,
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
    result = er_run.run_pipeline(
        _config(er_run, approved=False),
        allow_list=allow_list,
        fixtures_dir=TRAINING_DATASETS,
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
