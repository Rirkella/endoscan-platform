from __future__ import annotations

import hashlib
from pathlib import Path

from endoscan_workflows.contracts import (
    ActorType,
    AgentRunStatus,
    ApprovalRequest,
    ApprovalType,
    EndpointBuildCreate,
    ProviderToolRequest,
    ProviderTurn,
    TransitionRequest,
    WorkflowState,
)
from endoscan_workflows.discovery import DiscoveryOutput, discovery_request
from endoscan_workflows.harness import AgentHarness
from endoscan_workflows.providers import FakeAgentProvider, ProviderRegistry
from endoscan_workflows.testing import phase0_test_tool_registry

REPO_ROOT = Path(__file__).resolve().parents[3]


def harness_context(workflow_runtime):
    _database, store, _providers, harness, service = workflow_runtime
    build = service.create_build(
        EndpointBuildCreate(
            endpoint_name="Oxidative stress",
            endpoint_slug="oxidative-stress",
            biological_goal="Evaluate oxidative-stress response from transcriptomic signatures.",
            created_by="test-admin",
            idempotency_key="harness-context-build",
        )
    )
    definition = store.list_artifacts(build.id)[0]
    discovering = service.transition(
        build.id,
        TransitionRequest(
            target_state=WorkflowState.DISCOVERING_DATA,
            expected_version=0,
            idempotency_key="harness-context-transition",
            initiator=ActorType.HUMAN,
            initiator_id="test-admin",
            artifact_hashes=[definition.sha256],
        ),
    )
    step = service.create_step(
        build.id,
        WorkflowState.DISCOVERING_DATA,
        idempotency_key="harness-context-step",
        input_payload={"test": True},
    )
    request = discovery_request(
        workflow_id=build.id,
        step_id=step.id,
        endpoint_name=build.endpoint_name,
        biological_goal=build.biological_goal,
    )
    return build, discovering, step, request, harness, service


def with_mode(request, mode: str):
    return request.model_copy(update={"context": {**request.context, "fake_mode": mode}})


def test_structured_output_usage_and_trace_are_recorded(workflow_runtime) -> None:
    build, _discovering, _step, request, harness, service = harness_context(workflow_runtime)
    run_id, result = harness.run(request, DiscoveryOutput)
    assert result.status is AgentRunStatus.COMPLETED
    assert result.output["live_discovery"] is False
    assert result.usage.input_tokens > 0
    assert result.tool_calls == 4
    assert {event.event_type for event in result.trace} >= {
        "agent_run.started",
        "provider.turn.started",
        "tool_call.completed",
        "output.validated",
        "agent_run.finished",
    }
    stored = service.agent_run(run_id)
    assert len(stored["tools"]) == 4
    assert stored["status"] == "completed"
    assert len(service.agent_runs(build.id)) == 1


def test_malformed_output_is_rejected_after_one_repair(workflow_runtime) -> None:
    _build, _discovering, _step, request, harness, _service = harness_context(workflow_runtime)
    _run_id, result = harness.run(with_mode(request, "malformed"), DiscoveryOutput)
    assert result.status is AgentRunStatus.FAILED
    assert result.error.code == "malformed_output"
    assert sum(event.event_type == "output.validation_failed" for event in result.trace) == 2


def test_prohibited_tool_is_rejected_and_recorded(workflow_runtime) -> None:
    database, _store, _providers, _harness, service = workflow_runtime
    _build, _discovering, _step, request, _default, _service = harness_context(workflow_runtime)
    registry = ProviderRegistry()
    registry.register(
        "fake",
        lambda: FakeAgentProvider(
            [
                ProviderTurn(
                    kind="tool",
                    tool_request=ProviderToolRequest(
                        tool_name="shell", arguments={"command": "whoami"}
                    ),
                )
            ]
        ),
    )
    harness = AgentHarness(database, registry, phase0_test_tool_registry())
    bad = request.model_copy(
        update={
            "available_tools": ["test_success"],
            "context": {
                **request.context,
                "permission_scope": ["test:invoke"],
            },
        }
    )
    run_id, result = harness.run(bad, DiscoveryOutput)
    assert result.status is AgentRunStatus.FAILED
    assert result.error.code == "prohibited_tool"
    assert service.agent_run(run_id)["tools"][0]["status"] == "prohibited"


def test_tool_input_validation_failure(workflow_runtime) -> None:
    database, _store, _providers, _harness, _service = workflow_runtime
    _build, _discovering, _step, request, _default, _service2 = harness_context(workflow_runtime)
    registry = ProviderRegistry()
    registry.register(
        "fake",
        lambda: FakeAgentProvider(
            [
                ProviderTurn(
                    kind="tool",
                    tool_request=ProviderToolRequest(
                        tool_name="test_success", arguments={"wrong": "shape"}
                    ),
                )
            ]
        ),
    )
    harness = AgentHarness(database, registry, phase0_test_tool_registry())
    bad = request.model_copy(
        update={
            "available_tools": ["test_success"],
            "context": {**request.context, "permission_scope": ["test:invoke"]},
        }
    )
    _run_id, result = harness.run(bad, DiscoveryOutput)
    assert result.error.code == "tool_input_invalid"


