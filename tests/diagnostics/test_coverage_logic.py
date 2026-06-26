"""Pure coverage math: gate verdict, overlap, report assembly, floor sync."""

from __future__ import annotations

from endoscan_core.datasets import load_gates
from endoscan_core.diagnostics import (
    GateFloors,
    build_coverage_report,
    context_overlap,
    gate_verdict,
)
from endoscan_core.diagnostics.coverage import (
    DEFAULT_MIN_COMPOUNDS_PER_CLASS,
    DEFAULT_MIN_OVERLAP,
)


def test_default_floors_mirror_quality_gates_yaml() -> None:
    # The diagnostic's default floors must equal the real gate (single source of truth).
    gates = load_gates()
    assert DEFAULT_MIN_OVERLAP == gates.min_overlap
    assert DEFAULT_MIN_COMPOUNDS_PER_CLASS == gates.min_compounds_per_class


def test_gate_verdict_boundaries() -> None:
    floors = GateFloors(min_overlap=40, min_compounds_per_class=20)
    # Exactly on both floors -> PASS.
    assert gate_verdict(40, 20, 20, floors)[0] == "PASS"
    # One below overlap.
    v, why = gate_verdict(39, 20, 20, floors)
    assert v == "failed_qc" and "overlap 39<40" in why
    # One below the minority class.
    v, why = gate_verdict(100, 19, 81, floors)
    assert v == "failed_qc" and "min-class 19<20" in why


def test_context_overlap_counts() -> None:
    labels = {"A": 1, "B": 0, "C": 1, "D": 0}
    ov, pos, neg = context_overlap(labels, {"A", "C", "D", "ZZ"})
    assert (ov, pos, neg) == (3, 2, 1)


def _labels(n_pos: int, n_neg: int) -> dict[str, int]:
    d = {f"P{i}": 1 for i in range(n_pos)}
    d.update({f"N{i}": 0 for i in range(n_neg)})
    return d


def test_report_pass_prefers_androgen_context() -> None:
    labels = _labels(25, 25)
    keys = set(labels)
    report = build_coverage_report(
        target="AR",
        labels=labels,
        n_conflicts=2,
        context_inchikeys={"VCaP (androgen)": keys, "broad set": keys},
        contexts={"VCaP (androgen)": ["VCAP"], "broad set": ["VCAP", "MCF7"]},
        line_signature_counts={"VCAP": 100},
        floors=GateFloors(min_overlap=40, min_compounds_per_class=20),
        source_table="t.csv",
        source_call_column="AR_Binding",
    )
    assert report.any_context_passes
    assert report.recommended_context == "VCaP (androgen)"  # androgen-relevant PASS preferred
    assert report.n_structures == 50 and report.n_conflicts == 2


def test_report_failed_qc_when_underpowered() -> None:
    labels = _labels(5, 7)
    keys = set(labels)
    report = build_coverage_report(
        target="AR",
        labels=labels,
        n_conflicts=0,
        context_inchikeys={"VCaP (androgen)": keys},
        contexts={"VCaP (androgen)": ["VCAP"]},
        line_signature_counts={"VCAP": 10},
        floors=GateFloors(),  # 40/20
    )
    assert not report.any_context_passes
    assert report.contexts[0].verdict == "failed_qc"
    assert "failed_qc" in report.recommendation_note
