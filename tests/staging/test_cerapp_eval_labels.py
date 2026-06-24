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


# --- CERAPP ENDPOINT axis (Mode column): agonist / antagonist single-mode selection ---


def _mode_row(casrn: str, mode: str, active) -> dict:
    # assay_class is deliberately a NON-binding technology to prove the Mode axis does
    # NOT consult ASSAY_CLASS_NAME (reporter-gene rows still count for agonist/antagonist).
    return {
        "CASRN": casrn,
        "ASSAY_CLASS_NAME": "Reporter Gene",
        "Mode": mode,
        "All_active": active,
        "InChI_Code": f"InChI={casrn}",
    }


def test_agonist_mode_selects_only_mode_agonist_rows() -> None:
    rows = [
        _mode_row("AG1", "Agonist", 1),  # agonist active -> 1
        _mode_row("AG2", "agonist", 0),  # case-insensitive -> 0
        _mode_row("AN1", "Antagonist", 1),  # antagonist row: ignored in agonist mode
        _mode_row("MIX", "Agonist", 1),  # MIX: agonist active ...
        _mode_row("MIX", "Antagonist", 0),  # ... antagonist inactive ignored -> MIX stays 1
    ]
    labels, conflicts = cerapp.assemble_evaluation_labels(rows, label_mode="agonist")
    assert {r["casrn"]: r["label"] for r in labels} == {"AG1": 1, "AG2": 0, "MIX": 1}
    assert conflicts == []
    assert all(r["label_provenance"] == "cerapp_experimental_agonist" for r in labels)


def test_antagonist_mode_selects_only_mode_antagonist_rows() -> None:
    rows = [
        _mode_row("AN1", "Antagonist", 1),
        _mode_row("AN2", "Antagonist", 0),
        _mode_row("AG1", "Agonist", 1),  # ignored in antagonist mode
    ]
    labels, _ = cerapp.assemble_evaluation_labels(rows, label_mode="antagonist")
    assert {r["casrn"]: r["label"] for r in labels} == {"AN1": 1, "AN2": 0}
    assert all(r["label_provenance"] == "cerapp_experimental_antagonist" for r in labels)


def test_mode_match_is_exact_not_substring() -> None:
    # "agonist" is a substring of "antagonist": an exact match must NOT pick these up.
    rows = [_mode_row("X", "Antagonist", 1), _mode_row("Y", "Antagonist", 0)]
    labels, conflicts = cerapp.assemble_evaluation_labels(rows, label_mode="agonist")
    assert labels == [] and conflicts == []


def test_mode_axis_per_casrn_conflict_excluded() -> None:
    rows = [
        _mode_row("Z", "Agonist", 1),  # agonist rows for Z disagree ...
        _mode_row("Z", "Agonist", 0),  # ... -> CONFLICT, excluded (never voted)
        _mode_row("Z", "Antagonist", 1),  # different endpoint: ignored in agonist mode
    ]
    labels, conflicts = cerapp.assemble_evaluation_labels(rows, label_mode="agonist")
    assert labels == []
    assert [c["casrn"] for c in conflicts] == ["Z"]
    assert conflicts[0]["labels"] == [0, 1]


def test_custom_mode_col_name() -> None:
    rows = [
        {"CASRN": "M1", "endpoint": "Agonist", "All_active": 1, "InChI_Code": "i"},
        {"CASRN": "M2", "endpoint": "Antagonist", "All_active": 1, "InChI_Code": "i"},
    ]
    labels, _ = cerapp.assemble_evaluation_labels(rows, label_mode="agonist", mode_col="endpoint")
    assert {r["casrn"]: r["label"] for r in labels} == {"M1": 1}
