"""compound_mapper: canonical InChIKey, unmapped, ambiguous, block1, SMILES."""

from __future__ import annotations

from types import SimpleNamespace


def test_exact_mapping_to_full_inchikey(er_artifacts: SimpleNamespace) -> None:
    assert er_artifacts.mapping.to_canonical("100-00-1", "CASRN") == "AAAAAAAAAAAAAA-AAAAAAAAAA-A"
    assert er_artifacts.mapping.to_canonical("BRD-C1", "PERT_ID") == "AAAAAAAAAAAAAA-AAAAAAAAAA-A"


def test_unmapped_recorded(er_artifacts: SimpleNamespace) -> None:
    assert any(u.input_id == "100-00-8" for u in er_artifacts.mapping.unmapped)
    assert er_artifacts.mapping.to_canonical("100-00-8", "CASRN") is None


def test_ambiguous_recorded_not_resolved(er_artifacts: SimpleNamespace) -> None:
    conflict = next(c for c in er_artifacts.mapping.conflicts if c.input_id == "100-00-9")
    assert len(conflict.canonical_ids) == 2
    assert er_artifacts.mapping.to_canonical("100-00-9", "CASRN") is None


def test_block1_and_smiles_recorded(er_artifacts: SimpleNamespace) -> None:
    mapping = next(
        m
        for m in er_artifacts.mapping.mappings
        if m.input_id == "100-00-1" and m.confidence == "exact"
    )
    assert mapping.inchikey_block1 == "AAAAAAAAAAAAAA"
    assert len(mapping.inchikey_block1) == 14
    # SMILES is carried only as an identifier/provenance field.
    assert mapping.smiles is not None
