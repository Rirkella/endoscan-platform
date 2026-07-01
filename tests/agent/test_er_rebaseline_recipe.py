"""ER re-baseline repo prerequisites: the recipe + staged config load and are valid,
point at the UNCHANGED gate thresholds, and leave ER's frozen registry entry untouched.

This PR is repo-side ONLY (no server run, no un-freeze, no rebuilt artifacts). These tests
therefore VALIDATE the recipe/config — they do NOT run start_build on ER (that needs the
re-extracted data/staged/er/lincs.parquet, which does not exist until the server rebuild).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import yaml

from agent import BuildRecipe
from agent import builder_agent as ba

REPO_ROOT = Path(__file__).resolve().parents[2]
ER_RUNNER = "pipelines/endpoints/ER/run.py"
ER_RECIPE = REPO_ROOT / "pipelines/endpoints/ER/recipe.yaml"
ER_REAL_CONFIG = REPO_ROOT / "pipelines/endpoints/ER/config.real.yaml"
ER_FIXTURE_CONFIG = "pipelines/endpoints/ER/config.yaml"


def _load_runner() -> ModuleType:
    return ba._load_runner(REPO_ROOT / ER_RUNNER)


def _recipe() -> BuildRecipe:
    return BuildRecipe.model_validate(yaml.safe_load(ER_RECIPE.read_text(encoding="utf-8")))


def test_er_recipe_is_valid_and_points_at_the_generic_runner_and_real_config() -> None:
    recipe = _recipe()
    assert recipe.name == "er_rebaseline"
    assert recipe.pipeline_runner_path == ER_RUNNER  # reuses the generic ER runner
    assert recipe.pipeline_config_path == "pipelines/endpoints/ER/config.real.yaml"
    # The referenced config file exists and loads through the runner's own loader.
    runner = _load_runner()
    config = runner.load_config(ER_REAL_CONFIG)
    assert config.endpoint_id == "ER" and config.data.target == "ER"


def test_er_real_config_is_staged_cerapp_and_blocked() -> None:
    config = _load_runner().load_config(ER_REAL_CONFIG)
    assert config.data.adapter == "staged"
    assert config.data.staged_dir == "data/staged/er"
    assert config.data.required_label_sources == ["cerapp"]  # CERAPP functional call, no union
    assert config.data.seed == 0 and config.data.n_groups == 10  # same as the original ER run
    # Committed BLOCKED: only the agent's promote() token may authorize training.
    assert config.approval.approved is False


def test_er_rebuild_uses_same_thresholds_and_floors_no_change() -> None:
    runner = _load_runner()
    real = runner.load_config(ER_REAL_CONFIG)
    fixture = runner.load_config(REPO_ROOT / ER_FIXTURE_CONFIG)
    # The SAME strict gate file — not a rebuild-special (lowered) thresholds file.
    assert real.gate.thresholds_path == "registry/data/quality_gates.yaml"
    # Floors/ceilings identical to the committed ER config — the rebuild lowers nothing.
    assert real.training.validated_mvp_floors == fixture.training.validated_mvp_floors
    assert real.training.validated_mvp_ceilings == fixture.training.validated_mvp_ceilings


def test_er_frozen_registry_entry_intact() -> None:
    # ER's frozen baseline entry stays intact regardless of other endpoints being added
    # (AR was registered separately). The rebaseline recipe/config PR does not touch it.
    index = json.loads((REPO_ROOT / "registry/models/endpoints.json").read_text())
    ids = [e["endpoint_id"] for e in index["endpoints"]]
    assert "ER" in ids
    er = next(e for e in index["endpoints"] if e["endpoint_id"] == "ER")
    assert er["frozen"] is True and er["status"] == "experimental"
    assert er["model_path"] == "models/ER/model.pkl"  # entry shape unchanged
