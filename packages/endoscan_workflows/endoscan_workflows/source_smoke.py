"""Isolated, production-composed persistence for reviewed-source smoke gates."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from .artifacts import LocalArtifactStore
from .contracts import EndpointBuildCreate, ToolInvocation, WorkflowState
from .database import WorkflowDatabase
from .reviewed_source_adapters import (
    ReviewedSourceAdapterRegistry,
    ReviewedSourceExecutionResult,
    ReviewedSourceOperationInput,
    production_reviewed_source_registry,
)
from .service import WorkflowService
from .source_cache import SourceResponseCache
from .source_security import ScientificSourceClient
from .state_machine import WorkflowGraph

SMOKE_BUILD_KEY = "isolated-reviewed-source-smoke-build-v1"
SMOKE_STEP_KEY = "isolated-reviewed-source-smoke-step-v1"


class IsolatedReviewedSourceSmokeRuntime:
    """Real SQLite/artifact/cache composition that never touches normal runtime state."""

    def __init__(
        self,
        *,
        root: Path,
        repo_root: Path,
        database: WorkflowDatabase,
        artifacts: LocalArtifactStore,
        cache: SourceResponseCache,
        registry: ReviewedSourceAdapterRegistry,
        service: WorkflowService,
        workflow_id: str,
        step_id: str,
    ) -> None:
        self.root = root
        self.repo_root = repo_root
        self.database = database
        self.artifacts = artifacts
        self.cache = cache
        self.registry = registry
        self.service = service
        self.workflow_id = workflow_id
        self.step_id = step_id

    @classmethod
    def open(
        cls,
        *,
        root: Path,
        repo_root: Path,
        client: ScientificSourceClient,
    ) -> IsolatedReviewedSourceSmokeRuntime:
        isolated_root = Path(root).resolve()
        repository_root = Path(repo_root).resolve()
        isolated_root.mkdir(parents=True, exist_ok=True)
        database = WorkflowDatabase(isolated_root / "workflow.db")
        database.migrate()
        artifacts = LocalArtifactStore(
            database,
            isolated_root / "artifacts",
            maximum_bytes=1_500_000,
        )
        cache = SourceResponseCache(database)
        registry = production_reviewed_source_registry(
            client=client,
            cache=cache,
            artifacts=artifacts,
        )
        service = WorkflowService(
            database,
            artifacts,
            WorkflowGraph(repository_root / "docs" / "agents" / "workflow-state-machine.json"),
            repo_root=repository_root,
            reviewed_source_adapters=registry,
        )
        build = service.create_build(
            EndpointBuildCreate(
                endpoint_name="Isolated reviewed source smoke",
                endpoint_slug="isolated-reviewed-source-smoke",
                biological_goal=(
                    "Verify bounded reviewed-source transport, parsing, artifact provenance, "
                    "and cache persistence without creating scientific candidates."
                ),
                created_by="source-smoke-harness",
                idempotency_key=SMOKE_BUILD_KEY,
            )
        )
        step = service.create_step(
            build.id,
            WorkflowState.DRAFT,
            idempotency_key=SMOKE_STEP_KEY,
            input_payload={"technical_smoke_only": True, "scientific_candidate": False},
        )
        runtime = cls(
            root=isolated_root,
            repo_root=repository_root,
            database=database,
            artifacts=artifacts,
            cache=cache,
            registry=registry,
            service=service,
            workflow_id=build.id,
            step_id=step.id,
        )
        runtime.assert_coherent_persistence()
        return runtime

    def assert_coherent_persistence(self) -> None:
        if self.artifacts.database is not self.database or self.cache.database is not self.database:
            raise RuntimeError(
                "Smoke artifact store and source cache must share one workflow database."
            )
        capability = self.database.capability()
        if not capability.get("foreign_keys"):
            raise RuntimeError("Smoke workflow database must enforce foreign keys.")
        if self.artifacts.root.parent != self.root:
            raise RuntimeError("Smoke artifact root must remain inside the isolated runtime root.")

    def execute(
        self,
        operation: str,
        request: ReviewedSourceOperationInput,
        *,
        idempotency_key: str,
    ) -> ReviewedSourceExecutionResult:
        return self.registry.execute_with_telemetry(
            operation,
            request,
            ToolInvocation(
                tool_name=operation,
                arguments=request.model_dump(mode="json"),
                workflow_id=self.workflow_id,
                step_id=self.step_id,
                workflow_stage=WorkflowState.DRAFT,
                idempotency_key=idempotency_key,
            ),
        )

    def close(self) -> None:
        self.database.dispose()

    def __enter__(self) -> IsolatedReviewedSourceSmokeRuntime:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@contextmanager
def isolated_reviewed_source_smoke_runtime(
    *,
    repo_root: Path,
    client: ScientificSourceClient,
) -> Iterator[IsolatedReviewedSourceSmokeRuntime]:
    """Create and clean up an isolated production-equivalent persistence boundary."""

    with TemporaryDirectory(prefix="endoscan-source-smoke-") as temporary:
        runtime = IsolatedReviewedSourceSmokeRuntime.open(
            root=Path(temporary),
            repo_root=repo_root,
            client=client,
        )
        try:
            yield runtime
        finally:
            runtime.close()
