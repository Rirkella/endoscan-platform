"""Unit tests for the two fused-path seams: shape detection + InChIKey identity mapping."""

from __future__ import annotations

from endoscan_core.datasets import compound_mapper, signature_retriever
from endoscan_core.datasets.adapters.base import RawTable
from endoscan_core.datasets.sources import SourceEntry


class _Adapter:
    """Minimal adapter returning canned rows for any source (offline)."""

    def __init__(self, rows: RawTable) -> None:
        self._rows = rows

    def read_records(self, source: SourceEntry) -> RawTable:
        return self._rows


def _sig_source() -> SourceEntry:
    return SourceEntry(
        id="lincs",
        name="lincs",
        type="signatures",
        access_method="staged",
        provenance="test",
        version="0",
    )


def _map_source() -> SourceEntry:
    return SourceEntry(
        id="pubchem",
        name="pubchem",
        type="mapping",
        access_method="staged",
        provenance="test",
        version="0",
    )


def test_signature_level_rows_take_existing_path() -> None:
    rows = [{"sig_id": "S1", "pert_id": "BRD-1", "cell_id": "MCF7", "GENE_A": 0.5}]
    sigs = signature_retriever([_sig_source()], _Adapter(rows))
    assert sigs.granularity == "signature"
    assert sigs.records[0].compound_id_type == "PERT_ID"
    assert sigs.feature_names == ["GENE_A"]


def test_fused_rows_detected_when_no_signature_markers() -> None:
    rows = [{"compound_id": "aaaa-bbbb-n", "GENE_A": 0.5, "GENE_B": -0.5}]
    sigs = signature_retriever([_sig_source()], _Adapter(rows))
    assert sigs.granularity == "compound"
    rec = sigs.records[0]
    assert rec.compound_id == "aaaa-bbbb-n"
    assert rec.compound_id_type == "inchikey"
    assert sigs.feature_names == ["GENE_A", "GENE_B"]


def test_compound_key_alias_is_a_fused_id_not_a_feature() -> None:
    rows = [{"compound_key": "AAAA-BBBB-N", "GENE_A": 0.5}]
    sigs = signature_retriever([_sig_source()], _Adapter(rows))
    assert sigs.granularity == "compound"
    assert sigs.records[0].compound_id == "AAAA-BBBB-N"
    assert sigs.feature_names == ["GENE_A"]


def test_mapper_treats_inchikey_as_identity_and_normalizes() -> None:
    # No mapping-source row is needed for an already-canonical InChIKey; it self-maps.
    mapping = compound_mapper([("  aaaa-bbbb-n  ", "inchikey")], _map_source(), _Adapter([]))
    assert mapping.to_canonical("  aaaa-bbbb-n  ", "inchikey") == "AAAA-BBBB-N"
    assert mapping.mappings[0].canonical_id == "AAAA-BBBB-N"
    assert mapping.mappings[0].confidence == "exact"
    assert not mapping.unmapped


def test_mapper_casrn_still_requires_source_row() -> None:
    # Non-canonical id types are unchanged: a CASRN with no mapping row is unmapped.
    mapping = compound_mapper([("50-00-0", "CASRN")], _map_source(), _Adapter([]))
    assert mapping.to_canonical("50-00-0", "CASRN") is None
    assert mapping.unmapped and mapping.unmapped[0].input_id == "50-00-0"
