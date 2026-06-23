"""CERAPP evaluation-set label assembly: assay-level -> one ER label per compound."""

from __future__ import annotations

import cerapp  # noqa: E402 — resolved via tests/staging/conftest.py sys.path


def _row(casrn: str, assay_class: str, active) -> dict:
    return {
        "CASRN": casrn,
        "ASSAY_CLASS_NAME": assay_class,
        "All_active": active,
        "InChI_Code": f"InChI={casrn}",
    }


def _fixture() -> list[dict]:
    # Assay-level rows (many per CASRN), mixed assay classes + a NaN active value.
    return [
        _row("B1", "ER Binding", 1),  # binding active ...
        _row("B1", "ER Agonist", 0),  # ... + non-binding inactive (mode-dependent!)
        _row("B2", "Receptor Binding", 0),  # only binding, all inactive -> 0
        _row("B2", "Receptor Binding", 0),
        _row("C", "Binding", 1),  # binding rows disagree -> CONFLICT in both modes
        _row("C", "Binding", 0),
        _row("D", "Agonist", 1),  # non-binding active: dropped (binding), label 1 (any)
        _row("E", "Binding", None),  # NaN active -> dropped (no kept rows -> not emitted)
    ]


def test_binding_mode_filters_to_binding_class_and_collapses() -> None:
    labels, conflicts = cerapp.assemble_evaluation_labels(_fixture(), label_mode="binding")
    by_casrn = {r["casrn"]: r["label"] for r in labels}

    # B1: only the binding row (active) is kept -> 1; B2: binding inactive -> 0.
    assert by_casrn == {"B1": 1, "B2": 0}
    # C disagrees within binding rows -> excluded (counted, not voted). D/E not emitted.
    assert [c["casrn"] for c in conflicts] == ["C"]
    assert conflicts[0]["labels"] == [0, 1]


def test_any_mode_keeps_all_assay_classes_with_more_conflicts() -> None:
    labels, conflicts = cerapp.assemble_evaluation_labels(_fixture(), label_mode="any")
    by_casrn = {r["casrn"]: r["label"] for r in labels}

    # B2 all-inactive -> 0; D active -> 1. B1 now disagrees (binding 1 + agonist 0) and C
    # disagrees -> both excluded. E is all-NaN -> dropped.
    assert by_casrn == {"B2": 0, "D": 1}
    assert sorted(c["casrn"] for c in conflicts) == ["B1", "C"]


def test_disagreement_is_excluded_not_majority_voted() -> None:
    rows = [
        {"CASRN": "X", "ASSAY_CLASS_NAME": "Binding", "All_active": 1, "InChI_Code": "i"},
        {"CASRN": "X", "ASSAY_CLASS_NAME": "Binding", "All_active": 1, "InChI_Code": "i"},
        {"CASRN": "X", "ASSAY_CLASS_NAME": "Binding", "All_active": 0, "InChI_Code": "i"},
    ]
    labels, conflicts = cerapp.assemble_evaluation_labels(rows, label_mode="binding")
    # 2 active vs 1 inactive would be a majority -> but we EXCLUDE, never vote.
    assert labels == []
    assert [c["casrn"] for c in conflicts] == ["X"]


def test_output_schema_matches_staging_join() -> None:
    labels, _ = cerapp.assemble_evaluation_labels(_fixture(), label_mode="binding")
    row = next(r for r in labels if r["casrn"] == "B1")
    # Consumed by the unchanged label_retriever (casrn/target/consensus_call) + explicit fields.
    assert set(row) == {
        "casrn",
        "inchikey",
        "inchi_code",
        "target",
        "consensus_call",
        "consensus_score",
        "label",
        "label_provenance",
    }
    assert row["target"] == "ER"
    assert row["consensus_call"] in {"active", "inactive"}
    assert row["consensus_call"] == "active" and row["label"] == 1
    assert row["label_provenance"] == "cerapp_experimental_binding"
    assert row["inchikey"] is None  # resolved later via the PubChem CASRN->InChIKey path


def test_invalid_mode_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="label_mode"):
        cerapp.assemble_evaluation_labels(_fixture(), label_mode="majority")
