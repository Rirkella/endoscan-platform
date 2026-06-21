"""Allow-list loading and schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from endoscan_core.datasets import Locator, SourceEntry, SourcesAllowList, load_sources


def test_load_sources_has_expected_ids(allow_list: SourcesAllowList) -> None:
    assert {"toxcast", "tox21", "cerapp", "compara", "lincs", "pubchem"} <= allow_list.ids()


def test_targets_field(allow_list: SourcesAllowList) -> None:
    assert allow_list.by_id("toxcast").targets == []  # broad
    assert allow_list.by_id("cerapp").targets == ["ER"]
    assert allow_list.by_id("compara").targets == ["AR"]


def test_source_entry_rejects_bad_type() -> None:
    with pytest.raises(ValidationError):
        SourceEntry(
            id="x",
            name="X",
            type="genomics",  # not labels|signatures|mapping
            access_method="m",
            provenance="p",
            version="1",
        )


def test_allow_list_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        SourcesAllowList.model_validate({"sources": [], "extra": 1})


def test_load_sources_is_idempotent() -> None:
    assert load_sources().ids() == load_sources().ids()


def test_locators_are_optional_and_additive(allow_list: SourcesAllowList) -> None:
    # Existing entries without locators still validate (default empty list).
    assert allow_list.by_id("toxcast").locators == []
    # Enriched entries expose pinned locators.
    cerapp = allow_list.by_id("cerapp")
    assert len(cerapp.locators) >= 1
    assert all(loc.url.startswith("http") for loc in cerapp.locators)
    assert any("experimental" in (loc.name or "") for loc in cerapp.locators)


def test_source_entry_accepts_locators() -> None:
    entry = SourceEntry(
        id="x",
        name="X",
        type="labels",
        access_method="m",
        provenance="p",
        version="1",
        locators=[Locator(name="a", url="https://example.org/a", sha256="deadbeef")],
    )
    assert entry.locators[0].sha256 == "deadbeef"


def test_locator_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        Locator(name="a", url="https://example.org", bogus=1)
