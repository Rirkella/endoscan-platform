from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from sqlalchemy import select, update

from endoscan_workflows.artifacts import LocalArtifactStore
from endoscan_workflows.contracts import (
    EndpointBuildCreate,
    IdempotencyClassification,
    SideEffectClassification,
    ToolDefinition,
    ToolInvocation,
    WorkflowState,
)
from endoscan_workflows.models import (
    ScientificSourceRequestAttemptRow,
    ScientificSourceRequestBudgetRow,
    SourceCacheRow,
)
from endoscan_workflows.source_cache import SourceResponseCache
from endoscan_workflows.source_governor import ScientificSourceRequestGovernor
from endoscan_workflows.source_security import ScientificSourceClient, SourcePolicyError
from endoscan_workflows.tools import RegisteredTool, ToolInput, ToolOutput, ToolRegistry


class RequestLoopInput(ToolInput):
    prefix: str
    count: int


class RequestLoopOutput(ToolOutput):
    completed_responses: int


def _create_workflow(service, key: str) -> str:
    return service.create_build(
        EndpointBuildCreate(
            endpoint_name="Governor regression",
            endpoint_slug=f"governor-{key}",
            biological_goal="Prove exact build-global scientific-source request governance.",
            created_by="test",
            idempotency_key=key,
        )
    ).id


def _registry(
    *,
    governor: ScientificSourceRequestGovernor,
    implementation,
    timeout_seconds: float = 60.0,
) -> ToolRegistry:
    registry = ToolRegistry(source_request_governor=governor)
    registry.register(
        RegisteredTool(
            ToolDefinition(
                name="run_governed_source_loop",
                description="Exercise the common governed scientific-source transport boundary.",
                input_schema_name=RequestLoopInput.__name__,
                output_schema_name=RequestLoopOutput.__name__,
                required_permissions=["source:test:read"],
                side_effect=SideEffectClassification.EXTERNAL_READ,
                idempotency=IdempotencyClassification.IDEMPOTENT_WITH_KEY,
                timeout_seconds=timeout_seconds,
                allowed_workflow_stages=[WorkflowState.DISCOVERING_SOURCE_CANDIDATES],
                implementation_version="governor-test-v1",
            ),
            RequestLoopInput,
            RequestLoopOutput,
            implementation,
        )
    )
    return registry


def _invocation(
    workflow_id: str,
    *,
    task_id: str,
    count: int,
    maximum_requests: int,
) -> ToolInvocation:
    return ToolInvocation(
        tool_name="run_governed_source_loop",
        arguments={"prefix": task_id, "count": count},
        workflow_id=workflow_id,
        workflow_stage=WorkflowState.DISCOVERING_SOURCE_CANDIDATES,
        permission_scope=["source:test:read"],
        run_context={
            "agent_role": "request-governor-regression",
            "discovery_task_id": task_id,
            "discovery_round": 0,
            "maximum_global_scientific_source_requests": maximum_requests,
        },
        idempotency_key=task_id,
    )


def test_concurrent_paginated_providers_share_exact_global_cap(workflow_runtime) -> None:
    database, _store, _providers, _harness, service = workflow_runtime
    workflow_id = _create_workflow(service, "concurrent-cap")
    governor = ScientificSourceRequestGovernor(database)
    actual_transport_count = 0
    lock = threading.Lock()

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal actual_transport_count
        with lock:
            actual_transport_count += 1
        return httpx.Response(200, json={"ok": True}, request=request)

    client = ScientificSourceClient(
        maximum_attempts=1,
        requests_per_second=10,
        transport=httpx.MockTransport(transport),
        sleep=lambda _seconds: None,
    )

    def implementation(request: RequestLoopInput) -> RequestLoopOutput:
        completed = 0
        for index in range(request.count):
            try:
                client.get(
                    f"https://eutils.ncbi.nlm.nih.gov/governor/{request.prefix}/{index}",
                    tool_name="governed_source_test",
                    maximum_attempts=1,
                )
            except SourcePolicyError:
                break
            completed += 1
        return RequestLoopOutput(completed_responses=completed)

    registry = _registry(governor=governor, implementation=implementation)
    invocations = [
        _invocation(
            workflow_id,
            task_id=f"concurrent-provider-{index}",
            count=40,
            maximum_requests=80,
        )
        for index in range(4)
    ]
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(registry.invoke, invocations))

    assert all(result.output is not None for result in results)
    assert actual_transport_count == 80
    accounting = governor.reconcile(workflow_id, 0)
    assert accounting is not None
    assert accounting.maximum_requests == 80
    assert accounting.reserved_requests == 80
    assert accounting.completed_transport_attempts == 80
    assert accounting.failed_transport_attempts == 0
    assert accounting.blocked_requests >= 1
    assert accounting.remaining_requests == 0
    with database.session() as session:
        transported = session.scalars(
            select(ScientificSourceRequestAttemptRow).where(
                ScientificSourceRequestAttemptRow.workflow_id == workflow_id,
                ScientificSourceRequestAttemptRow.request_left_process == 1,
            )
        ).all()
        blocked = session.scalars(
            select(ScientificSourceRequestAttemptRow).where(
                ScientificSourceRequestAttemptRow.workflow_id == workflow_id,
                ScientificSourceRequestAttemptRow.outcome == "blocked_budget_exhausted",
            )
        ).all()
    assert len(transported) == 80
    assert blocked
    client.close()