def test_tool_output_validation_failure(workflow_runtime) -> None:
    database, _store, _providers, _harness, _service = workflow_runtime
    _build, _discovering, _step, request, _default, _service2 = harness_context(workflow_runtime)
    registry = ProviderRegistry()
    registry.register(
        "fake",
        lambda: FakeAgentProvider(
            [
                ProviderTurn(
                    kind="tool",
                    tool_request=ProviderToolRequest(
                        tool_name="test_output_validation_failure", arguments={"value": "x"}
                    ),
                )
            ]
        ),
    )
    harness = AgentHarness(database, registry, phase0_test_tool_registry())
    bad = request.model_copy(
        update={
            "available_tools": ["test_output_validation_failure"],
            "context": {**request.context, "permission_scope": ["test:invoke"]},
        }
    )
    _run_id, result = harness.run(bad, DiscoveryOutput)
    assert result.error.code == "tool_output_invalid"


def test_maximum_turns_and_tool_calls_are_enforced(workflow_runtime) -> None:
    _build, _discovering, _step, request, harness, _service = harness_context(workflow_runtime)
    one_turn = request.model_copy(
        update={"budget": request.budget.model_copy(update={"maximum_turns": 1})}
    )
    _run_id, result = harness.run(one_turn, DiscoveryOutput)
    assert result.status is AgentRunStatus.BUDGET_EXCEEDED
    assert result.error.code == "maximum_turns_exceeded"

    another = request.model_copy(
        update={
            "instruction_version": "tool-limit-test",
            "budget": request.budget.model_copy(update={"maximum_tool_calls": 0}),
        }
    )
    _run_id, result = harness.run(another, DiscoveryOutput)
    assert result.error.code == "maximum_tool_calls_exceeded"


def test_timeout_and_provider_failure_are_normalized(workflow_runtime) -> None:
    _build, _discovering, _step, request, harness, _service = harness_context(workflow_runtime)
    _run_id, timeout = harness.run(with_mode(request, "timeout"), DiscoveryOutput)
    assert timeout.status is AgentRunStatus.TIMED_OUT
    assert timeout.error.code == "provider_timeout"
    failed_request = with_mode(request, "failure").model_copy(
        update={"instruction_version": "failure-test"}
    )
    _run_id, failed = harness.run(failed_request, DiscoveryOutput)
    assert failed.status is AgentRunStatus.FAILED
    assert failed.error.code == "provider_failure"


def test_transient_provider_failure_retries(workflow_runtime) -> None:
    _build, _discovering, _step, request, harness, _service = harness_context(workflow_runtime)
    _run_id, result = harness.run(with_mode(request, "transient_failure"), DiscoveryOutput)
    assert result.status is AgentRunStatus.COMPLETED
    assert any(event.event_type == "provider.retry" for event in result.trace)


def test_approval_interruption_is_typed(workflow_runtime) -> None:
    _build, _discovering, _step, request, harness, _service = harness_context(workflow_runtime)
    approval = ApprovalRequest(
        workflow_id=request.workflow_id,
        stage=WorkflowState.DISCOVERING_DATA,
        approval_type=ApprovalType.DATASET_SELECTION,
        proposed_decision="Review prepared candidate.",
        evidence_summary="Prepared fixture only.",
        artifact_hashes=["0" * 64],
        agent_recommendation="review",
        requested_action="approve or revise",
    )
    approval_request = request.model_copy(
        update={
            "instruction_version": "approval-interruption-test",
            "context": {
                **request.context,
                "fake_mode": "approval",
                "approval": approval.model_dump(mode="json"),
            },
        }
    )
    _run_id, result = harness.run(approval_request, DiscoveryOutput)
    assert result.status is AgentRunStatus.APPROVAL_REQUIRED
    assert result.approval_proposal.approval_type is ApprovalType.DATASET_SELECTION


def test_deterministic_replay_returns_prior_run_without_duplicate(workflow_runtime) -> None:
    _build, _discovering, _step, request, harness, service = harness_context(workflow_runtime)
    first_id, first = harness.run(request, DiscoveryOutput)
    event_count = len(service.timeline(request.workflow_id))
    second_id, second = harness.run(request, DiscoveryOutput)
    assert first_id == second_id
    assert first.model_dump() == second.model_dump()
    assert len(service.timeline(request.workflow_id)) == event_count


def test_production_registry_is_never_mutated(workflow_runtime) -> None:
    registry = REPO_ROOT / "registry" / "models" / "endpoints.json"
    before = hashlib.sha256(registry.read_bytes()).hexdigest()
    _build, _discovering, _step, request, harness, _service = harness_context(workflow_runtime)
    _run_id, result = harness.run(request, DiscoveryOutput)
    assert result.status is AgentRunStatus.COMPLETED
    assert hashlib.sha256(registry.read_bytes()).hexdigest() == before
