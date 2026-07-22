"""Shared fixtures for inference tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_PY = REPO_ROOT / "pipelines" / "endpoints" / "ER" / "run.py"


@pytest.fixture(scope="session")
def er_run() -> ModuleType:
    """Import the config-driven ER runner from its file path (pipelines is not a package)."""
    spec = importlib.util.spec_from_file_location("er_run", RUN_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["er_run"] = module  # register before exec so pydantic resolves forward refs
    spec.loader.exec_module(module)
    return module