def test_timeout_cancels_next_request_and_preserves_partial_evidence(
    workflow_runtime, tmp_path: Path
) -> None:
    database, _store, _providers, _harness, service = workflow_runtime
    workflow_id = _create_workflow(service, "timeout-partial")
    governor = ScientificSourceRequestGovernor(database)
    artifacts = LocalArtifactStore(database, tmp_path / "governor-artifacts")
    cache = SourceResponseCache(database)
    transport_started: list[float] = []
    worker_finished = threading.Event()

    def transport(request: httpx.Request) -> httpx.Response:
        transport_started.append(time.monotonic())
        time.sleep(0.15)
        return httpx.Response(200, json={"index": len(transport_started)}, request=request)

    client = ScientificSourceClient(
        maximum_attempts=1,
        requests_per_second=10,
        transport=httpx.MockTransport(transport),
        sleep=lambda _seconds: None,
    )

    def implementation(request: RequestLoopInput) -> RequestLoopOutput:
        completed = 0
        try:
            for index in range(request.count):
                arguments = {"prefix": request.prefix, "index": index}
                cached = cache.get("governed_partial", arguments)
                if cached is not None:
                    completed += 1
                    continue
                response = client.get(
                    f"https://eutils.ncbi.nlm.nih.gov/partial/{request.prefix}/{index}",
                    tool_name="governed_source_test",
                    maximum_attempts=1,
                )
                artifact = artifacts.put_bytes(
                    workflow_id=workflow_id,
                    content=response.content,
                    mime_type="application/json",
                    artifact_type="scientific_source_raw",
                    logical_name=f"partial-{request.prefix}-{index}.json",
                    producer="governor-regression",
                    original_source=response.url,
                    idempotency_key=f"partial:{request.prefix}:{index}",
                )
                cache.put(
                    "governed_partial",
                    arguments,
                    source_url=response.url,
                    content_hash=response.sha256,
                    parsed_output={"record_count": 1},
                    raw_artifact_id=artifact.id,
                    http_metadata={
                        "status_code": response.status_code,
                        "content_type": response.content_type,
                        "response_bytes": len(response.content),
                    },
                )
                completed += 1
        finally:
            worker_finished.set()
        return RequestLoopOutput(completed_responses=completed)

    registry = _registry(
        governor=governor,
        implementation=implementation,
        timeout_seconds=0.08,
    )
    result = registry.invoke(
        _invocation(
            workflow_id,
            task_id="slow-provider",
            count=5,
            maximum_requests=20,
        )
    )
    assert result.status.value == "timed_out"
    assert worker_finished.wait(timeout=2)
    assert len(transport_started) == 1
    with database.session() as session:
        cache_rows = session.scalars(select(SourceCacheRow)).all()
    assert len(cache_rows) == 1
    descriptor, content = artifacts.get(cache_rows[0].raw_artifact_id)
    assert descriptor.sha256 == hashlib.sha256(content).hexdigest()
    accounting = governor.reconcile(workflow_id, 0)
    assert accounting is not None
    assert accounting.reserved_requests == 1
    assert accounting.completed_transport_attempts == 1
    assert accounting.prevented_by_cancellation >= 1
    client.close()


