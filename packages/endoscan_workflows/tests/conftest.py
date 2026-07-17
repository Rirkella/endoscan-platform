from __future__ import annotations

from pathlib import Path

import pytest

from endoscan_workflows.artifacts import LocalArtifactStore
from endoscan_workflows.database import WorkflowDatabase
from endoscan_workflows.harness import AgentHarness
from endoscan_workflows.providers import FakeAgentProvider, ProviderRegistry
from endoscan_workflows.service import WorkflowService
from endoscan_workflows.state_machine import WorkflowGraph
from endoscan_workflows.tools import phase0_tool_registry

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def workflow_runtime(tmp_path):
    database = WorkflowDatabase(tmp_path / "workflow.db")
    database.migrate()
    store = LocalArtifactStore(database, tmp_path / "artifacts", maximum_bytes=1024 * 1024)
    providers = ProviderRegistry()
    providers.register("fake", FakeAgentProvider)
    harness = AgentHarness(database, providers, phase0_tool_registry(REPO_ROOT))
    service = WorkflowService(
        database,
        store,
        WorkflowGraph(REPO_ROOT / "docs" / "agents" / "workflow-state-machine.json"),
        repo_root=REPO_ROOT,
        harness=harness,
    )
    yield database, store, providers, harness, service
    database.dispose()
