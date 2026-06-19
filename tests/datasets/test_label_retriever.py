"""label_retriever: normalization, target filtering, conflict recording."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from endoscan_core.datasets import SourcesAllowList, label_retriever


def test_target_filtered_and_inconclusive_dropped(er_artifacts: SimpleNamespace) -> None:
    casrns = {r.compound_id for r in er_artifacts.labels.records}
    assert all(r.target == "ER" for r in er_artifacts.labels.records)
    assert "100-00-99" not in casrns  # AR row excluded
    assert "100-00-10" not in casrns  # tox21 inconclusive dropped


def test_labels_are_binary_with_source_and_confidence(er_artifacts: SimpleNamespace) -> None:
    for record in er_artifacts.labels.records:
        assert record.label in (0, 1)
        assert record.assay_source in {"toxcast", "tox21", "cerapp"}
        assert 0.0 <= record.confidence <= 1.0


def test_conflict_recorded_not_resolved(er_artifacts: SimpleNamespace) -> None:
    conflicts = er_artifacts.labels.conflicts
    assert any(c.compound_id == "100-00-7" for c in conflicts)
    # Both conflicting records are kept (never silently picked).
    c7 = [r for r in er_artifacts.labels.records if r.compound_id == "100-00-7"]
    assert {r.label for r in c7} == {0, 1}


def test_rejects_non_labels_source(allow_list: SourcesAllowList, adapter) -> None:
    lincs = allow_list.by_id("lincs")
    with pytest.raises(ValueError):
        label_retriever("ER", [lincs], adapter)
