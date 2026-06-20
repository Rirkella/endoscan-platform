"""Shared fixtures for M3 training/pipeline tests (offline, deterministic)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from endoscan_core.datasets import (
    CandidateTable,
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

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "datasets"
RUN_PY = REPO_ROOT / "pipelines" / "endpoints" / "ER" / "run.py"


@pytest.fixture(scope="session")
def er_run() -> ModuleType:
    """Import the config-driven runner from its file path (pipelines is not a package)."""
    spec = importlib.util.spec_from_file_location("er_run", RUN_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec so pydantic can resolve the module's forward refs.
    sys.modules["er_run"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def allow_list() -> SourcesAllowList:
    return load_sources(repo_root=REPO_ROOT)


@pytest.fixture
def candidate_table(allow_list: SourcesAllowList) -> CandidateTable:
    """Build the ER candidate table from the M2 fixtures (separable signal)."""
    adapter = FixtureSourceAdapter(DATASET_FIXTURES)
    target = "ER"
    labels = label_retriever(
        target,
        source_selector(target, allow_list, types=["labels"]),
        adapter,
        allow_list=allow_list,
    )
    sigs = signature_retriever(
        source_selector(target, allow_list, types=["signatures"]), adapter, allow_list=allow_list
    )
    ids = [(r.compound_id, r.compound_id_type) for r in labels.records]
    ids += [(s.compound_id, s.compound_id_type) for s in sigs.records]
    mapping = compound_mapper(
        ids,
        source_selector(target, allow_list, types=["mapping"])[0],
        adapter,
        allow_list=allow_list,
    )
    overlap = overlap_computer(labels, sigs, mapping)
    return candidate_table_builder(labels, sigs, overlap, mapping, n_groups=2, seed=0)


@pytest.fixture
def tmp_output_root(tmp_path: Path) -> Path:
    """A throwaway repo-root for artifacts + an empty registry index (like M1)."""
    index = tmp_path / "registry" / "models" / "endpoints.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text('{\n  "endpoints": []\n}\n', encoding="utf-8")
    (tmp_path / "registry" / "data" / "dataset_cards").mkdir(parents=True, exist_ok=True)
    (tmp_path / "models").mkdir(parents=True, exist_ok=True)
    return tmp_path
