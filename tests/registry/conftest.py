"""Shared fixtures for registry tests.

`tmp_registry` builds a throwaway repo-root copy (artifacts + an empty index) so
register/update tests never touch the real `registry/models/endpoints.json`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from endoscan_core.registry import EndpointEntry

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "registry"


@pytest.fixture
def fixture_entry() -> EndpointEntry:
    """The canonical valid DEMO_ER entry, loaded from the documented fixture."""
    raw = json.loads((FIXTURES / "endpoint_entry.json").read_text(encoding="utf-8"))
    return EndpointEntry.model_validate(raw)


@pytest.fixture
def tmp_registry(tmp_path: Path) -> Path:
    """A throwaway repo root: fixture artifacts under ``models/`` + an empty index.

    Returns the tmp repo root to pass as ``repo_root=`` to store functions.
    """
    shutil.copytree(FIXTURES / "models", tmp_path / "models")
    index_path = tmp_path / "registry" / "models" / "endpoints.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text('{\n  "endpoints": []\n}\n', encoding="utf-8")
    return tmp_path
