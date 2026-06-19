"""signature_retriever: feature vectors, metadata, compound filter."""

from __future__ import annotations

from types import SimpleNamespace

from endoscan_core.datasets import SourcesAllowList, signature_retriever, source_selector


def test_feature_names_and_metadata(er_artifacts: SimpleNamespace) -> None:
    sigs = er_artifacts.signatures
    assert sigs.feature_names == ["GENE_A", "GENE_B", "GENE_C", "GENE_D", "GENE_E"]

    s1 = next(r for r in sigs.records if r.signature_id == "S1")
    assert s1.perturbagen_id == "BRD-C1"
    assert s1.compound_id_type == "PERT_ID"
    assert s1.metadata.cell_line == "MCF7"
    assert s1.metadata.dose == 10.0
    assert s1.metadata.time == 24.0
    assert set(s1.features) == set(sigs.feature_names)


def test_compounds_filter(allow_list: SourcesAllowList, adapter) -> None:
    sources = source_selector("ER", allow_list, types=["signatures"])
    sigs = signature_retriever(sources, adapter, compounds={"BRD-C2"})
    assert {r.perturbagen_id for r in sigs.records} == {"BRD-C2"}
