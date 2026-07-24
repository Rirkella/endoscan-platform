from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import func, select

from endoscan_workflows.contracts import EndpointBuildCreate
from endoscan_workflows.discovery_strategy import (
    ArtifactReference,
    DiscoveryBudgets,
    DiscoveryPlan,
    EvidenceRole,
    OperationalCapability,
    PaginationCompletionPolicy,
    ProviderDiscoveryTask,
)
from endoscan_workflows.models import ScientificSourceRequestAttemptRow
from endoscan_workflows.source_governor import (
    ScientificSourceBudgetExhausted,
    ScientificSourceRequestGovernor,
)
from endoscan_workflows.source_scheduler import (
    BuildFairRequestScheduler,
    ScientificSourceTaskAllocationExhausted,
)


def _workflow(service, key: str) -> str:
    return service.create_build(
        EndpointBuildCreate(
            endpoint_name="Fair scheduler regression",
            endpoint_slug=f"fair-scheduler-{key}",
            biological_goal="Prove fair bounded TR source scheduling.",
            created_by="test",
            idempotency_key=f"fair-scheduler-{key}",
        )
    ).id


def _task(
    workflow_id: str,
    provider: str,
    role: EvidenceRole,
    modality: str | None,
) -> ProviderDiscoveryTask:
    suffix = f"{provider}-{role.value}-{modality or 'none'}"
    return ProviderDiscoveryTask(
        task_id=f"task-{hashlib.sha256(suffix.encode()).hexdigest()[:24]}",
        provider=provider,
        evidence_role=role,
        modality=modality,
        operation=f"operation-{provider}",
        typed_input_contract="TypedInput",
        typed_output_contract="TypedOutput",
        query_parameters={},
        required_capabilities=[OperationalCapability.CANDIDATE_SEARCH],
        pagination_policy=PaginationCompletionPolicy(
            maximum_pages=500,
            completion_conditions=["cursor exhausted"],
        ),
    )


def _plan(workflow_id: str) -> DiscoveryPlan:
    modalities = ["binding", "agonism", "antagonism"]
    tasks = [
        _task(workflow_id, provider, EvidenceRole.ACTIVITY, modality)
        for modality in modalities
        for provider in ("pubchem-bioassay", "tox21", "toxcast")
    ]
    tasks.extend(
        [
            _task(workflow_id, "lincs-l1000", EvidenceRole.TRANSCRIPTOMIC, None),
            _task(workflow_id, "pubchem-compound", EvidenceRole.IDENTITY, None),
            _task(workflow_id, "ncbi-geo", EvidenceRole.TRANSCRIPTOMIC, None),
            _task(
                workflow_id,
                "ncbi-supporting-metadata",
                EvidenceRole.SUPPORTING_METADATA,
                None,
            ),
        ]
    )
    return DiscoveryPlan.create(
        workflow_id=workflow_id,
        endpoint_identifier="thyroid-hormone-receptor",
        approved_endpoint_specification_artifact=ArtifactReference(
            artifact_id="artifact-specification",
            sha256="a" * 64,
            artifact_type="approved_specification",
        ),
        discovery_round=0,
        applicable_registered_providers=sorted({task.provider for task in tasks}),
        target_identifiers=["Thyroid hormone receptor"],
        target_synonyms=[],
        requested_modalities=modalities,
        provider_specific_query_tasks=tasks,
        required_hydration_fields={
            "activity": ["compound_id"],
            "transcriptomic": ["perturbagen_id"],
            "identity": ["canonical_id"],
            "supporting_metadata": ["provenance"],
        },
        allowed_preapproval_tool_capabilities=[OperationalCapability.CANDIDATE_SEARCH],
        scientific_and_execution_budgets=DiscoveryBudgets(
            maximum_provider_invocations=0,
            maximum_tool_calls=64,
            maximum_scientific_source_requests=80,
            maximum_input_tokens=0,
            maximum_output_tokens=0,
            maximum_estimated_cost_usd=0.0,
            timeout_seconds=1200.0,
        ),
    )


