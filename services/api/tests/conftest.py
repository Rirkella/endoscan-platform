"""Shared fixtures: a TestClient bound to the REAL committed repo (no network, no dvc)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from endoscan_api import create_app

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def client() -> TestClient:
    # Explicit repo_root == the committed workspace: models load from git alone.
    return TestClient(create_app(repo_root=REPO_ROOT))


@pytest.fixture
def full_signature() -> Callable[..., dict[str, float]]:
    """Factory: a schema-complete signature (every landmark gene present, finite)."""

    def _make(endpoint_id: str, value: float = 0.0) -> dict[str, float]:
        schema = json.loads(
            (REPO_ROOT / "models" / endpoint_id / "feature_schema.json").read_text(encoding="utf-8")
        )
        return {gene: value for gene in schema["features"]}

    return _make
