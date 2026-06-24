"""Label-source honesty: a genuinely-absent OPTIONAL label file contributes zero rows
(not a crash), a present-but-malformed one still errors, required sources must be
staged, the found-vs-absent status is logged, the tolerance never weakens the gate,
and provenance (source_refs + dataset card) lists only the sources that contributed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import ModuleType

import pandas as pd
import pytest

from endoscan_core.datasets import SourcesAllowList
from endoscan_core.registry import get_endpoint

REPO_ROOT = Path(__file__).resolve().parents[2]
TRAINING_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "training"
RELAXED_GATES = TRAINING_FIXTURES / "quality_gates_relaxed.yaml"
ER_FUSED = TRAINING_FIXTURES / "staged" / "er_fused"  # compound_id + cerapp/pubchem (+empty stubs)


def _cerapp_only(dst: Path, *, with_cerapp: bool = True) -> Path:
    """A staged dir with ONLY the real CERAPP-run files (no toxcast/tox21 stubs)."""
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy(ER_FUSED / "lincs.parquet", dst / "lincs.parquet")
    shutil.copy(ER_FUSED / "pubchem.csv", dst / "pubchem.csv")
    if with_cerapp:
        shutil.copy(ER_FUSED / "cerapp.csv", dst / "cerapp.csv")
    return dst


def _cfg(er_run: ModuleType, staged_dir: Path, *, approved: bool = False, required=("cerapp",)):
    return er_run.PipelineConfig.model_validate(
        {
            "endpoint_id": "ER",
            "biological_target": "Estrogen Receptor",
            "version": "0.1.0",
            "data": {
                "target": "ER",
                "adapter": "staged",
                "staged_dir": str(staged_dir),
                "n_groups": 3,
                "seed": 0,
                "required_label_sources": list(required),
            },
            "gate": {"thresholds_path": str(RELAXED_GATES)},
            "approval": {"approved": approved},
            "evaluation": {"mode": "nested", "outer_splits": 5, "inner_splits": 3},
        }
    )


def _run(er_run, config, allow_list, output_root):
    return er_run.run_pipeline(
        config,
        allow_list=allow_list,
        data_dir=Path(config.data.staged_dir),
        thresholds=er_run.load_thresholds(RELAXED_GATES),
        output_root=output_root,
    )


def test_absent_optional_label_file_is_tolerated_and_logged(
    er_run: ModuleType, allow_list: SourcesAllowList, tmp_path: Path, capsys
) -> None:
    staged = _cerapp_only(tmp_path / "staged")  # cerapp present; toxcast/tox21 absent
    res = _run(er_run, _cfg(er_run, staged), allow_list, tmp_path / "out")
    # No FileNotFoundError: CERAPP labels resolved, gate produced a verdict.
    assert res.n_overlap == 10
    out = capsys.readouterr().out
    assert "cerapp=present" in out
    assert "toxcast=absent" in out and "tox21=absent" in out


def test_present_but_malformed_label_file_still_errors(
    er_run: ModuleType, allow_list: SourcesAllowList, tmp_path: Path
) -> None:
    staged = _cerapp_only(tmp_path / "staged")
    # A present CERAPP file with a non-numeric consensus_score must NOT be swallowed.
    pd.DataFrame(
        [
            {
                "casrn": "300-00-0",
                "target": "ER",
                "consensus_call": "active",
                "consensus_score": "not_a_number",
            }
        ]
    ).to_csv(staged / "cerapp.csv", index=False)
    with pytest.raises(ValueError):
        _run(er_run, _cfg(er_run, staged), allow_list, tmp_path / "out")


def test_missing_required_label_source_raises_clear_error(
    er_run: ModuleType, allow_list: SourcesAllowList, tmp_path: Path
) -> None:
    staged = _cerapp_only(tmp_path / "staged", with_cerapp=False)  # CERAPP absent
    with pytest.raises(ValueError, match="required label source"):
        _run(er_run, _cfg(er_run, staged, required=("cerapp",)), allow_list, tmp_path / "out")


def test_zero_labels_fails_gate_not_crash(
    er_run: ModuleType, allow_list: SourcesAllowList, tmp_path: Path
) -> None:
    # No label files at all, nothing required -> zero labels -> the GATE fails honestly.
    staged = _cerapp_only(tmp_path / "staged", with_cerapp=False)
    res = _run(er_run, _cfg(er_run, staged, required=()), allow_list, tmp_path / "out")
    assert res.gate_passed is False
    assert res.trained is False
    assert res.n_overlap == 0


def test_present_but_empty_required_source_does_not_mask_empty_contribution(
    er_run: ModuleType, allow_list: SourcesAllowList, tmp_path: Path
) -> None:
    # The required-source check is presence-only and INDEPENDENT of the gate's
    # label-count judgment: a PRESENT-but-EMPTY required CERAPP file satisfies the
    # required check (no missing-required error) yet still FAILS the gate as
    # zero-overlap -- required-present must NOT mask an empty contribution.
    staged = _cerapp_only(tmp_path / "staged", with_cerapp=False)
    # Header-only CERAPP file: present (has_source True) but yields zero label rows.
    pd.DataFrame(columns=["casrn", "target", "consensus_call", "consensus_score"]).to_csv(
        staged / "cerapp.csv", index=False
    )
    res = _run(er_run, _cfg(er_run, staged, required=("cerapp",)), allow_list, tmp_path / "out")
    # Required check satisfied (the file is present) -> run_pipeline did NOT raise.
    # Gate fails on the empty contribution, not "passes" because the source was present.
    assert res.n_overlap == 0
    assert res.gate_passed is False
    assert "min_overlap" in res.gate_summary and "min_compounds_per_class" in res.gate_summary
    assert res.trained is False


def _balanced_cerapp_only(dst: Path) -> Path:
    """A balanced (8/8) fused CERAPP-only staged dir for a stable nested-CV train run."""
    dst.mkdir(parents=True, exist_ok=True)
    genes = ["GENE_A", "GENE_B", "GENE_C", "GENE_D", "GENE_E"]
    patterns = [
        [1.5, 1.4, 1.3, 1.2, 1.1],
        [-1.5, -1.4, -1.3, -1.2, -1.1],
        [1.0, -1.0, 1.0, -1.0, 1.0],
        [0.1, 0.0, -0.1, 0.05, -0.05],
    ]
    sig, lab, mp = [], [], []
    for i in range(16):
        key = f"{chr(ord('A') + i) * 14}-{chr(ord('A') + i) * 10}-N"
        casrn = f"300-00-{i:02d}"
        sig.append({"compound_id": key, **dict(zip(genes, patterns[i % 4], strict=True))})
        lab.append(
            {
                "casrn": casrn,
                "target": "ER",
                "consensus_call": "active" if i < 8 else "inactive",
                "consensus_score": 0.9,
            }
        )
        mp.append(
            {
                "input_id": casrn,
                "input_id_type": "CASRN",
                "inchikey": key,
                "mapping_confidence": "exact",
            }
        )
    pd.DataFrame(sig).to_parquet(dst / "lincs.parquet", index=False)
    pd.DataFrame(lab).to_csv(dst / "cerapp.csv", index=False)
    pd.DataFrame(mp).to_csv(dst / "pubchem.csv", index=False)
    return dst


def test_source_refs_and_card_list_only_contributing_sources(
    er_run: ModuleType, allow_list: SourcesAllowList, tmp_path: Path
) -> None:
    staged = _balanced_cerapp_only(tmp_path / "staged")
    output_root = tmp_path / "out"
    (output_root / "registry" / "models").mkdir(parents=True, exist_ok=True)
    (output_root / "registry" / "models" / "endpoints.json").write_text(
        '{\n  "endpoints": []\n}\n', encoding="utf-8"
    )
    config = er_run.PipelineConfig.model_validate(
        {
            "endpoint_id": "ER",
            "biological_target": "Estrogen Receptor",
            "version": "0.1.0",
            "data": {
                "target": "ER",
                "adapter": "staged",
                "staged_dir": str(staged),
                "n_groups": 5,
                "seed": 0,
                "required_label_sources": ["cerapp"],
            },
            "gate": {"thresholds_path": str(RELAXED_GATES)},
            "approval": {"approved": True, "approved_by": "test"},
            "evaluation": {"mode": "nested", "outer_splits": 5, "inner_splits": 3},
            "training": {
                "validated_mvp_floors": {"auroc": 0.0, "auprc": 0.0, "balanced_accuracy": 0.0},
                "validated_mvp_ceilings": {},
            },
        }
    )
    res = _run(er_run, config, allow_list, output_root)
    assert res.trained is True
    entry = get_endpoint("ER", repo_root=output_root)
    # Only the sources that ACTUALLY contributed: CERAPP labels + LINCS + PubChem.
    assert entry.source_refs == ["cerapp", "lincs", "pubchem"]
    assert "toxcast" not in entry.source_refs and "tox21" not in entry.source_refs
    card = (output_root / "models" / "ER" / "dataset_card.md").read_text()
    assert "cerapp" in card and "toxcast" not in card and "tox21" not in card
    # Sanity: metrics file written (the honest nested estimate path ran).
    assert json.loads((output_root / res.metrics_path).read_text())["selected_model"]