def _ready_toxcast(plan: DiscoveryPlan) -> dict[str, dict[str, object]]:
    return {
        task.task_id: {
            "status": "ready",
            "release_id": "invitrodb-v4.3-2025-08",
            "cache_only_execution_possible": True,
            "normalized_sqlite_sha256": "b" * 64,
        }
        for task in plan.provider_specific_query_tasks
        if task.provider == "toxcast"
    }


def test_80_request_simulation_preserves_breadth_and_exact_global_cap(
    workflow_runtime,
) -> None:
    database, _store, _providers, _harness, service = workflow_runtime
    workflow_id = _workflow(service, "complete-80")
    plan = _plan(workflow_id)
    governor = ScientificSourceRequestGovernor(database)
    scheduler = governor.scheduler
    scheduler.ensure_policy(plan, prerequisite_findings=_ready_toxcast(plan))

    execution_order: list[str] = []
    for _ in plan.provider_specific_query_tasks:
        state = scheduler.state(workflow_id, 0)
        assert state is not None
        pending = {
            item["task_id"]
            for item in state["tasks"]
            if item["status"] in {"pending", "prerequisite_blocked"}
        }
        task_id = scheduler.next_task_id(workflow_id, 0, pending)
        assert task_id is not None
        decision = scheduler.prepare_task(workflow_id, 0, task_id)
        execution_order.append(task_id)
        while True:
            try:
                attempt_id = governor.reserve(
                    workflow_id=workflow_id,
                    discovery_round=0,
                    maximum_requests=80,
                    task_id=task_id,
                    tool_name=f"simulation-{decision.provider}",
                    url=f"https://eutils.ncbi.nlm.nih.gov/fair/{task_id}",
                    params=None,
                )
            except (ScientificSourceTaskAllocationExhausted, ScientificSourceBudgetExhausted):
                break
            governor.mark_started(attempt_id)
            governor.mark_completed(
                attempt_id,
                http_status=200,
                final_approved_host="eutils.ncbi.nlm.nih.gov",
            )
        scheduler.mark_terminal(
            workflow_id,
            0,
            task_id,
            outcome="completed",
            completion_reason="deterministic simulation",
        )

    accounting = governor.reconcile(workflow_id, 0)
    assert accounting is not None
    assert accounting.reserved_requests == 80
    assert accounting.completed_transport_attempts == 80
    state = scheduler.reconcile(workflow_id, 0)
    assert state is not None
    assert max(item["consumed_requests"] for item in state["tasks"]) <= 16
    assert all(item["consumed_requests"] > 0 for item in state["tasks"])
    assert execution_order[0] == next(
        task.task_id
        for task in plan.provider_specific_query_tasks
        if task.provider == "lincs-l1000"
    )
    activity = [item for item in state["tasks"] if item["evidence_role"] == "activity"]
    assert {item["modality"] for item in activity} == {
        "binding",
        "agonism",
        "antagonism",
    }
    assert all(item["modality"] for item in activity)
    with database.session() as session:
        physical = session.scalar(
            select(func.count(ScientificSourceRequestAttemptRow.id)).where(
                ScientificSourceRequestAttemptRow.workflow_id == workflow_id,
                ScientificSourceRequestAttemptRow.request_left_process == 1,
            )
        )
    assert physical == 80
    with pytest.raises(ScientificSourceBudgetExhausted):
        governor.reserve(
            workflow_id=workflow_id,
            discovery_round=0,
            maximum_requests=80,
            task_id="request-81-global-proof",
            tool_name="simulation-global-cap",
            url="https://eutils.ncbi.nlm.nih.gov/fair/request-81",
            params=None,
        )
    with database.session() as session:
        physical_after_block = session.scalar(
            select(func.count(ScientificSourceRequestAttemptRow.id)).where(
                ScientificSourceRequestAttemptRow.workflow_id == workflow_id,
                ScientificSourceRequestAttemptRow.request_left_process == 1,
            )
        )
    assert physical_after_block == 80


