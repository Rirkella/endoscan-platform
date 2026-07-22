from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from endoscan_workflows.config import AgentConfiguration, AgentRunMode
from endoscan_workflows.contracts import (
    ActorType,
    AgentRunStatus,
    ApprovalRequest,
    ApprovalType,
    EndpointBuildCreate,
    ProviderToolRequest,
    ProviderTurn,
    TransitionRequest,
    UsageReport,
    WorkflowState,
)
from endoscan_workflows.discovery import DiscoveryOutput, discovery_request
from endoscan_workflows.discovery_tools import DiscoveryToolService
from endoscan_workflows.errors import GuardNotSatisfied
from endoscan_workflows.harness import AgentHarness
from endoscan_workflows.providers import (
    DeterministicOfflineProvider,
    ProviderFailure,
    ProviderRegistry,
)
from endoscan_workflows.source_cache import SourceResponseCache
from endoscan_workflows.source_security import ScientificSourceClient
from endoscan_workflows.testing import offline_test_tool_registry
from endoscan_workflows.tools import EchoOutput, production_tool_registry

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
    return request.model_copy(update={"context": {**request.context, "offline_fixture_mode": mode}})


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
        "offline_fixture",
        lambda: DeterministicOfflineProvider(
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
    harness = AgentHarness(database, registry, offline_test_tool_registry())
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


def test_discovery_tool_cannot_run_outside_its_substage(workflow_runtime) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    build, _discovering, step, _request, _default, _service = harness_context(workflow_runtime)
    request = discovery_request(
        workflow_id=build.id,
        step_id=step.id,
        endpoint_name=build.endpoint_name,
        biological_goal=build.biological_goal,
        configuration=AgentConfiguration(
            provider="openai",
            run_mode=AgentRunMode.LIVE,
            api_key=SecretStr("offline-placeholder-never-used"),
        ),
    )
    providers = ProviderRegistry()
    providers.register(
        "openai",
        lambda: DeterministicOfflineProvider(
            [
                ProviderTurn(
                    kind="tool",
                    tool_request=ProviderToolRequest(
                        tool_name="validate_geo_accessions",
                        arguments={"accessions": ["GSE12345"]},
                    ),
                )
            ]
        ),
    )

    def external_request_forbidden(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Out-of-stage tool policy attempted an external request")

    tools = production_tool_registry(
        REPO_ROOT,
        DiscoveryToolService(
            SourceResponseCache(database),
            store,
            ScientificSourceClient(
                transport=httpx.MockTransport(external_request_forbidden),
                sleep=lambda _seconds: None,
            ),
        ),
    )
    run_id, result = AgentHarness(database, providers, tools).run(request, DiscoveryOutput)
    assert result.status is AgentRunStatus.FAILED
    assert result.error.code == "prohibited_tool"
    stored = service.agent_run(run_id)
    assert stored["tools"][0]["tool_name"] == "validate_geo_accessions"
    assert stored["tools"][0]["status"] == "prohibited"
    started = next(
        event
        for event in stored["trace"]["events"]
        if event["event_type"] == "provider.turn.started"
    )
    assert started["detail"]["discovery_substage"] == "search_planning"
    assert started["detail"]["tools_exposed"] == ["search_geo_series"]


def test_tool_input_validation_failure(workflow_runtime) -> None:
    database, _store, _providers, _harness, _service = workflow_runtime
    _build, _discovering, _step, request, _default, _service2 = harness_context(workflow_runtime)
    registry = ProviderRegistry()
    registry.register(
        "offline_fixture",
        lambda: DeterministicOfflineProvider(
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
    harness = AgentHarness(database, registry, offline_test_tool_registry())
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
        "offline_fixture",
        lambda: DeterministicOfflineProvider(
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
    harness = AgentHarness(database, registry, offline_test_tool_registry())
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


def test_safe_provider_failure_diagnostics_are_persisted_and_logged(
    workflow_runtime, caplog
) -> None:
    database, _store, _providers, _harness, service = workflow_runtime
    build, _discovering, _step, request, _default, _service = harness_context(workflow_runtime)

    class DiagnosticFailureProvider:
        name = "diagnostic"

        def run_turn(self, *_args, **_kwargs):
            raise ProviderFailure(
                "Safe normalized message.",
                retryable=False,
                exception_class="BadRequestError",
                http_status=400,
                provider_error_code="unsupported_value",
                provider_error_type="invalid_request_error",
                provider_request_id="req_safe-diagnostic",
                provider_parameter="temperature",
            )

    registry = ProviderRegistry()
    registry.register("diagnostic", DiagnosticFailureProvider)
    harness = AgentHarness(database, registry, offline_test_tool_registry())
    diagnostic_request = request.model_copy(
        update={
            "model": request.model.model_copy(update={"provider": "diagnostic"}),
            "instruction_version": "safe-provider-diagnostic-test",
            "budget": request.budget.model_copy(update={"retry_count": 0}),
        }
    )

    caplog.set_level(logging.WARNING, logger="uvicorn.error.endoscan.workflow.provider")
    run_id, result = harness.run(diagnostic_request, DiscoveryOutput)
    assert result.error.retryable is False
    failed = next(event for event in result.trace if event.event_type == "provider.turn.failed")
    assert failed.detail == {
        "turn": 1,
        "provider_invocation": 1,
        "retryable": False,
        "exception_class": "BadRequestError",
        "http_status": 400,
        "provider_error_code": "unsupported_value",
        "provider_error_type": "invalid_request_error",
        "provider_request_id": "req_safe-diagnostic",
        "provider_parameter": "temperature",
    }
    stored = service.agent_run(run_id)
    stored_failed = next(
        event
        for event in stored["trace"]["events"]
        if event["event_type"] == "provider.turn.failed"
    )
    assert stored_failed["detail"] == failed.detail
    assert service.errors(build.id)[-1]["retryable"] is False
    logged = caplog.get_records("call")[-1].getMessage()
    assert "workflow_id=" in logged
    assert "agent_run_id=" in logged
    assert "exception_class=BadRequestError" in logged
    assert "http_status=400" in logged
    assert "provider_error_code=unsupported_value" in logged
    assert "provider_request_id=req_safe-diagnostic" in logged
    assert "retryable=False" in logged
    assert "attempt=1" in logged
    assert "Safe normalized message" not in logged


def test_local_sdk_diagnostic_is_internal_and_does_not_store_request_prompt(
    workflow_runtime, caplog
) -> None:
    database, _store, _providers, _harness, service = workflow_runtime
    build, _discovering, _step, request, _default, _service = harness_context(workflow_runtime)

    class LocalSdkFailureProvider:
        name = "diagnostic"

        def run_turn(self, *_args, **_kwargs):
            raise ProviderFailure(
                "OpenAI Agents SDK configuration was rejected locally.",
                retryable=False,
                exception_class="UserError",
                developer_message="additionalProperties should not be set for object types.",
                sdk_version="0.18.2",
                adapter_operation="run_turn",
                adapter_model="gpt-5.4-mini",
                adapter_max_turns=1,
                adapter_tool_count=6,
                adapter_output_schema="DiscoveryOutput",
                adapter_use_responses=True,
                adapter_parallel_tool_calls=False,
                adapter_store=False,
                adapter_tracing_disabled=True,
            )

    registry = ProviderRegistry()
    registry.register("diagnostic", LocalSdkFailureProvider)
    harness = AgentHarness(database, registry, offline_test_tool_registry())
    raw_prompt = "RAW-PROMPT-MUST-NOT-APPEAR-IN-DIAGNOSTICS"
    diagnostic_request = request.model_copy(
        update={
            "instructions": raw_prompt,
            "model": request.model.model_copy(update={"provider": "diagnostic"}),
            "instruction_version": "local-sdk-diagnostic-test",
            "budget": request.budget.model_copy(update={"retry_count": 0}),
        }
    )
    caplog.set_level(logging.WARNING, logger="uvicorn.error.endoscan.workflow.provider")
    run_id, result = harness.run(diagnostic_request, DiscoveryOutput)
    failed = next(event for event in result.trace if event.event_type == "provider.turn.failed")
    assert failed.detail["developer_message"] == (
        "additionalProperties should not be set for object types."
    )
    assert failed.detail["retryable"] is False
    stored = service.agent_run(run_id)
    assert stored["run_mode"] == "replay"
    assert stored["request"]["instructions_sha256"]
    assert "instructions" not in stored["request"]
    stored_failed = next(
        event
        for event in stored["trace"]["events"]
        if event["event_type"] == "provider.turn.failed"
    )
    assert stored_failed["detail"] == failed.detail
    assert raw_prompt not in str(stored)
    assert raw_prompt not in caplog.get_records("call")[-1].getMessage()
    assert service.errors(build.id)[-1]["safe_message"] == (
        "Provider failed after bounded retries."
    )
    assert "additionalProperties" not in service.errors(build.id)[-1]["safe_message"]


@pytest.mark.parametrize(
    ("retryable", "expected_step_status"),
    [(False, "failed"), (True, "failed_retryable")],
)
def test_live_provider_failure_is_never_automatically_retried(
    workflow_runtime, retryable: bool, expected_step_status: str
) -> None:
    database, _store, _providers, _harness, service = workflow_runtime

    class ClassifiedFailureProvider:
        name = "offline_fixture"
        calls = 0

        def run_turn(self, *_args, **_kwargs):
            self.calls += 1
            raise ProviderFailure(
                "Classified provider failure.",
                retryable=retryable,
                exception_class="SyntheticProviderError",
                http_status=429 if retryable else 400,
            )

    provider = ClassifiedFailureProvider()
    registry = ProviderRegistry()
    registry.register("openai", lambda: provider)
    service.harness = AgentHarness(database, registry, offline_test_tool_registry())
    service.agent_configuration = AgentConfiguration(
        provider="openai",
        run_mode=AgentRunMode.LIVE,
        api_key="test-only-placeholder",
    )
    created = service.create_build(
        EndpointBuildCreate(
            endpoint_name="Retryability test",
            endpoint_slug=f"retryability-{str(retryable).lower()}",
            biological_goal="Verify one authoritative provider retryability classification.",
            created_by="test-admin",
            idempotency_key=f"retryability-build-{retryable}",
        )
    )
    failed = service.start_build(
        created.id,
        expected_version=created.version,
        actor="test-admin",
        idempotency_key=f"retryability-start-{retryable}",
    )
    assert failed.current_stage is WorkflowState.FAILED
    assert provider.calls == 1
    error = service.errors(created.id)[-1]
    step = service.steps(created.id)[-1]
    assert error["retryable"] is retryable
    assert step["status"] == expected_step_status
    assert step["error_id"] == error["id"]
    run = service.agent_run(service.agent_runs(created.id)[-1]["id"])
    assert run["request"]["budget"]["retry_count"] == 0
    assert (
        sum(event["event_type"] == "provider.turn.started" for event in run["trace"]["events"]) == 1
    )
    assert not any(event["event_type"] == "provider.retry" for event in run["trace"]["events"])
    if not retryable:
        with pytest.raises(GuardNotSatisfied, match="not retryable"):
            service.retry_failed(
                created.id,
                expected_version=failed.version,
                actor="test-admin",
                idempotency_key="retryability-terminal-retry",
            )


def test_transient_provider_failure_retries(workflow_runtime) -> None:
    _build, _discovering, _step, request, harness, _service = harness_context(workflow_runtime)
    retrying = with_mode(request, "transient_failure").model_copy(
        update={"budget": request.budget.model_copy(update={"retry_count": 1})}
    )
    _run_id, result = harness.run(retrying, DiscoveryOutput)
    assert result.status is AgentRunStatus.COMPLETED
    assert any(event.event_type == "provider.retry" for event in result.trace)


def test_source_http_retries_do_not_create_extra_provider_invocations(workflow_runtime) -> None:
    database, _store, _providers, _harness, _service = workflow_runtime
    _build, _discovering, _step, request, _default, _service2 = harness_context(workflow_runtime)
    source_requests = 0

    def source_transport(request: httpx.Request) -> httpx.Response:
        nonlocal source_requests
        source_requests += 1
        if source_requests == 1:
            raise httpx.ReadTimeout("bounded transient timeout", request=request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"ok": True},
        )

    client = ScientificSourceClient(
        transport=httpx.MockTransport(source_transport),
        maximum_attempts=2,
        sleep=lambda _seconds: None,
    )
    tools = offline_test_tool_registry()

    def source_backed_tool(_request) -> EchoOutput:
        client.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi",
            accepted_types={"application/json"},
        )
        return EchoOutput(value="source-retrieved")

    tools.get("test_success").implementation = source_backed_tool

    class CountingProvider:
        name = "counting"

        def __init__(self) -> None:
            self.calls = 0

        def run_turn(self, *_args, **_kwargs) -> ProviderTurn:
            self.calls += 1
            if self.calls == 1:
                return ProviderTurn(
                    kind="tool",
                    tool_request=ProviderToolRequest(
                        tool_name="test_success",
                        arguments={"value": "retrieve"},
                        idempotency_key="source-retry-tool-call",
                    ),
                )
            return ProviderTurn(kind="output", output={"value": "done"})

    provider = CountingProvider()
    registry = ProviderRegistry()
    registry.register("counting", lambda: provider)
    harness = AgentHarness(database, registry, tools)
    source_request = request.model_copy(
        update={
            "model": request.model.model_copy(update={"provider": "counting"}),
            "output_schema_name": EchoOutput.__name__,
            "available_tools": ["test_success"],
            "context": {**request.context, "permission_scope": ["test:invoke"]},
            "budget": request.budget.model_copy(update={"retry_count": 0}),
        }
    )
    try:
        _run_id, result = harness.run(source_request, EchoOutput)
    finally:
        client.close()

    assert result.status is AgentRunStatus.COMPLETED
    assert source_requests == 2
    assert provider.calls == 2
    assert not any(event.event_type == "provider.retry" for event in result.trace)


def test_two_normal_model_turns_accept_3880_tokens_without_counting_a_retry(
    workflow_runtime,
) -> None:
    database, _store, _providers, _harness, _service = workflow_runtime
    _build, _discovering, _step, request, _default, _service2 = harness_context(workflow_runtime)

    class TwoTurnProvider:
        name = "counting"

        def __init__(self) -> None:
            self.calls = 0

        def run_turn(self, *_args, **_kwargs) -> ProviderTurn:
            self.calls += 1
            usage = UsageReport(input_tokens=1940, output_tokens=100, cost_cents=0.1)
            if self.calls == 1:
                return ProviderTurn(
                    kind="tool",
                    tool_request=ProviderToolRequest(
                        tool_name="test_success",
                        arguments={"value": "bounded"},
                        idempotency_key="two-normal-turns-tool",
                    ),
                    usage=usage,
                )
            return ProviderTurn(kind="output", output={"value": "done"}, usage=usage)

    provider = TwoTurnProvider()
    registry = ProviderRegistry()
    registry.register("counting", lambda: provider)
    harness = AgentHarness(database, registry, offline_test_tool_registry())
    bounded = request.model_copy(
        update={
            "model": request.model.model_copy(update={"provider": "counting"}),
            "output_schema_name": EchoOutput.__name__,
            "available_tools": ["test_success"],
            "context": {**request.context, "permission_scope": ["test:invoke"]},
            "budget": request.budget.model_copy(
                update={
                    "maximum_input_tokens": 8000,
                    "maximum_output_tokens": 1500,
                    "retry_count": 0,
                }
            ),
        }
    )
    _run_id, result = harness.run(bounded, EchoOutput)
    assert result.status is AgentRunStatus.COMPLETED
    assert result.turns == 2
    assert result.usage.input_tokens == 3880
    assert provider.calls == 2
    assert sum(event.event_type == "provider.turn.completed" for event in result.trace) == 2
    assert not any(event.event_type == "provider.retry" for event in result.trace)


def test_next_turn_budget_precheck_stops_before_an_unsafe_provider_call(
    workflow_runtime,
) -> None:
    database, _store, _providers, _harness, _service = workflow_runtime
    _build, _discovering, _step, request, _default, _service2 = harness_context(workflow_runtime)

    class ExpensiveProvider:
        name = "counting"
        calls = 0

        def run_turn(self, *_args, **_kwargs) -> ProviderTurn:
            self.calls += 1
            return ProviderTurn(
                kind="tool",
                tool_request=ProviderToolRequest(
                    tool_name="test_success",
                    arguments={"value": "bounded"},
                    idempotency_key="budget-precheck-tool",
                ),
                usage=UsageReport(input_tokens=4500),
            )

    provider = ExpensiveProvider()
    registry = ProviderRegistry()
    registry.register("counting", lambda: provider)
    harness = AgentHarness(database, registry, offline_test_tool_registry())
    bounded = request.model_copy(
        update={
            "model": request.model.model_copy(update={"provider": "counting"}),
            "output_schema_name": EchoOutput.__name__,
            "available_tools": ["test_success"],
            "context": {**request.context, "permission_scope": ["test:invoke"]},
            "budget": request.budget.model_copy(update={"maximum_input_tokens": 8000}),
        }
    )
    _run_id, result = harness.run(bounded, EchoOutput)
    assert result.status is AgentRunStatus.BUDGET_EXCEEDED
    assert result.error.code == "input_token_budget_precheck"
    assert result.error.safe_message == (
        "Live agent run stopped at the configured cumulative token budget."
    )
    assert provider.calls == 1


def test_cost_cap_is_enforced_after_each_model_turn(workflow_runtime) -> None:
    database, _store, _providers, _harness, _service = workflow_runtime
    _build, _discovering, _step, request, _default, _service2 = harness_context(workflow_runtime)

    class CostlyProvider:
        name = "costly"

        def run_turn(self, *_args, **_kwargs) -> ProviderTurn:
            return ProviderTurn(
                kind="output",
                output={"value": "done"},
                usage=UsageReport(cost_cents=20.01),
            )

    registry = ProviderRegistry()
    registry.register("costly", CostlyProvider)
    harness = AgentHarness(database, registry, offline_test_tool_registry())
    costly = request.model_copy(
        update={
            "model": request.model.model_copy(update={"provider": "costly"}),
            "output_schema_name": EchoOutput.__name__,
            "budget": request.budget.model_copy(update={"maximum_cost_cents": 20}),
        }
    )
    _run_id, result = harness.run(costly, EchoOutput)
    assert result.status is AgentRunStatus.BUDGET_EXCEEDED
    assert result.error.code == "cost_budget_exceeded"


def test_zero_first_geo_result_triggers_one_bounded_alternative_and_structured_output(
    workflow_runtime,
) -> None:
    database, store, _providers, _harness, _service = workflow_runtime
    _build, _discovering, _step, request, _default, _service2 = harness_context(workflow_runtime)
    source_requests = 0

    def no_results(source_request: httpx.Request) -> httpx.Response:
        nonlocal source_requests
        source_requests += 1
        return httpx.Response(
            200,
            json={"esearchresult": {"idlist": []}},
            headers={"content-type": "application/json"},
            request=source_request,
        )

    source_client = ScientificSourceClient(
        transport=httpx.MockTransport(no_results), sleep=lambda _seconds: None
    )
    discovery_tools = DiscoveryToolService(SourceResponseCache(database), store, source_client)

    class ZeroResultProvider:
        name = "scripted"

        def __init__(self) -> None:
            self.calls = 0

        def run_turn(self, *_args, **_kwargs) -> ProviderTurn:
            self.calls += 1
            if self.calls <= 2:
                study_types = ["sequencing"] if self.calls == 1 else ["array"]
                return ProviderTurn(
                    kind="tool",
                    tool_request=ProviderToolRequest(
                        tool_name="search_geo_series",
                        arguments={
                            "scientific_terms": ["oxidative stress"],
                            "organism_alternatives": ["Homo sapiens"],
                            "study_type_alternatives": study_types,
                            "cell_tissue_terms": [],
                            "treatment_terms": [],
                            "maximum_results": 5,
                            "publication_date_start": None,
                            "publication_date_end": None,
                            "strategy_reason": (
                                "Focused sequencing search."
                                if self.calls == 1
                                else "Alternative array search after zero results."
                            ),
                        },
                        idempotency_key=f"zero-search-{self.calls}",
                    ),
                )
            return ProviderTurn(
                kind="output",
                output={
                    "endpoint_name": "Oxidative stress",
                    "endpoint_definition_summary": "Transcriptomic oxidative-stress response.",
                    "run_mode": "live",
                    "offline_fixture_label": None,
                    "live_discovery": True,
                    "search_strategy": "Two focused searches returned zero GEO Series.",
                    "queries_executed": ["human sequencing", "human array"],
                    "search_strategy_steps": [],
                    "candidates": [],
                    "recommended_candidate_id": None,
                    "recommendation": "No dataset recommendation.",
                    "decision_summary": "Review the bounded no-candidate outcome.",
                    "rejected_candidates": [],
                    "unresolved_questions": ["Should one bounded filter be revised?"],
                    "requires_human_review": True,
                    "evidence_references": [],
                    "limitations": ["No candidate found under the current strategy."],
                    "confidence_category": "low",
                },
            )

    provider = ZeroResultProvider()
    registry = ProviderRegistry()
    registry.register("scripted", lambda: provider)
    harness = AgentHarness(database, registry, production_tool_registry(REPO_ROOT, discovery_tools))
    bounded = request.model_copy(
        update={
            "model": request.model.model_copy(update={"provider": "scripted"}),
            "instruction_version": "zero-result-bounded-search-test",
            "available_tools": ["search_geo_series"],
            "context": {
                **request.context,
                "run_mode": "live",
                "permission_scope": ["source:geo:read"],
            },
        }
    )
    try:
        run_id, result = harness.run(bounded, DiscoveryOutput)
    finally:
        source_client.close()
    assert result.status is AgentRunStatus.COMPLETED
    assert result.tool_calls == 2
    assert result.turns == 3
    assert result.output["candidates"] == []
    assert result.output["recommended_candidate_id"] is None
    assert provider.calls == 3
    assert source_requests == 2


def test_one_geo_parser_failure_does_not_terminally_fail_discovery(
    workflow_runtime,
) -> None:
    database, store, _providers, _harness, _service = workflow_runtime
    _build, _discovering, _step, request, _default, _service2 = harness_context(workflow_runtime)
    accessions = ["GSE330744", "GSE338134", "GSE314573", "GSE314406", "GSE301422"]

    def geo(request: httpx.Request) -> httpx.Response:
        accession = str(request.url.params.get("acc"))
        if accession == "GSE314406":
            body = "<html><form>GEO accession search</form></html>"
        else:
            body = "\n".join(
                [
                    f"^SERIES = {accession}",
                    f"!Series_geo_accession = {accession}",
                    "!Series_status = Public on Jul 18 2026",
                    f"!Series_title = Official fixture {accession}",
                    "!Series_type = Expression profiling by high throughput sequencing",
                    "!Series_sample_organism_ch1 = Homo sapiens",
                ]
            )
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/plain"},
            request=request,
        )

    source_client = ScientificSourceClient(
        transport=httpx.MockTransport(geo), sleep=lambda _seconds: None
    )
    discovery_tools = DiscoveryToolService(SourceResponseCache(database), store, source_client)

    class CandidateIsolationProvider:
        name = "candidate-isolation"

        def __init__(self) -> None:
            self.calls = 0

        def run_turn(self, *_args, **_kwargs) -> ProviderTurn:
            self.calls += 1
            if self.calls == 1:
                return ProviderTurn(
                    kind="tool",
                    tool_request=ProviderToolRequest(
                        tool_name="validate_geo_accessions",
                        arguments={"accessions": accessions},
                        idempotency_key="candidate-isolation-batch",
                    ),
                )
            return ProviderTurn(
                kind="output",
                output={
                    "endpoint_name": "Oxidative stress",
                    "endpoint_definition_summary": "Transcriptomic oxidative-stress response.",
                    "run_mode": "live",
                    "offline_fixture_label": None,
                    "live_discovery": True,
                    "search_strategy": "Validate the bounded candidate set.",
                    "queries_executed": ["prior official GEO search"],
                    "search_strategy_steps": [],
                    "candidates": [
                        {
                            "candidate_id": "candidate-GSE330744",
                            "accession": "GSE330744",
                            "title": "Official fixture GSE330744",
                            "source": "NCBI GEO",
                            "organism": ["Homo sapiens"],
                            "data_type": "Transcriptomic series",
                            "sample_count": 0,
                            "biological_context": "Metadata review required.",
                            "treatment_control_evidence": "Not yet inspected.",
                            "dose_time_evidence": "Not yet inspected.",
                            "strengths": ["Public GEO record"],
                            "limitations": ["Sample design not yet inspected."],
                            "exclusion_reasons": [],
                            "recommendation_status": "recommended_for_human_review",
                            "evidence_references": [],
                            "accession_verified": True,
                            "license_verified": False,
                            "geo_validation_status": "public_valid",
                        }
                    ],
                    "recommended_candidate_id": "candidate-GSE330744",
                    "recommendation": "Review the verified public candidate.",
                    "decision_summary": "One parser failure did not erase valid candidates.",
                    "rejected_candidates": [
                        {
                            "accession": "GSE314406",
                            "reason": "Unexpected source format.",
                        }
                    ],
                    "unresolved_questions": ["Is the sample design suitable?"],
                    "requires_human_review": True,
                    "evidence_references": [],
                    "limitations": ["Detailed metadata was intentionally deferred."],
                    "confidence_category": "moderate",
                },
            )

    provider = CandidateIsolationProvider()
    providers = ProviderRegistry()
    providers.register("candidate-isolation", lambda: provider)
    harness = AgentHarness(
        database,
        providers,
        production_tool_registry(REPO_ROOT, discovery_tools),
    )
    bounded = request.model_copy(
        update={
            "model": request.model.model_copy(update={"provider": "candidate-isolation"}),
            "available_tools": ["validate_geo_accessions"],
            "context": {
                **request.context,
                "run_mode": "live",
                "permission_scope": ["source:geo:read"],
            },
        }
    )
    try:
        run_id, result = harness.run(bounded, DiscoveryOutput)
    finally:
        source_client.close()
    assert result.status is AgentRunStatus.COMPLETED
    assert result.turns == 2
    assert result.tool_calls == 1
    stored = _service.agent_run(run_id)
    statuses = [item["status"] for item in stored["tool_calls"][0]["result"]["output"]["results"]]
    assert statuses == [
        "public_valid",
        "public_valid",
        "public_valid",
        "unexpected_source_format",
        "public_valid",
    ]
    assert result.output["recommended_candidate_id"] == "candidate-GSE330744"


def test_optional_geo_term_normalization_executes_in_same_run_without_retry(
    workflow_runtime,
) -> None:
    database, _store, _providers, _harness, service = workflow_runtime
    build, _discovering, _step, request, _default, _service = harness_context(workflow_runtime)
    supplied_arguments = {
        "cell_tissue_terms": [],
        "maximum_results": 5,
        "organism_alternatives": ["Homo sapiens", "Mus musculus"],
        "publication_date_end": None,
        "publication_date_start": None,
        "scientific_terms": ["oxidative stress", "transcriptomic"],
        "strategy_reason": (
            "Find public GEO Series with transcriptomic measurements relevant to oxidative "
            "stress in human or mouse."
        ),
        "study_type_alternatives": [
            "expression profiling by array",
            "high throughput sequencing",
        ],
        "treatment_terms": [],
    }
    executed_arguments: list[dict] = []

    class BoundaryDiscoveryService:
        def search_geo_series(self, typed_request, _invocation):
            values = typed_request.model_dump(mode="json")
            executed_arguments.append(values)
            return {
                "typed_request": values,
                "rendered_query": "mocked bounded GEO query",
                "normalized_query": "mocked bounded geo query",
                "strategy_reason": typed_request.strategy_reason,
                "results": [],
                "result_count": 0,
                "new_accession_count": 0,
                "retrieval_timestamp": datetime.now(UTC),
                "source_artifact_id": "art-mocked-normalized-search",
                "cache_status": "cached",
            }

        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: {}

    class NormalizationProvider:
        name = "scripted"

        def __init__(self) -> None:
            self.calls = 0

        def run_turn(self, *_args, **_kwargs) -> ProviderTurn:
            self.calls += 1
            if self.calls == 1:
                return ProviderTurn(
                    kind="tool",
                    tool_request=ProviderToolRequest(
                        tool_name="search_geo_series",
                        arguments=supplied_arguments,
                        idempotency_key="normalized-live-search",
                    ),
                )
            return ProviderTurn(
                kind="output",
                output={
                    "endpoint_name": "Oxidative stress",
                    "endpoint_definition_summary": "Transcriptomic oxidative-stress response.",
                    "run_mode": "live",
                    "offline_fixture_label": None,
                    "live_discovery": True,
                    "search_strategy": "One safely normalized bounded search.",
                    "queries_executed": ["mocked bounded GEO query"],
                    "search_strategy_steps": [],
                    "candidates": [],
                    "recommended_candidate_id": None,
                    "recommendation": "No dataset recommendation from the mocked boundary.",
                    "decision_summary": "Normalization preserved the intended search scope.",
                    "rejected_candidates": [],
                    "unresolved_questions": [],
                    "requires_human_review": True,
                    "evidence_references": [],
                    "limitations": ["Mocked GEO execution boundary only."],
                    "confidence_category": "low",
                },
            )

    provider = NormalizationProvider()
    providers = ProviderRegistry()
    providers.register("scripted", lambda: provider)
    harness = AgentHarness(
        database,
        providers,
        production_tool_registry(REPO_ROOT, BoundaryDiscoveryService()),
    )
    bounded = request.model_copy(
        update={
            "model": request.model.model_copy(update={"provider": "scripted"}),
            "instruction_version": "optional-search-term-normalization-test",
            "available_tools": ["search_geo_series"],
            "budget": request.budget.model_copy(update={"retry_count": 0}),
            "context": {
                **request.context,
                "run_mode": "live",
                "permission_scope": ["source:geo:read"],
            },
        }
    )

    run_id, result = harness.run(bounded, DiscoveryOutput)

    assert result.status is AgentRunStatus.COMPLETED
    assert result.turns == 2
    assert result.tool_calls == 1
    assert provider.calls == 2
    assert len(service.agent_runs(build.id)) == 1
    canonical_arguments = {
        **supplied_arguments,
        "study_type_alternatives": [
            "Expression profiling by array",
            "Expression profiling by high throughput sequencing",
        ],
    }
    assert executed_arguments == [canonical_arguments]
    assert not any(event.event_type == "provider.retry" for event in result.trace)
    completed = next(event for event in result.trace if event.event_type == "tool_call.completed")
    assert completed.detail["model_supplied_arguments"] == supplied_arguments
    assert completed.detail["normalized_execution_arguments"] == {
        **canonical_arguments,
    }
    assert completed.detail["normalization_warning_count"] == 2
    assert completed.detail["normalization_warning_codes"] == [
        "controlled_vocabulary_alias_canonicalized",
        "controlled_vocabulary_alias_canonicalized",
    ]
    stored = service.agent_run(run_id)
    call = stored["tool_calls"][0]
    assert call["arguments"]["original_arguments"] == supplied_arguments
    assert call["arguments"]["normalized_arguments"] == {
        **canonical_arguments,
    }
    assert call["arguments"]["normalization_warnings"] == [
        {
            "schema_version": "1.0.0",
            "code": "controlled_vocabulary_alias_canonicalized",
            "field": "study_type_alternatives",
            "original_index": 0,
            "original": "expression profiling by array",
            "normalized": "Expression profiling by array",
            "policy_version": "controlled-vocabulary-v1",
        },
        {
            "schema_version": "1.0.0",
            "code": "controlled_vocabulary_alias_canonicalized",
            "field": "study_type_alternatives",
            "original_index": 1,
            "original": "high throughput sequencing",
            "normalized": "Expression profiling by high throughput sequencing",
            "policy_version": "controlled-vocabulary-v1",
        },
    ]


def test_terminal_source_failure_persists_safe_diagnostic_and_failed_trace(
    workflow_runtime, caplog
) -> None:
    database, store, _providers, _harness, service = workflow_runtime

    def prohibited_redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://example.com/private?secret=do-not-store"},
            request=request,
        )

    source_client = ScientificSourceClient(
        transport=httpx.MockTransport(prohibited_redirect), sleep=lambda _seconds: None
    )
    discovery_tools = DiscoveryToolService(SourceResponseCache(database), store, source_client)

    class TerminalSourceProvider:
        name = "terminal-source-test"

        def __init__(self) -> None:
            self.calls = 0

        def run_turn(self, *_args, **_kwargs) -> ProviderTurn:
            self.calls += 1
            return ProviderTurn(
                kind="tool",
                tool_request=ProviderToolRequest(
                    tool_name="validate_geo_accessions",
                    arguments={"accessions": ["GSE314406"]},
                    idempotency_key="terminal-source-call",
                ),
            )

    provider = TerminalSourceProvider()
    providers = ProviderRegistry()
    providers.register("terminal-source-test", lambda: provider)
    real_harness = AgentHarness(
        database,
        providers,
        production_tool_registry(REPO_ROOT, discovery_tools),
    )

    class RequestOverrideHarness:
        def run(self, request, output_schema):
            return real_harness.run(
                request.model_copy(
                    update={
                        "model": request.model.model_copy(
                            update={"provider": "terminal-source-test"}
                        ),
                        "context": {
                            **request.context,
                            "run_mode": "live",
                            "permission_scope": ["source:geo:read"],
                        },
                        "available_tools": ["validate_geo_accessions"],
                    }
                ),
                output_schema,
            )

    service.harness = RequestOverrideHarness()
    created = service.create_build(
        EndpointBuildCreate(
            endpoint_name="Terminal source diagnostic",
            endpoint_slug="terminal-source-diagnostic",
            biological_goal="Verify safe source diagnostics on a terminal redirect failure.",
            created_by="test-admin",
            idempotency_key="terminal-source-build",
        )
    )
    caplog.set_level(logging.WARNING, logger="uvicorn.error.endoscan.workflow.source_tool")
    try:
        finished = service.start_build(
            created.id,
            expected_version=created.version,
            actor="test-admin",
            idempotency_key="terminal-source-start",
        )
    finally:
        source_client.close()

    assert finished.current_stage is WorkflowState.FAILED
    assert provider.calls == 1
    run = service.agent_run(service.agent_runs(created.id)[0]["id"])
    assert run["status"] == "failed"
    tool_result = run["tool_calls"][0]["result"]
    assert tool_result["source_diagnostic"] is not None, tool_result
    diagnostic = tool_result["source_diagnostic"]
    assert diagnostic["source_host"] == "www.ncbi.nlm.nih.gov"
    assert diagnostic["safe_url_path"] == "/geo/query/acc.cgi"
    assert diagnostic["http_status"] == 302
    assert diagnostic["source_error_category"] == "redirect_not_approved"
    errors = service.errors(created.id)
    assert errors[0]["detail"]["source_diagnostic"] == diagnostic
    trace_artifact = next(
        item for item in store.list_artifacts(created.id) if item.artifact_type == "search_trace"
    )
    _descriptor, raw_trace = store.get(trace_artifact.id)
    trace = json.loads(raw_trace)
    assert trace["status"] == "failed"
    assert trace["tool_calls"][0]["source_diagnostic"] == diagnostic
    persisted = json.dumps({"run": run, "errors": errors, "trace": trace})
    assert "do-not-store" not in persisted
    assert "example.com/private" not in persisted
    assert "redirect_not_approved" in caplog.text
    assert "do-not-store" not in caplog.text


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
                "offline_fixture_mode": "approval",
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


def test_latest_live_trace_regression_completes_offline_with_stage_specific_tools(
    workflow_runtime,
) -> None:
    database, _store, _providers, _harness, service = workflow_runtime
    accessions = ["GSE330744", "GSE338134", "GSE314573", "GSE314406", "GSE301422"]

    class OfflineDiscoveryTools:
        def search_geo_series(self, request, _invocation):
            return {
                "typed_request": request.model_dump(mode="json"),
                "rendered_query": "offline oxidative-stress GEO fixture query",
                "normalized_query": "offline oxidative-stress geo fixture query",
                "strategy_reason": request.strategy_reason,
                "results": [
                    {
                        "accession": accession,
                        "title": f"Offline verified title for {accession}",
                        "organism": "Homo sapiens",
                        "study_type": "Expression profiling by high throughput sequencing",
                        "sample_count": 12,
                    }
                    for accession in accessions
                ],
                "result_count": 5,
                "new_accession_count": 5,
                "retrieval_timestamp": datetime.now(UTC),
                "source_artifact_id": "offline-search-artifact",
                "cache_status": "cached",
                "search_executed": True,
                "stop_reason": None,
            }

        def validate_geo_accessions(self, request, _invocation):
            return {
                "results": [
                    {
                        "accession": accession,
                        "status": "public_valid",
                        "exists_in_geo_index": True,
                        "public_record_available": True,
                        "title": f"Offline verified title for {accession}",
                        "organism": ["Homo sapiens"],
                        "study_type": ["Expression profiling by high throughput sequencing"],
                        "source_reference": f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={accession}",
                        "source_artifact_id": f"source-{accession}",
                        "evidence_references": [f"artifact:source-{accession}#Series_title"],
                        "validation_timestamp": datetime.now(UTC),
                        "retryable": False,
                        "cache_status": "cached",
                    }
                    for accession in request.accessions
                ],
                "public_valid_count": len(request.accessions),
                "unavailable_count": 0,
                "invalid_count": 0,
                "source_artifact_references": [
                    f"artifact:source-{accession}" for accession in request.accessions
                ],
                "cache_states": ["cached" for _ in request.accessions],
            }

        def inspect_geo_candidates(self, request, _invocation):
            return {
                "results": [
                    {
                        "accession": accession,
                        "status": "inspected",
                        "verified_title": f"Offline verified title for {accession}",
                        "organism": ["Homo sapiens"],
                        "study_type": ["Expression profiling by high throughput sequencing"],
                        "sample_count": 12,
                        "biological_context": "Bounded offline candidate context.",
                        "cell_lines_or_tissues": ["cultured cells"],
                        "treatment_groups": ["treated"],
                        "likely_control_groups": ["vehicle control"],
                        "replicate_information": {"treated": 3, "vehicle control": 3},
                        "dose_metadata": ["dose recorded"],
                        "time_metadata": ["24 h"],
                        "linked_publication_ids": [],
                        "metadata_completeness": "high",
                        "explicit_uncertainties": ["Scientific suitability requires review."],
                        "source_artifact_references": [f"artifact:source-{accession}"],
                        "evidence_references": [f"artifact:source-{accession}#Sample_records"],
                        "cache_status": "cached",
                    }
                    for accession in request.accessions
                ],
                "inspected_count": len(request.accessions),
                "failed_count": 0,
                "evidence_references": [
                    f"artifact:source-{accession}#Sample_records"
                    for accession in request.accessions
                ],
            }

        @staticmethod
        def compare_dataset_candidates(request, _invocation):
            return {
                "candidates": [
                    candidate.model_dump(mode="json") for candidate in request.candidates
                ],
                "comparison_fields": [
                    "sample_count",
                    "organism",
                    "biological_context",
                    "treatment_evidence",
                    "control_evidence",
                    "dose_time_evidence",
                ],
            }

        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: {}

    class LatestTraceProvider:
        name = "openai"

        def __init__(self) -> None:
            self.calls = 0
            self.exposures: list[list[str]] = []

        def estimate_context_components(self, _request, history):
            return {"bounded_context": 4218 if len(history) == 2 else 1200}

        def run_turn(self, request, _history, **_kwargs):
            self.calls += 1
            self.exposures.append(list(request.available_tools))
            input_tokens = [3350, 3353, 4218, 1200, 1000][self.calls - 1]
            usage = UsageReport(
                input_tokens=input_tokens,
                output_tokens=80,
                cached_tokens=0,
                cost_cents=0.1,
                provider_request_ids=[f"offline-request-{self.calls}"],
            )
            if self.calls == 1:
                return ProviderTurn(
                    kind="tool",
                    tool_request=ProviderToolRequest(
                        tool_name="search_geo_series",
                        arguments={
                            "scientific_terms": ["oxidative stress", "transcriptomic"],
                            "organism_alternatives": ["Homo sapiens", "Mus musculus"],
                            "study_type_alternatives": ["sequencing"],
                            "cell_tissue_terms": [],
                            "treatment_terms": [],
                            "maximum_results": 5,
                            "publication_date_start": None,
                            "publication_date_end": None,
                            "strategy_reason": "Replay the latest bounded live search offline.",
                        },
                        idempotency_key="offline-search",
                    ),
                    usage=usage,
                )
            if self.calls == 2:
                return ProviderTurn(
                    kind="tool",
                    tool_request=ProviderToolRequest(
                        tool_name="validate_geo_accessions",
                        arguments={"accessions": accessions},
                        idempotency_key="offline-validation",
                    ),
                    usage=usage,
                )
            if self.calls == 3:
                return ProviderTurn(
                    kind="tool",
                    tool_request=ProviderToolRequest(
                        tool_name="inspect_geo_candidates",
                        arguments={"accessions": accessions},
                        idempotency_key="offline-inspection",
                    ),
                    usage=usage,
                )
            if self.calls == 4:
                candidates = [
                    {
                        "accession": accession,
                        "title": f"Offline verified title for {accession}",
                        "organism": ["Homo sapiens"],
                        "sample_count": 12,
                        "biological_context": "Bounded offline candidate context.",
                        "likely_treatment_groups": ["treated"],
                        "likely_control_groups": ["vehicle control"],
                        "dose_time_evidence": "Dose and time recorded.",
                        "source_artifact_ids": [f"source-{accession}"],
                    }
                    for accession in accessions
                ]
                return ProviderTurn(
                    kind="tool",
                    tool_request=ProviderToolRequest(
                        tool_name="compare_dataset_candidates",
                        arguments={"candidates": candidates},
                        idempotency_key="offline-comparison",
                    ),
                    usage=usage,
                )
            candidates = [
                {
                    "candidate_id": f"candidate-{accession}",
                    "accession": accession,
                    "title": f"Offline verified title for {accession}",
                    "source": f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={accession}",
                    "organism": ["Homo sapiens"],
                    "data_type": "Transcriptomic GEO Series",
                    "sample_count": 12,
                    "biological_context": "Bounded offline candidate context.",
                    "treatment_control_evidence": "Treatment and control labels require review.",
                    "dose_time_evidence": (
                        "Dose and time were present but suitability was unresolved."
                    ),
                    "strengths": ["Official public GEO record."],
                    "limitations": ["Scientific suitability was not established by the fixture."],
                    "exclusion_reasons": ["No candidate was preferred in this bounded fixture."],
                    "recommendation_status": "reject",
                    "evidence_references": [
                        {
                            "source_artifact_id": f"source-{accession}",
                            "field_path": "Sample_records",
                            "supports_claim": "Treatment/control metadata summary.",
                        }
                    ],
                    "accession_verified": True,
                    "license_verified": False,
                    "geo_validation_status": "public_valid",
                }
                for accession in accessions
            ]
            return ProviderTurn(
                kind="output",
                output={
                    "endpoint_name": "Oxidative stress",
                    "endpoint_definition_summary": "Response-defined oxidative-stress endpoint.",
                    "run_mode": "live",
                    "live_discovery": True,
                    "search_strategy": "One bounded offline regression search.",
                    "queries_executed": ["offline oxidative-stress GEO fixture query"],
                    "search_strategy_steps": [],
                    "candidates": candidates,
                    "recommended_candidate_id": None,
                    "recommendation": "No suitable dataset identified in the bounded fixture.",
                    "decision_summary": "Five public records were inspected without selecting one.",
                    "rejected_candidates": [
                        {"accession": accession, "reason": "Requires revised scientific search."}
                        for accession in accessions
                    ],
                    "unresolved_questions": [
                        "Which perturbation design should the revised search target?"
                    ],
                    "proposed_next_search_strategy": (
                        "Target direct oxidant perturbations with matched controls."
                    ),
                    "requires_human_review": True,
                    "evidence_references": [],
                    "limitations": ["Offline deterministic regression; no live source request."],
                    "confidence_category": "low",
                },
                usage=usage,
            )

    provider = LatestTraceProvider()
    providers = ProviderRegistry()
    providers.register("openai", lambda: provider)
    service.harness = AgentHarness(
        database,
        providers,
        production_tool_registry(REPO_ROOT, OfflineDiscoveryTools()),
    )
    service.agent_configuration = AgentConfiguration(
        provider="openai",
        run_mode=AgentRunMode.LIVE,
        api_key=SecretStr("offline-placeholder-never-used"),
    )
    created = service.create_build(
        EndpointBuildCreate(
            endpoint_name="Oxidative stress",
            endpoint_slug="latest-live-trace-offline-regression",
            biological_goal="Evaluate the latest live trace without external requests.",
            created_by="test-admin",
            idempotency_key="latest-live-trace-offline-regression-build",
        )
    )
    finished = service.start_build(
        created.id,
        expected_version=0,
        actor="test-admin",
        idempotency_key="latest-live-trace-offline-regression-start",
    )

    assert finished.current_stage is WorkflowState.AWAITING_SEARCH_REVIEW
    assert len(service.agent_runs(created.id)) == 1
    run = service.agent_run(service.agent_runs(created.id)[0]["id"])
    assert run["status"] == "completed"
    assert run["turns"] == 5
    assert len(run["tool_calls"]) == 4
    assert run["usage"]["input_tokens"] == 13121
    assert provider.calls == 5
    assert provider.exposures == [
        ["search_geo_series"],
        ["validate_geo_accessions"],
        ["inspect_geo_candidates"],
        ["compare_dataset_candidates"],
        [],
    ]
    assert not any(event["event_type"] == "provider.retry" for event in run["trace"]["events"])
    context_events = [
        event
        for event in run["trace"]["events"]
        if event["event_type"] == "agent_run.context_precheck"
    ]
    assert context_events[2]["detail"]["cumulative_input_tokens"] == 6703
    assert context_events[2]["detail"]["estimated_next_turn_input_tokens"] == 4218
    assert context_events[2]["detail"]["remaining_input_tokens"] == 13297
    approvals = service.list_approvals(created.id)
    assert not any(item["approval_type"] == "dataset_selection" for item in approvals)
    assert any(item["approval_type"] == "search_revision" for item in approvals)
