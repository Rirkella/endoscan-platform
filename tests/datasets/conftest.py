"""Shared fixtures for the dataset-construction toolchain tests.

All tests are offline: sources are read from the real curated allow-list, and
data is read via the `FixtureSourceAdapter` over `tests/fixtures/datasets/`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from endoscan_core.datasets import (
    FixtureSourceAdapter,
    SourcesAllowList,
    candidate_table_builder,
    compound_mapper,
    label_retriever,
    load_sources,
    overlap_computer,
    signature_retriever,
    source_selector,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "datasets"


@pytest.fixture
def allow_list() -> SourcesAllowList:
    return load_sources()


@pytest.fixture
def adapter() -> FixtureSourceAdapter:
    return FixtureSourceAdapter(FIXTURES)


@pytest.fixture
def er_artifacts(allow_list: SourcesAllowList, adapter: FixtureSourceAdapter) -> SimpleNamespace:
    """Run the full ER toolchain on fixtures and return every intermediate object."""
    target = "ER"
    label_sources = source_selector(target, allow_list, types=["labels"])
    sig_sources = source_selector(target, allow_list, types=["signatures"])
    map_source = source_selector(target, allow_list, types=["mapping"])[0]

    labels = label_retriever(target, label_sources, adapter, allow_list=allow_list)
    signatures = signature_retriever(sig_sources, adapter, allow_list=allow_list)

    ids = [(r.compound_id, r.compound_id_type) for r in labels.records]
    ids += [(s.compound_id, s.compound_id_type) for s in signatures.records]
    mapping = compound_mapper(ids, map_source, adapter, allow_list=allow_list)

    overlap = overlap_computer(labels, signatures, mapping)
    table = candidate_table_builder(labels, signatures, overlap, mapping, n_groups=2, seed=0)

    return SimpleNamespace(
        target=target,
        labels=labels,
        signatures=signatures,
        mapping=mapping,
        overlap=overlap,
        table=table,
        label_sources=label_sources,
        sig_sources=sig_sources,
        map_source=map_source,
    )