def test_round_restart_and_unused_reallocation_are_deterministic(workflow_runtime) -> None:
    database, _store, _providers, _harness, service = workflow_runtime
    workflow_id = _workflow(service, "restart")
    plan = _plan(workflow_id)
    scheduler = BuildFairRequestScheduler(database)
    scheduler.ensure_policy(plan, prerequisite_findings=_ready_toxcast(plan))
    pending = {item.task_id for item in plan.provider_specific_query_tasks}
    first = scheduler.next_task_id(workflow_id, 0, pending)
    assert first is not None
    prepared = scheduler.prepare_task(workflow_id, 0, first)
    assert prepared.provider == "lincs-l1000"
    assert scheduler.acquire_if_configured(workflow_id, 0, first)
    assert scheduler.acquire_if_configured(workflow_id, 0, first)
    before_terminal = scheduler.state(workflow_id, 0)
    assert before_terminal is not None
    scheduler.mark_terminal(
        workflow_id,
        0,
        first,
        outcome="completed_no_candidates",
        completion_reason="bounded no-candidate first pass",
    )
    after_terminal = scheduler.state(workflow_id, 0)
    assert after_terminal is not None
    released = next(item for item in after_terminal["tasks"] if item["task_id"] == first)
    assert released["released_requests"] == prepared.allocated_requests - 2
    assert (
        after_terminal["available_shared_remainder"]
        > (before_terminal["available_shared_remainder"])
    )

    restarted = BuildFairRequestScheduler(database)
    restarted.ensure_policy(plan, prerequisite_findings=_ready_toxcast(plan))
    assert restarted.state(workflow_id, 0) == after_terminal
    remaining = pending - {first}
    assert restarted.next_task_id(workflow_id, 0, remaining) == scheduler.next_task_id(
        workflow_id, 0, remaining
    )
    digest = hashlib.sha256(
        json.dumps(after_terminal, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    restarted_digest = hashlib.sha256(
        json.dumps(
            restarted.state(workflow_id, 0),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert restarted_digest == digest


def test_absent_toxcast_prerequisite_blocks_without_transport_and_releases_capacity(
    workflow_runtime,
) -> None:
    database, _store, _providers, _harness, service = workflow_runtime
    workflow_id = _workflow(service, "toxcast-blocked")
    plan = _plan(workflow_id)
    toxcast_findings = {
        task.task_id: {
            "status": "prerequisite_blocked",
            "release_id": "invitrodb-v4.3-2025-08",
            "cache_only_execution_possible": False,
            "remediation": "Stage the reviewed public cache after separate approval.",
        }
        for task in plan.provider_specific_query_tasks
        if task.provider == "toxcast"
    }
    governor = ScientificSourceRequestGovernor(database)
    scheduler = governor.scheduler
    scheduler.ensure_policy(plan, prerequisite_findings=toxcast_findings)
    for task in plan.provider_specific_query_tasks:
        if task.provider != "toxcast":
            continue
        decision = scheduler.prepare_task(workflow_id, 0, task.task_id)
        assert decision.status == "prerequisite_blocked"
        with pytest.raises(ScientificSourceTaskAllocationExhausted):
            governor.reserve(
                workflow_id=workflow_id,
                discovery_round=0,
                maximum_requests=80,
                task_id=task.task_id,
                tool_name="toxcast-preflight",
                url="https://clowder.edap-cluster.com/reviewed",
                params=None,
            )
        scheduler.mark_terminal(
            workflow_id,
            0,
            task.task_id,
            outcome="blocked",
            completion_reason="PUBLIC_TOXCAST_ACTIVITY_CACHE_NOT_STAGED",
        )
    with database.session() as session:
        attempts = session.scalar(
            select(func.count(ScientificSourceRequestAttemptRow.id)).where(
                ScientificSourceRequestAttemptRow.workflow_id == workflow_id
            )
        )
    assert attempts == 0
    state = scheduler.state(workflow_id, 0)
    assert state is not None
    toxcast = [item for item in state["tasks"] if item["provider"] == "toxcast"]
    assert len(toxcast) == 3
    assert all(item["consumed_requests"] == 0 for item in toxcast)
    assert all(item["status"] == "terminal" for item in toxcast)
