"""Allow-list loading and schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from endoscan_core.datasets import SourceEntry, SourcesAllowList, load_sources


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
