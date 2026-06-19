"""quality_gates: pass + fail verdicts; defaults load from YAML."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from endoscan_core.datasets import (
    GateThresholds,
    dataset_quality_report,
    load_gates,
    quality_gates,
)


def _report(tmp_path: Path, er_artifacts: SimpleNamespace):
    overlap = er_artifacts.overlap
    return dataset_quality_report(
        "ER",
        er_artifacts.table,
        er_artifacts.labels,
        er_artifacts.signatures,
        overlap.n_labeled,
        overlap.n_with_signature,
        overlap.n_overlap,
        tmp_path,
        write_card=False,
    )


def test_gate_pass(tmp_path: Path, er_artifacts: SimpleNamespace) -> None:
    report = _report(tmp_path, er_artifacts)
    thresholds = GateThresholds(
        min_compounds_per_class=2,
        min_overlap=4,
        max_duplicate_signature_rate=0.2,
        max_label_conflict_rate=0.2,
        min_metadata_coverage=0.9,
        require_compound_level_split_feasible=True,
    )
    verdict = quality_gates(report, thresholds)
    assert verdict.passed is True
    assert verdict.summary == "PASS"


def test_gate_fail(tmp_path: Path, er_artifacts: SimpleNamespace) -> None:
    report = _report(tmp_path, er_artifacts)
    thresholds = GateThresholds(
        min_compounds_per_class=10,
        min_overlap=40,
        max_duplicate_signature_rate=0.0,
        max_label_conflict_rate=0.0,
        min_metadata_coverage=0.99,
        require_compound_level_split_feasible=True,
    )
    verdict = quality_gates(report, thresholds)
    assert verdict.passed is False
    assert "min_compounds_per_class" in verdict.summary


def test_load_gates_defaults() -> None:
    thresholds = load_gates()
    assert thresholds.min_compounds_per_class == 20
    assert thresholds.min_overlap == 40
    assert thresholds.require_compound_level_split_feasible is True
