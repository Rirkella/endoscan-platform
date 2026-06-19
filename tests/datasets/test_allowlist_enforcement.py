"""Allow-list enforcement: only approved sources may be used; real adapter is a stub."""

from __future__ import annotations

import pytest

from endoscan_core.datasets import (
    FixtureSourceAdapter,
    RealDownloadAdapter,
    SourceEntry,
    SourcesAllowList,
    UnregisteredSourceError,
    label_retriever,
    require_allowed,
    source_selector,
)


def test_selector_only_returns_allow_listed(allow_list: SourcesAllowList) -> None:
    ids = {s.id for s in source_selector("ER", allow_list)}
    assert "evil_source" not in ids
    assert ids <= allow_list.ids()


def test_retriever_rejects_unregistered_source(
    allow_list: SourcesAllowList, adapter: FixtureSourceAdapter
) -> None:
    evil = SourceEntry(
        id="evil_source",
        name="Open-web scrape",
        type="labels",
        access_method="scrape",
        provenance="open web",
        version="0",
    )
    with pytest.raises(UnregisteredSourceError):
        label_retriever("ER", [evil], adapter, allow_list=allow_list)


def test_require_allowed_passes_for_registered(allow_list: SourcesAllowList) -> None:
    require_allowed([allow_list.by_id("toxcast")], allow_list)  # must not raise


def test_real_download_adapter_is_stub(allow_list: SourcesAllowList) -> None:
    with pytest.raises(NotImplementedError):
        RealDownloadAdapter().read_records(allow_list.by_id("toxcast"))
