"""source_selector: target applicability + type filtering, allow-list only."""

from __future__ import annotations

from endoscan_core.datasets import SourcesAllowList, source_selector


def test_target_applicability(allow_list: SourcesAllowList) -> None:
    er_ids = {s.id for s in source_selector("ER", allow_list)}
    assert "cerapp" in er_ids
    assert "compara" not in er_ids  # AR-specific
    assert "toxcast" in er_ids  # broad

    ar_ids = {s.id for s in source_selector("AR", allow_list)}
    assert "compara" in ar_ids
    assert "cerapp" not in ar_ids


def test_type_filter(allow_list: SourcesAllowList) -> None:
    labels = source_selector("ER", allow_list, types=["labels"])
    assert {s.id for s in labels} == {"toxcast", "tox21", "cerapp"}
    assert all(s.type == "labels" for s in labels)

    assert {s.id for s in source_selector("ER", allow_list, types=["signatures"])} == {"lincs"}
    assert {s.id for s in source_selector("ER", allow_list, types=["mapping"])} == {"pubchem"}


def test_only_returns_allow_listed(allow_list: SourcesAllowList) -> None:
    selected_ids = {s.id for s in source_selector("ER", allow_list)}
    assert selected_ids <= allow_list.ids()
