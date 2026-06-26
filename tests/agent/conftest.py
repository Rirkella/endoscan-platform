"""Fixtures for the M5 Builder Agent tests (offline, fixture-backed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import BuildRecipe

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = "pipelines/endpoints/ER/run.py"
PASS_CONFIG = "tests/fixtures/training/er_pipeline.test.yaml"  # relaxed gate -> PASS
FAIL_CONFIG = "tests/fixtures/agent/er_strict.config.yaml"  # strict gate -> FAIL


@pytest.fixture
def builds_root(tmp_path: Path) -> Path:
    """A throwaway build workspace root (separate from any registry)."""
    return tmp_path / "builds"


@pytest.fixture
def output_root(tmp_path: Path) -> Path:
    """A throwaway repo-root for the registry write target (empty index, like M1/M3)."""
    index = tmp_path / "registry" / "models" / "endpoints.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text('{\n  "endpoints": []\n}\n', encoding="utf-8")
    (tmp_path / "models").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def pass_recipe() -> BuildRecipe:
    return BuildRecipe(
        name="er-pass", pipeline_runner_path=RUNNER, pipeline_config_path=PASS_CONFIG
    )


@pytest.fixture
def fail_recipe() -> BuildRecipe:
    return BuildRecipe(
        name="er-fail", pipeline_runner_path=RUNNER, pipeline_config_path=FAIL_CONFIG
    )