def test_cache_only_restart_keeps_transport_budget_and_reconciliation_hash_stable(
    workflow_runtime, tmp_path: Path
) -> None:
    database, _store, _providers, _harness, service = workflow_runtime
    workflow_id = _create_workflow(service, "cache-restart")
    governor = ScientificSourceRequestGovernor(database)
    artifacts = LocalArtifactStore(database, tmp_path / "cache-artifacts")
    cache = SourceResponseCache(database)
    actual_transport_count = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal actual_transport_count
        actual_transport_count += 1
        return httpx.Response(200, json={"ok": True}, request=request)

    client = ScientificSourceClient(
        maximum_attempts=1,
        requests_per_second=10,
        transport=httpx.MockTransport(transport),
        sleep=lambda _seconds: None,
    )

    def implementation(request: RequestLoopInput) -> RequestLoopOutput:
        completed = 0
        for index in range(request.count):
            arguments = {"prefix": request.prefix, "index": index}
            cached = cache.get("governed_restart", arguments)
            if cached is not None:
                completed += 1
                continue
            response = client.get(
                f"https://eutils.ncbi.nlm.nih.gov/restart/{request.prefix}/{index}",
                tool_name="governed_source_test",
                maximum_attempts=1,
            )
            artifact = artifacts.put_bytes(
                workflow_id=workflow_id,
                content=response.content,
                mime_type="application/json",
                artifact_type="scientific_source_raw",
                logical_name=f"restart-{request.prefix}-{index}.json",
                producer="governor-regression",
                original_source=response.url,
                idempotency_key=f"restart:{request.prefix}:{index}",
            )
            cache.put(
                "governed_restart",
                arguments,
                source_url=response.url,
                content_hash=response.sha256,
                parsed_output={"record_count": 1},
                raw_artifact_id=artifact.id,
                http_metadata={
                    "status_code": 200,
                    "content_type": "application/json",
                    "response_bytes": len(response.content),
                },
            )
            completed += 1
        return RequestLoopOutput(completed_responses=completed)

    first_registry = _registry(governor=governor, implementation=implementation)
    invocation = _invocation(
        workflow_id,
        task_id="restart-provider",
        count=2,
        maximum_requests=5,
    )
    assert first_registry.invoke(invocation).output is not None
    first = governor.reconcile(workflow_id, 0)
    assert first is not None
    first_hash = hashlib.sha256(json.dumps(first.as_dict(), sort_keys=True).encode()).hexdigest()
    assert actual_transport_count == 2
    assert first.remaining_requests == 3

    restarted_governor = ScientificSourceRequestGovernor(database)
    restarted_registry = _registry(
        governor=restarted_governor,
        implementation=implementation,
    )
    assert restarted_registry.invoke(invocation).output is not None
    second = restarted_governor.reconcile(workflow_id, 0)
    assert second is not None
    assert actual_transport_count == 2
    assert second.reserved_requests == 2
    assert second.remaining_requests == 3
    assert second.cache_hits == 2
    second_hash = hashlib.sha256(json.dumps(second.as_dict(), sort_keys=True).encode()).hexdigest()

    assert restarted_registry.invoke(invocation).output is not None
    stable_refresh = restarted_governor.reconcile(workflow_id, 0)
    assert stable_refresh == second
    assert (
        hashlib.sha256(json.dumps(stable_refresh.as_dict(), sort_keys=True).encode()).hexdigest()
        == second_hash
    )

    with database.session() as session:
        budget = session.scalar(
            select(ScientificSourceRequestBudgetRow).where(
                ScientificSourceRequestBudgetRow.workflow_id == workflow_id
            )
        )
        assert budget is not None
        session.execute(
            update(ScientificSourceRequestBudgetRow)
            .where(ScientificSourceRequestBudgetRow.id == budget.id)
            .values(
                reserved_requests=0,
                completed_transport_attempts=0,
                cache_hits=0,
            )
        )
    recovered = restarted_governor.reconcile(workflow_id, 0)
    assert recovered == second
    recovered_again = restarted_governor.reconcile(workflow_id, 0)
    assert recovered_again == recovered
    recovered_hash = hashlib.sha256(
        json.dumps(recovered.as_dict(), sort_keys=True).encode()
    ).hexdigest()
    assert recovered_hash == second_hash
    assert first_hash != second_hash
    client.close()
