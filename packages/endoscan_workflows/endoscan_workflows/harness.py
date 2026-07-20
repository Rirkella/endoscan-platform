"""Controlled provider runtime: budgets, typed tools, traces, retries, and persistence."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from .contracts import (
    AgentRunRequest,
    AgentRunResult,
    AgentRunStatus,
    NormalizedAgentError,
    ProviderToolRequest,
    ToolCallStatus,
    ToolInvocation,
    ToolNormalizationWarning,
    ToolResult,
    TraceEvent,
    UsageReport,
    WorkflowState,
)
from .database import WorkflowDatabase
from .errors import WorkflowConflict, WorkflowNotFound
from .models import AgentRunRow, ToolCallRow, WorkflowErrorRow, WorkflowStepRow
from .providers import ProviderFailure, ProviderRegistry, ProviderTimeout
from .repository import (
    append_event,
    canonical_json,
    deterministic_id,
    load_versioned_json,
    require_build,
    utc_text,
    versioned_payload,
)
from .tools import ToolRegistry

SECRET_KEYS = {"authorization", "api_key", "apikey", "password", "secret", "token"}
logger = logging.getLogger("uvicorn.error.endoscan.workflow.provider")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in SECRET_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class AgentHarness:
    def __init__(
        self,
        database: WorkflowDatabase,
        providers: ProviderRegistry,
        tools: ToolRegistry,
    ):
        self.database = database
        self.providers = providers
        self.tools = tools

    def run(
        self,
        request: AgentRunRequest,
        output_model: type[BaseModel],
        *,
        trace_callback: Callable[[TraceEvent], None] | None = None,
        tool_result_callback: Callable[[str, str, ToolResult], None] | None = None,
        interruption_request: Callable[[], bool] | None = None,
    ) -> tuple[str, AgentRunResult]:
        started = time.monotonic()
        interruption_request = interruption_request or (lambda: False)
        request_payload = request.model_dump(mode="json")
        instructions = str(request_payload.pop("instructions", ""))
        request_payload["instructions_sha256"] = hashlib.sha256(instructions.encode()).hexdigest()
        safe_request = redact(request_payload)
        input_hash = hashlib.sha256(canonical_json(safe_request).encode()).hexdigest()
        run_id = deterministic_id("run", request.workflow_id, request.step_id, input_hash)
        prior = self._start_run(run_id, request, safe_request, input_hash)
        if prior is not None:
            return run_id, prior

        trace: list[TraceEvent] = []
        usage = UsageReport()
        history: list[dict] = []
        turns = 0
        tool_calls = 0
        validation_failures = 0
        provider_invocations = 0
        scientific_source_requests = 0
        retries = 0
        last_turn_usage: UsageReport | None = None
        provider = self.providers.create(request.model.provider)

        def emit(event_type: str, status: str = "", **detail: Any) -> None:
            event = TraceEvent(
                sequence=len(trace) + 1,
                event_type=event_type,
                workflow_id=request.workflow_id,
                step_id=request.step_id,
                agent_run_id=run_id,
                status=status,
                detail=redact(detail),
            )
            trace.append(event)
            if trace_callback:
                trace_callback(event)

        def log_provider_failure(detail: dict[str, Any]) -> None:
            logger.warning(
                "provider_failure workflow_id=%s agent_run_id=%s provider=%s model=%s "
                "exception_class=%s http_status=%s provider_error_code=%s "
                "provider_error_type=%s provider_request_id=%s provider_parameter=%s "
                "sdk_version=%s adapter_operation=%s developer_message=%s "
                "retryable=%s attempt=%s",
                request.workflow_id,
                run_id,
                request.model.provider,
                request.model.model_identifier,
                detail.get("exception_class"),
                detail.get("http_status"),
                detail.get("provider_error_code"),
                detail.get("provider_error_type"),
                detail.get("provider_request_id"),
                detail.get("provider_parameter"),
                detail.get("sdk_version"),
                detail.get("adapter_operation"),
                detail.get("developer_message"),
                detail["retryable"],
                detail["turn"],
            )

        emit("agent_run.started", "running", provider=provider.name)
        result: AgentRunResult | None = None
        while result is None:
            elapsed = time.monotonic() - started
            if elapsed >= request.budget.timeout_seconds:
                result = self._failure(
                    AgentRunStatus.TIMED_OUT,
                    "agent_timeout",
                    "Agent run exceeded its configured timeout.",
                    trace,
                    usage,
                    turns,
                    tool_calls,
                    started,
                )
                break
            if turns >= request.budget.maximum_turns:
                result = self._failure(
                    AgentRunStatus.BUDGET_EXCEEDED,
                    "maximum_turns_exceeded",
                    "Agent run exceeded its maximum turn count.",
                    trace,
                    usage,
                    turns,
                    tool_calls,
                    started,
                )
                break
            orchestrated = self._next_orchestrated_discovery_tool(request, history)
            if orchestrated is not None:
                if tool_calls >= request.budget.maximum_tool_calls:
                    result = self._failure(
                        AgentRunStatus.BUDGET_EXCEEDED,
                        "maximum_tool_calls_exceeded",
                        "Agent run exceeded its maximum tool-call count.",
                        trace,
                        usage,
                        turns,
                        tool_calls,
                        started,
                    )
                    break
                if orchestrated.tool_name not in request.available_tools:
                    result = self._failure(
                        AgentRunStatus.FAILED,
                        "orchestration_tool_not_allowed",
                        "A deterministic discovery tool was outside the bounded role inventory.",
                        trace,
                        usage,
                        turns,
                        tool_calls,
                        started,
                        category="policy",
                    )
                    break
                estimated_source_requests = self._estimated_scientific_source_requests(orchestrated)
                if scientific_source_requests + estimated_source_requests > int(
                    request.context.get(
                        "remaining_global_scientific_source_requests",
                        1_000_000,
                    )
                ):
                    result = self._failure(
                        AgentRunStatus.BUDGET_EXCEEDED,
                        "global_scientific_source_request_budget_exceeded",
                        "The global scientific-source request budget is exhausted.",
                        trace,
                        usage,
                        turns,
                        tool_calls,
                        started,
                    )
                    break
                tool_calls += 1
                call_id, tool_result = self._invoke_tool(
                    run_id,
                    request,
                    orchestrated,
                    tool_calls,
                )
                if (
                    tool_result_callback is not None
                    and tool_result.status is ToolCallStatus.COMPLETED
                ):
                    try:
                        tool_result_callback(
                            call_id,
                            orchestrated.tool_name,
                            tool_result,
                        )
                    except Exception as exc:
                        emit(
                            "tool_observation.persistence_failed",
                            "failed",
                            tool_call_id=call_id,
                            tool_name=orchestrated.tool_name,
                            exception_class=type(exc).__name__,
                            orchestration_generated=True,
                        )
                        result = self._failure(
                            AgentRunStatus.FAILED,
                            "tool_observation_persistence_failed",
                            "A verified source observation could not be persisted safely.",
                            trace,
                            usage,
                            turns,
                            tool_calls,
                            started,
                        )
                        break
                if isinstance(tool_result.output, dict):
                    scientific_source_requests += int(
                        tool_result.output.get("source_request_count", 0) or 0
                    )
                emit(
                    "tool_call.completed",
                    tool_result.status.value,
                    tool_name=orchestrated.tool_name,
                    discovery_substage="deterministic_candidate_hydration",
                    tool_requested=orchestrated.tool_name,
                    tool_executed=orchestrated.tool_name,
                    tool_call_id=call_id,
                    replayed=tool_result.replayed,
                    orchestration_generated=True,
                    model_supplied_arguments=None,
                    normalized_execution_arguments=tool_result.normalized_arguments,
                    source_diagnostic=(
                        tool_result.source_diagnostic.model_dump(mode="json")
                        if tool_result.source_diagnostic
                        else None
                    ),
                    tool_diagnostic=(
                        tool_result.error.tool_diagnostic.model_dump(mode="json")
                        if tool_result.error and tool_result.error.tool_diagnostic
                        else None
                    ),
                )
                history.append(
                    {
                        "tool_name": orchestrated.tool_name,
                        "status": tool_result.status.value,
                        "original_arguments": tool_result.original_arguments,
                        "normalized_arguments": tool_result.normalized_arguments,
                        "output": tool_result.output,
                        "error": (
                            tool_result.error.model_dump(mode="json") if tool_result.error else None
                        ),
                        "source_diagnostic": (
                            tool_result.source_diagnostic.model_dump(mode="json")
                            if tool_result.source_diagnostic
                            else None
                        ),
                        "discovery_substage": "deterministic_candidate_hydration",
                        "orchestration_generated": True,
                    }
                )
                if tool_result.status is not ToolCallStatus.COMPLETED:
                    invalid_input = bool(
                        tool_result.error and tool_result.error.code == "tool_input_invalid"
                    )
                    result = self._failure(
                        AgentRunStatus.FAILED,
                        (
                            "orchestration_tool_input_invalid"
                            if invalid_input
                            else tool_result.error.code
                            if tool_result.error
                            else "tool_failure"
                        ),
                        (
                            "Deterministic orchestration generated invalid typed tool input."
                            if invalid_input
                            else tool_result.error.safe_message
                            if tool_result.error
                            else "Tool failed safely."
                        ),
                        trace,
                        usage,
                        turns,
                        tool_calls,
                        started,
                        retryable=False,
                        category=(
                            "orchestration_validation"
                            if invalid_input
                            else tool_result.error.category
                            if tool_result.error
                            else "agent_runtime"
                        ),
                    )
                continue
            discovery_substage, exposed_tools = self._discovery_turn_policy(request, history)
            turn_request = request.model_copy(
                update={
                    "available_tools": exposed_tools,
                    "context": self._stage_scoped_turn_context(
                        request,
                        discovery_substage=discovery_substage,
                        exposed_tools=exposed_tools,
                        tool_calls=tool_calls,
                        history=history,
                    ),
                }
            )
            context_components = self._context_component_estimates(provider, turn_request, history)
            component_estimate = sum(context_components.values())
            # Stage-specific tool schemas and compact prior results can make a later turn
            # materially smaller than the first. Estimate the actual next request rather
            # than conservatively replaying the previous measured input size.
            provider_estimator = getattr(provider, "estimate_context_components", None)
            estimated_next_input = (
                component_estimate
                if callable(provider_estimator)
                else max(
                    component_estimate,
                    last_turn_usage.input_tokens if last_turn_usage is not None else 0,
                )
            )
            emit(
                "agent_run.context_precheck",
                "ready",
                discovery_substage=discovery_substage,
                tools_exposed=exposed_tools,
                exposed_tool_count=len(exposed_tools),
                cumulative_input_tokens=usage.input_tokens,
                estimated_next_turn_input_tokens=estimated_next_input,
                remaining_input_tokens=max(
                    0, request.budget.maximum_input_tokens - usage.input_tokens
                ),
                remaining_cost_cents=max(0.0, request.budget.maximum_cost_cents - usage.cost_cents),
                context_components=context_components,
            )
            estimated_budget_error = self._estimated_next_turn_budget_error(
                request,
                usage,
                last_turn_usage,
                estimated_next_input=estimated_next_input,
            )
            if estimated_budget_error:
                code, estimate = estimated_budget_error
                emit(
                    "agent_run.budget_precheck_failed",
                    "budget_exceeded",
                    code=code,
                    completed_model_turns=turns,
                    **estimate,
                )
                result = self._failure(
                    AgentRunStatus.BUDGET_EXCEEDED,
                    code,
                    self._budget_message(code),
                    trace,
                    usage,
                    turns,
                    tool_calls,
                    started,
                )
                break
            turn_number = turns + 1
            if provider_invocations >= int(
                request.context.get("remaining_global_provider_invocations", 1_000_000)
            ):
                result = self._failure(
                    AgentRunStatus.BUDGET_EXCEEDED,
                    "global_provider_invocation_budget_exceeded",
                    "The global provider-invocation budget is exhausted.",
                    trace,
                    usage,
                    turns,
                    tool_calls,
                    started,
                )
                break
            provider_invocations += 1
            fingerprint = None
            fingerprint_builder = getattr(provider, "structured_output_fingerprint", None)
            if callable(fingerprint_builder):
                fingerprint = fingerprint_builder(turn_request)
                if (
                    not fingerprint.output_type_present
                    or not fingerprint.boundary_runtime_contracts_match
                ):
                    emit(
                        "provider.structured_output_contract_mismatch",
                        "failed",
                        structured_output_request_fingerprint=fingerprint.model_dump(mode="json"),
                    )
                    result = self._failure(
                        AgentRunStatus.FAILED,
                        "structured_output_contract_mismatch",
                        "Provider execution was blocked because its structured-output contract "
                        "did not match the validated boundary configuration.",
                        trace,
                        usage,
                        turns,
                        tool_calls,
                        started,
                    )
                    break
            emit(
                "provider.turn.started",
                "running",
                turn=turn_number,
                provider_invocation=provider_invocations,
                retry_attempt=retries,
                discovery_substage=discovery_substage,
                tools_exposed=exposed_tools,
                structured_output_request_fingerprint=(
                    fingerprint.model_dump(mode="json") if fingerprint else None
                ),
            )
            try:
                turn = self._provider_turn(
                    provider,
                    turn_request,
                    history,
                    interruption_request(),
                    timeout=max(0.01, request.budget.timeout_seconds - elapsed),
                )
            except (ProviderTimeout, FutureTimeout) as exc:
                timeout_detail = {
                    "turn": turn_number,
                    "provider_invocation": provider_invocations,
                    "retryable": True,
                    **getattr(
                        exc,
                        "trace_detail",
                        ProviderTimeout(
                            "Provider boundary timed out.", exception_class=type(exc).__name__
                        ).trace_detail,
                    ),
                }
                emit("provider.turn.failed", "timed_out", **timeout_detail)
                log_provider_failure(timeout_detail)
                result = self._failure(
                    AgentRunStatus.TIMED_OUT,
                    "provider_timeout",
                    "Provider exceeded the configured timeout.",
                    trace,
                    usage,
                    turns,
                    tool_calls,
                    started,
                    retryable=True,
                )
                break
            except ProviderFailure as exc:
                exception_diagnostic = exc.trace_detail.get("structured_output_diagnostic")
                if isinstance(exception_diagnostic, dict):
                    diagnostic_usage = exception_diagnostic.get("usage")
                    if isinstance(diagnostic_usage, dict):
                        usage = self._add_usage(
                            usage,
                            UsageReport.model_validate(diagnostic_usage),
                        )
                failure_detail = {
                    "turn": turn_number,
                    "provider_invocation": provider_invocations,
                    "retryable": exc.retryable,
                    **exc.trace_detail,
                }
                emit(
                    "provider.turn.failed",
                    "failed",
                    **failure_detail,
                )
                log_provider_failure(failure_detail)
                if exc.retryable and retries < request.budget.retry_count:
                    retries += 1
                    emit(
                        "provider.retry",
                        "retrying",
                        retry=retries,
                        turn=turn_number,
                        provider_invocation=provider_invocations,
                    )
                    continue
                result = self._failure(
                    AgentRunStatus.FAILED,
                    "provider_failure",
                    "Provider failed after bounded retries.",
                    trace,
                    usage,
                    turns,
                    tool_calls,
                    started,
                    retryable=exc.retryable,
                )
                break
            turns += 1
            last_turn_usage = turn.usage
            usage = self._add_usage(usage, turn.usage)
            if turn.diagnostic:
                diagnostic = turn.diagnostic
                logger.warning(
                    "structured_output_terminal workflow_id=%s agent_run_id=%s agent=%s "
                    "provider=%s model=%s classification=%s handler=%s request_ids=%s "
                    "response_ids=%s usage_status=%s retryable=%s developer_message=%s",
                    request.workflow_id,
                    run_id,
                    diagnostic.agent_role,
                    diagnostic.provider,
                    diagnostic.configured_model,
                    diagnostic.failure_classification,
                    diagnostic.error_handler,
                    diagnostic.provider_request_ids,
                    diagnostic.provider_response_ids,
                    diagnostic.usage.usage_status,
                    diagnostic.retryable,
                    diagnostic.developer_message,
                )
            emit(
                "provider.turn.completed",
                "completed",
                turn=turns,
                provider_invocation=provider_invocations,
                kind=turn.kind,
                discovery_substage=discovery_substage,
                tools_exposed=exposed_tools,
                structured_output_diagnostic=(
                    turn.diagnostic.model_dump(mode="json") if turn.diagnostic else None
                ),
            )
            budget_error = self._budget_error(request, usage)
            if budget_error:
                result = self._failure(
                    AgentRunStatus.BUDGET_EXCEEDED,
                    budget_error,
                    self._budget_message(budget_error),
                    trace,
                    usage,
                    turns,
                    tool_calls,
                    started,
                )
                break
            if turn.kind == "tool":
                if turn.tool_request is None:
                    result = self._failure(
                        AgentRunStatus.FAILED,
                        "provider_protocol_error",
                        "Provider returned an incomplete tool request.",
                        trace,
                        usage,
                        turns,
                        tool_calls,
                        started,
                    )
                    break
                if tool_calls >= request.budget.maximum_tool_calls:
                    result = self._failure(
                        AgentRunStatus.BUDGET_EXCEEDED,
                        "maximum_tool_calls_exceeded",
                        "Agent run exceeded its maximum tool-call count.",
                        trace,
                        usage,
                        turns,
                        tool_calls,
                        started,
                    )
                    break
                requested = turn.tool_request
                model_supplied_arguments = requested.arguments
                required_modality = turn_request.context.get("required_activity_modality")
                if requested.tool_name == "search_activity_sources" and isinstance(
                    required_modality, str
                ):
                    validated = turn_request.context.get("validated_artifacts", {})
                    endpoint_scope = (
                        validated.get("endpoint_discovery_scope", {})
                        if isinstance(validated, dict)
                        else {}
                    )
                    approved_target = (
                        endpoint_scope.get("biological_target")
                        if isinstance(endpoint_scope, dict)
                        else None
                    )
                    approved_target = (
                        approved_target
                        if isinstance(approved_target, str) and approved_target.strip()
                        else requested.arguments.get("biological_target")
                    )
                    requested = requested.model_copy(
                        update={
                            "arguments": {
                                **requested.arguments,
                                "query": f"{approved_target} {required_modality}",
                                "biological_target": approved_target,
                                "endpoint_modality": required_modality,
                            }
                        }
                    )
                estimated_source_requests = self._estimated_scientific_source_requests(requested)
                if scientific_source_requests + estimated_source_requests > int(
                    request.context.get(
                        "remaining_global_scientific_source_requests",
                        1_000_000,
                    )
                ):
                    result = self._failure(
                        AgentRunStatus.BUDGET_EXCEEDED,
                        "global_scientific_source_request_budget_exceeded",
                        "The global scientific-source request budget is exhausted.",
                        trace,
                        usage,
                        turns,
                        tool_calls,
                        started,
                    )
                    break
                tool_calls += 1
                duplicate_query = any(
                    item.get("tool_name") == requested.tool_name
                    and canonical_json(item.get("original_arguments") or {})
                    == canonical_json(requested.arguments)
                    for item in history
                )
                if duplicate_query:
                    tool_result = ToolResult(
                        tool_name=requested.tool_name,
                        status=ToolCallStatus.PROHIBITED,
                        error=NormalizedAgentError(
                            code="duplicate_tool_query",
                            safe_message=(
                                "An identical completed tool request cannot be repeated in the "
                                "same bounded agent run."
                            ),
                            retryable=False,
                            category="policy",
                        ),
                        original_arguments=requested.arguments,
                        normalized_arguments=requested.arguments,
                    )
                    call_id = self._record_prohibited_call(
                        run_id, turn_request, requested, tool_result, tool_calls
                    )
                elif requested.tool_name not in turn_request.available_tools:
                    tool_result = ToolResult(
                        tool_name=requested.tool_name,
                        status=ToolCallStatus.PROHIBITED,
                        error=NormalizedAgentError(
                            code="prohibited_tool",
                            safe_message=(
                                "Provider requested a tool not allowed for this agent run."
                            ),
                            retryable=False,
                            category="policy",
                        ),
                    )
                    call_id = self._record_prohibited_call(
                        run_id, turn_request, requested, tool_result, tool_calls
                    )
                else:
                    call_id, tool_result = self._invoke_tool(
                        run_id,
                        turn_request,
                        requested,
                        tool_calls,
                        model_supplied_arguments=model_supplied_arguments,
                    )
                if (
                    tool_result_callback is not None
                    and tool_result.status is ToolCallStatus.COMPLETED
                ):
                    try:
                        tool_result_callback(call_id, requested.tool_name, tool_result)
                    except Exception as exc:
                        emit(
                            "tool_observation.persistence_failed",
                            "failed",
                            tool_call_id=call_id,
                            tool_name=requested.tool_name,
                            exception_class=type(exc).__name__,
                        )
                        result = self._failure(
                            AgentRunStatus.FAILED,
                            "tool_observation_persistence_failed",
                            "A verified source observation could not be persisted safely.",
                            trace,
                            usage,
                            turns,
                            tool_calls,
                            started,
                        )
                        break
                if isinstance(tool_result.output, dict):
                    scientific_source_requests += int(
                        tool_result.output.get("source_request_count", 0) or 0
                    )
                source_diagnostics = []
                if tool_result.source_diagnostic:
                    source_diagnostics.append(tool_result.source_diagnostic.model_dump(mode="json"))
                if isinstance(tool_result.output, dict):
                    for candidate_result in tool_result.output.get("results", []):
                        if isinstance(candidate_result, dict) and isinstance(
                            candidate_result.get("source_diagnostic"), dict
                        ):
                            source_diagnostics.append(candidate_result["source_diagnostic"])
                emit(
                    "tool_call.completed",
                    tool_result.status.value,
                    tool_name=requested.tool_name,
                    discovery_substage=discovery_substage,
                    tools_exposed=exposed_tools,
                    tool_requested=requested.tool_name,
                    tool_executed=(
                        requested.tool_name
                        if tool_result.status is not ToolCallStatus.PROHIBITED
                        else None
                    ),
                    tool_call_id=call_id,
                    replayed=tool_result.replayed,
                    model_supplied_arguments=tool_result.original_arguments,
                    normalized_execution_arguments=tool_result.normalized_arguments,
                    normalization_warning_count=len(tool_result.normalization_warnings),
                    normalization_warning_codes=[
                        warning.code for warning in tool_result.normalization_warnings
                    ],
                    source_diagnostic=(
                        tool_result.source_diagnostic.model_dump(mode="json")
                        if tool_result.source_diagnostic
                        else None
                    ),
                    tool_diagnostic=(
                        tool_result.error.tool_diagnostic.model_dump(mode="json")
                        if tool_result.error and tool_result.error.tool_diagnostic
                        else None
                    ),
                    source_diagnostics=source_diagnostics,
                )
                history.append(
                    {
                        "tool_name": requested.tool_name,
                        "status": tool_result.status.value,
                        "original_arguments": tool_result.original_arguments,
                        "normalized_arguments": tool_result.normalized_arguments,
                        "output": tool_result.output,
                        "error": tool_result.error.model_dump(mode="json")
                        if tool_result.error
                        else None,
                        "source_diagnostic": (
                            tool_result.source_diagnostic.model_dump(mode="json")
                            if tool_result.source_diagnostic
                            else None
                        ),
                        "discovery_substage": discovery_substage,
                    }
                )
                if tool_result.status is not ToolCallStatus.COMPLETED:
                    result = self._failure(
                        AgentRunStatus.FAILED,
                        tool_result.error.code if tool_result.error else "tool_failure",
                        tool_result.error.safe_message
                        if tool_result.error
                        else "Tool failed safely.",
                        trace,
                        usage,
                        turns,
                        tool_calls,
                        started,
                        retryable=(tool_result.error.retryable if tool_result.error else False),
                        category=(
                            tool_result.error.category if tool_result.error else "agent_runtime"
                        ),
                        source_diagnostic=tool_result.source_diagnostic,
                        tool_diagnostic=(
                            tool_result.error.tool_diagnostic if tool_result.error else None
                        ),
                    )
                continue
            if turn.kind == "approval":
                if turn.approval is None:
                    result = self._failure(
                        AgentRunStatus.FAILED,
                        "provider_protocol_error",
                        "Provider returned an incomplete approval proposal.",
                        trace,
                        usage,
                        turns,
                        tool_calls,
                        started,
                    )
                else:
                    emit("agent_run.interrupted_for_approval", "approval_required")
                    result = AgentRunResult(
                        status=AgentRunStatus.APPROVAL_REQUIRED,
                        approval_proposal=turn.approval,
                        usage=usage,
                        trace=trace,
                        turns=turns,
                        tool_calls=tool_calls,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                break
            if turn.kind == "error":
                result = self._failure(
                    AgentRunStatus.FAILED,
                    turn.error.code if turn.error else "provider_error",
                    turn.error.safe_message if turn.error else "Provider returned an error.",
                    trace,
                    usage,
                    turns,
                    tool_calls,
                    started,
                )
                break
            if (
                request.agent_name == "Activity Evidence Discovery Agent"
                and discovery_substage == "candidate_search"
            ):
                result = self._failure(
                    AgentRunStatus.FAILED,
                    "mandatory_activity_searches_incomplete",
                    "Activity synthesis cannot complete before every required modality search.",
                    trace,
                    usage,
                    turns,
                    tool_calls,
                    started,
                    category="orchestration_validation",
                )
                break
            try:
                validated = output_model.model_validate(turn.output)
            except ValidationError as exc:
                validation_failures += 1
                emit(
                    "output.validation_failed",
                    "failed",
                    attempt=validation_failures,
                    errors=exc.errors(include_input=False, include_url=False),
                )
                if output_model.__name__ == "DatasetSpecificationAgentOutcome":
                    validated = output_model.model_validate(
                        {
                            "schema_version": "1.0.0",
                            "status": "invalid_model_output",
                            "specification": None,
                            "requires_human_review": True,
                            "decision_summary": (
                                "The structured response requires revision before source "
                                "discovery."
                            ),
                            "blocking_questions": [],
                            "approval_questions": [],
                            "missing_core_elements": [],
                            "unresolved_questions": [],
                            "limitations": ["No valid dataset specification was produced."],
                            "failure_category": "schema_validation_failed",
                            "safe_failure_summary": (
                                "Structured output validation failed; no scientific values "
                                "were inferred."
                            ),
                        }
                    )
                    emit(
                        "output.invalid_terminal_outcome",
                        "revision_required",
                        failure_classification="schema_validation_failed",
                        provider_retry_created=False,
                    )
                    result = AgentRunResult(
                        status=AgentRunStatus.COMPLETED,
                        output=validated.model_dump(mode="json"),
                        usage=usage,
                        trace=trace,
                        turns=turns,
                        tool_calls=tool_calls,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                    break
                if output_model.__name__ == "DatasetSpecificationReviewOutcome":
                    validated = output_model.model_validate(
                        {
                            "schema_version": "1.0.0",
                            "status": "invalid_model_output",
                            "review_summary": (
                                "Optional AI review output failed validation; the deterministic "
                                "draft remains unchanged."
                            ),
                            "blocking_findings": [],
                            "approval_questions_to_add": [],
                            "suggested_field_corrections": [],
                            "scientific_consistency_flags": [],
                            "requires_human_review": True,
                        }
                    )
                    emit(
                        "output.invalid_terminal_outcome",
                        "human_review_available",
                        failure_classification="schema_validation_failed",
                        provider_retry_created=False,
                        deterministic_draft_preserved=True,
                    )
                    result = AgentRunResult(
                        status=AgentRunStatus.COMPLETED,
                        output=validated.model_dump(mode="json"),
                        usage=usage,
                        trace=trace,
                        turns=turns,
                        tool_calls=tool_calls,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                    break
                if validation_failures <= 1:
                    history.append(
                        {"validation_error": exc.errors(include_input=False, include_url=False)}
                    )
                    continue
                result = self._failure(
                    AgentRunStatus.FAILED,
                    "malformed_output",
                    "Provider output failed the required schema after one repair attempt.",
                    trace,
                    usage,
                    turns,
                    tool_calls,
                    started,
                )
                break
            emit("output.validated", "completed")
            result = AgentRunResult(
                status=AgentRunStatus.COMPLETED,
                output=validated.model_dump(mode="json"),
                usage=usage,
                trace=trace,
                turns=turns,
                tool_calls=tool_calls,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        emit("agent_run.finished", result.status.value)
        result = result.model_copy(
            update={
                "trace": trace,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "usage": result.usage.model_copy(
                    update={"provider_invocations": provider_invocations}
                ),
            }
        )
        self._finish_run(run_id, request, result)
        return run_id, result

    @staticmethod
    def _provider_turn(provider, request, history, interrupted, *, timeout):
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            provider.run_turn,
            request,
            list(history),
            interruption_requested=interrupted,
        )
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as exc:
            future.cancel()
            raise ProviderTimeout("Provider turn timed out.") from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _start_run(self, run_id, request, safe_request, input_hash) -> AgentRunResult | None:
        with self.database.session() as session:
            step = session.get(WorkflowStepRow, request.step_id)
            if step is None or step.workflow_id != request.workflow_id:
                raise WorkflowNotFound("Compatible workflow step was not found for agent run.")
            build = require_build(session, request.workflow_id)
            existing = session.get(AgentRunRow, run_id)
            if existing is not None:
                if existing.status == AgentRunStatus.RUNNING.value:
                    raise WorkflowConflict("Identical agent run is already active.")
                if existing.result_json:
                    return AgentRunResult.model_validate_json(existing.result_json)
            row = AgentRunRow(
                id=run_id,
                workflow_id=request.workflow_id,
                step_id=request.step_id,
                agent_name=request.agent_name,
                agent_version=request.agent_version,
                provider=request.model.provider,
                model_identifier=request.model.model_identifier,
                instruction_version=request.instruction_version,
                input_hash=input_hash,
                status=AgentRunStatus.RUNNING.value,
                request_json=canonical_json(safe_request),
                result_json=None,
                trace_json=canonical_json(versioned_payload(events=[])),
                usage_json=UsageReport().model_dump_json(),
                turns=0,
                duration_ms=0,
                created_at=utc_text(),
                completed_at=None,
            )
            session.add(row)
            session.flush()
            append_event(
                session,
                build,
                event_type="agent_run.started",
                actor_type="agent",
                actor_id=request.agent_name,
                idempotency_key=f"agent-run:{run_id}:started",
                payload={
                    "agent_run_id": run_id,
                    "step_id": request.step_id,
                    "input_hash": input_hash,
                },
                from_state=build.current_stage,
                to_state=build.current_stage,
            )
        return None

    def _finish_run(self, run_id, request, result: AgentRunResult) -> None:
        with self.database.session() as session:
            row = session.get(AgentRunRow, run_id)
            if row is None:
                raise WorkflowNotFound("Agent run disappeared before completion.")
            row.status = result.status.value
            row.result_json = result.model_dump_json()
            row.trace_json = canonical_json(
                versioned_payload(events=[event.model_dump(mode="json") for event in result.trace])
            )
            row.usage_json = result.usage.model_dump_json()
            row.turns = result.turns
            row.duration_ms = result.duration_ms
            row.completed_at = utc_text()
            build = require_build(session, request.workflow_id)
            if result.error:
                session.add(
                    WorkflowErrorRow(
                        id=deterministic_id("err", run_id, result.error.code),
                        workflow_id=request.workflow_id,
                        step_id=request.step_id,
                        agent_run_id=run_id,
                        tool_call_id=None,
                        code=result.error.code,
                        category=result.error.category,
                        retryable=int(result.error.retryable),
                        safe_message=result.error.safe_message,
                        detail_json=canonical_json(
                            versioned_payload(
                                agent_run_id=run_id,
                                source_diagnostic=(
                                    result.error.source_diagnostic.model_dump(mode="json")
                                    if result.error.source_diagnostic
                                    else None
                                ),
                                tool_diagnostic=(
                                    result.error.tool_diagnostic.model_dump(mode="json")
                                    if result.error.tool_diagnostic
                                    else None
                                ),
                            )
                        ),
                        created_at=utc_text(),
                    )
                )
            append_event(
                session,
                build,
                event_type="agent_run.finished",
                actor_type="agent",
                actor_id=request.agent_name,
                idempotency_key=f"agent-run:{run_id}:finished",
                payload={
                    "agent_run_id": run_id,
                    "status": result.status.value,
                    "turns": result.turns,
                    "tool_calls": result.tool_calls,
                    "duration_ms": result.duration_ms,
                    "usage": result.usage.model_dump(mode="json"),
                },
                from_state=build.current_stage,
                to_state=build.current_stage,
            )

    def _invoke_tool(
        self,
        run_id,
        request,
        tool_request,
        ordinal,
        *,
        model_supplied_arguments: dict[str, Any] | None = None,
    ) -> tuple[str, ToolResult]:
        key = tool_request.idempotency_key or f"turn-{ordinal}"
        tool = self.tools.get(tool_request.tool_name)
        arguments = redact(tool_request.arguments)
        with self.database.session() as session:
            prior = session.scalar(
                select(ToolCallRow).where(
                    ToolCallRow.workflow_id == request.workflow_id,
                    ToolCallRow.tool_name == tool_request.tool_name,
                    ToolCallRow.idempotency_key == key,
                )
            )
            if prior is not None:
                stored_arguments = load_versioned_json(prior.arguments_json).get("arguments")
                if canonical_json(stored_arguments) != canonical_json(arguments):
                    raise WorkflowConflict(
                        "Tool idempotency key was reused with different arguments."
                    )
                if prior.status != ToolCallStatus.COMPLETED.value or not prior.result_json:
                    raise WorkflowConflict("Prior logical tool call did not complete successfully.")
                replay = ToolResult.model_validate_json(prior.result_json).model_copy(
                    update={"replayed": True}
                )
                return prior.id, replay
            call_id = deterministic_id("tool", request.workflow_id, tool_request.tool_name, key)
            row = ToolCallRow(
                id=call_id,
                workflow_id=request.workflow_id,
                agent_run_id=run_id,
                tool_name=tool_request.tool_name,
                tool_version=tool.definition.implementation_version,
                permission_scope_json=canonical_json(
                    versioned_payload(permissions=request.context.get("permission_scope", []))
                ),
                arguments_json=canonical_json(versioned_payload(arguments=arguments)),
                result_json=None,
                status=ToolCallStatus.RUNNING.value,
                idempotency_key=key,
                duration_ms=0,
                created_at=utc_text(),
                completed_at=None,
            )
            session.add(row)
            build = require_build(session, request.workflow_id)
            append_event(
                session,
                build,
                event_type="tool_call.started",
                actor_type="tool",
                actor_id=tool_request.tool_name,
                idempotency_key=f"tool-call:{call_id}:started",
                payload={
                    "tool_call_id": call_id,
                    "agent_run_id": run_id,
                    "tool": tool_request.tool_name,
                },
                from_state=build.current_stage,
                to_state=build.current_stage,
            )
        result = self.tools.invoke(
            ToolInvocation(
                tool_name=tool_request.tool_name,
                arguments=tool_request.arguments,
                workflow_id=request.workflow_id,
                step_id=request.step_id,
                workflow_stage=WorkflowState(request.context["workflow_stage"]),
                permission_scope=request.context.get("permission_scope", []),
                run_context={
                    "run_mode": request.context.get("run_mode", "replay"),
                    "agent_role": request.agent_name,
                    "dependency_status": request.context.get(
                        "dependency_status", "prerequisites_satisfied"
                    ),
                    "refresh_source_metadata": bool(
                        request.context.get("refresh_source_metadata", False)
                    ),
                },
                idempotency_key=key,
            )
        )
        original_arguments = redact(
            model_supplied_arguments or result.original_arguments or tool_request.arguments
        )
        normalized_arguments = redact(result.normalized_arguments or tool_request.arguments)
        if model_supplied_arguments is not None and canonical_json(
            model_supplied_arguments
        ) != canonical_json(tool_request.arguments):
            enforced_warnings = [
                ToolNormalizationWarning(
                    code="approved_activity_scope_enforced",
                    field=field,
                    original_index=index,
                    original=str(model_supplied_arguments.get(field, "")),
                    normalized=str(tool_request.arguments.get(field, "")),
                    policy_version="activity-modality-sequence-v1",
                )
                for index, field in enumerate(("biological_target", "endpoint_modality", "query"))
                if model_supplied_arguments.get(field) != tool_request.arguments.get(field)
            ]
            result = result.model_copy(
                update={
                    "normalization_warnings": [
                        *result.normalization_warnings,
                        *enforced_warnings,
                    ]
                }
            )
        normalization_warnings = [
            warning.model_dump(mode="json") for warning in result.normalization_warnings
        ]
        result = result.model_copy(
            update={
                "original_arguments": original_arguments,
                "normalized_arguments": normalized_arguments,
            }
        )
        with self.database.session() as session:
            row = session.get(ToolCallRow, call_id)
            row.arguments_json = canonical_json(
                versioned_payload(
                    arguments=arguments,
                    original_arguments=original_arguments,
                    normalized_arguments=normalized_arguments,
                    normalization_warnings=normalization_warnings,
                )
            )
            row.result_json = result.model_dump_json()
            row.status = result.status.value
            row.duration_ms = result.duration_ms
            row.completed_at = utc_text()
            build = require_build(session, request.workflow_id)
            append_event(
                session,
                build,
                event_type="tool_call.finished",
                actor_type="tool",
                actor_id=tool_request.tool_name,
                idempotency_key=f"tool-call:{call_id}:finished",
                payload={
                    "tool_call_id": call_id,
                    "status": result.status.value,
                    "duration_ms": result.duration_ms,
                    "normalization_warning_count": len(normalization_warnings),
                    "normalization_warning_codes": [
                        warning["code"] for warning in normalization_warnings
                    ],
                    "source_diagnostic": (
                        result.source_diagnostic.model_dump(mode="json")
                        if result.source_diagnostic
                        else None
                    ),
                    "source_diagnostics": [
                        item.get("source_diagnostic")
                        for item in (result.output or {}).get("results", [])
                        if isinstance(item, dict)
                        and isinstance(item.get("source_diagnostic"), dict)
                    ],
                    "tool_diagnostic": (
                        result.error.tool_diagnostic.model_dump(mode="json")
                        if result.error and result.error.tool_diagnostic
                        else None
                    ),
                },
                from_state=build.current_stage,
                to_state=build.current_stage,
            )
        return call_id, result

    def _record_prohibited_call(self, run_id, request, tool_request, result, ordinal) -> str:
        key = tool_request.idempotency_key or f"turn-{ordinal}"
        call_id = deterministic_id("tool", request.workflow_id, tool_request.tool_name, key)
        with self.database.session() as session:
            row = ToolCallRow(
                id=call_id,
                workflow_id=request.workflow_id,
                agent_run_id=run_id,
                tool_name=tool_request.tool_name,
                tool_version="unregistered",
                permission_scope_json=canonical_json(versioned_payload(permissions=[])),
                arguments_json=canonical_json(
                    versioned_payload(arguments=redact(tool_request.arguments))
                ),
                result_json=result.model_dump_json(),
                status=ToolCallStatus.PROHIBITED.value,
                idempotency_key=key,
                duration_ms=0,
                created_at=utc_text(),
                completed_at=utc_text(),
            )
            session.add(row)
            build = require_build(session, request.workflow_id)
            append_event(
                session,
                build,
                event_type="tool_call.prohibited",
                actor_type="tool",
                actor_id=tool_request.tool_name,
                idempotency_key=f"tool-call:{call_id}:prohibited",
                payload={"tool_call_id": call_id, "tool": tool_request.tool_name},
                from_state=build.current_stage,
                to_state=build.current_stage,
            )
        return call_id

    @staticmethod
    def _stage_scoped_turn_context(
        request: AgentRunRequest,
        *,
        discovery_substage: str,
        exposed_tools: list[str],
        tool_calls: int,
        history: list[dict] | None = None,
    ) -> dict:
        """Bind the provider boundary proof to the deterministic per-stage tool scope."""

        context = {
            **request.context,
            "discovery_substage": discovery_substage,
            "tools_exposed": exposed_tools,
            "tool_budget_remaining": max(0, request.budget.maximum_tool_calls - tool_calls),
        }
        stage_tool_sets = request.context.get("stage_tool_sets")
        if (
            request.agent_name == "Activity Evidence Discovery Agent"
            and discovery_substage == "candidate_search"
        ):
            required = list(
                dict.fromkeys(str(item) for item in request.context.get("candidate_modalities", []))
            ) or ["binding", "agonism", "antagonism"]
            completed = {
                str((item.get("normalized_arguments") or {}).get("endpoint_modality"))
                for item in history or []
                if item.get("status") == ToolCallStatus.COMPLETED.value
                and item.get("tool_name") == "search_activity_sources"
            }
            context["required_activity_modality"] = next(
                (item for item in required if item not in completed),
                None,
            )
        expected_contract = request.context.get("structured_output_boundary_contract")
        if (
            isinstance(stage_tool_sets, dict)
            and stage_tool_sets
            and isinstance(expected_contract, dict)
        ):
            context["structured_output_boundary_contract"] = {
                **expected_contract,
                "tool_count": len(exposed_tools),
                "tool_names": sorted(exposed_tools),
                "tool_choice_mode": "auto" if exposed_tools else "none",
            }
        return context

    @staticmethod
    def _next_orchestrated_discovery_tool(
        request: AgentRunRequest,
        history: list[dict],
    ) -> ProviderToolRequest | None:
        """Return the next deterministic hydration/batch call, never a model-invented query."""

        completed = [
            item
            for item in history
            if item.get("status") == ToolCallStatus.COMPLETED.value
            and isinstance(item.get("tool_name"), str)
        ]

        def observed_identifiers(*, adapter_id: str, prefixes: tuple[str, ...]) -> list[str]:
            return list(
                dict.fromkeys(
                    str(observation.get("stable_source_identifier"))
                    for item in completed
                    for observation in (
                        (item.get("output") or {}).get("observations", [])
                        if isinstance(item.get("output"), dict)
                        else []
                    )
                    if isinstance(observation, dict)
                    and (item.get("output") or {}).get("adapter_id") == adapter_id
                    and str(observation.get("stable_source_identifier", "")).startswith(prefixes)
                )
            )

        def completed_identifiers(tool_name: str, field: str) -> set[str]:
            values: set[str] = set()
            for item in completed:
                if item.get("tool_name") != tool_name:
                    continue
                arguments = item.get("normalized_arguments") or {}
                for value in arguments.get(field, []):
                    if isinstance(value, str):
                        values.add(value)
                stable = arguments.get("stable_identifier")
                if isinstance(stable, str):
                    values.add(stable)
            return values

        def planned(tool_name: str, arguments: dict[str, Any], ordinal: int) -> ProviderToolRequest:
            digest = hashlib.sha256(
                canonical_json({"tool": tool_name, "arguments": arguments}).encode()
            ).hexdigest()[:24]
            return ProviderToolRequest(
                tool_name=tool_name,
                arguments=arguments,
                idempotency_key=f"orchestrated-{ordinal}-{digest}",
            )

        if request.agent_name == "Activity Evidence Discovery Agent":
            required = list(
                dict.fromkeys(str(item) for item in request.context.get("candidate_modalities", []))
            ) or ["binding", "agonism", "antagonism"]
            searched = {
                str((item.get("normalized_arguments") or {}).get("endpoint_modality"))
                for item in completed
                if item.get("tool_name") == "search_activity_sources"
            }
            if not set(required).issubset(searched):
                return None
            source_identifiers = observed_identifiers(
                adapter_id="pubchem-bioassay",
                prefixes=("AID:",),
            )
            for ordinal, operation in enumerate(("fetch_activity_source_metadata",), start=1):
                if operation not in request.available_tools:
                    continue
                remaining = [
                    item
                    for item in source_identifiers
                    if item not in completed_identifiers(operation, "source_identifiers")
                ]
                if remaining:
                    return planned(
                        operation,
                        {
                            "source_system": "pubchem-bioassay",
                            "source_identifiers": remaining[:5],
                            "maximum_results": 5,
                        },
                        ordinal,
                    )
            return None

        if request.agent_name == "Transcriptomic Evidence Discovery Agent":
            validated = request.context.get("validated_artifacts", {})
            dependency = (
                validated.get("compound_first_dependency", {})
                if isinstance(validated, dict)
                else {}
            )
            compounds = [
                item
                for item in dependency.get("search_batch", [])
                if isinstance(item, dict)
                and re.fullmatch(
                    r"CID:[1-9][0-9]{0,11}",
                    str(item.get("pubchem_cid") or ""),
                    flags=re.I,
                )
                and str(item.get("canonical_name") or "").strip()
            ][:5]
            if not compounds:
                return None

            source_searches = (
                ("search_transcriptomic_sources", "ncbi-geo-series"),
                ("search_lincs_resources", "lincs-l1000"),
            )

            def call_matches(
                item: dict[str, Any],
                *,
                tool_name: str,
                pubchem_cid: str,
                stable_identifier: str | None = None,
            ) -> bool:
                if item.get("tool_name") != tool_name:
                    return False
                arguments = item.get("normalized_arguments") or {}
                sampled = arguments.get("sampled_identifiers", [])
                if pubchem_cid not in sampled:
                    return False
                if stable_identifier is None:
                    return True
                return stable_identifier in {
                    arguments.get("stable_identifier"),
                    *arguments.get("source_identifiers", []),
                }

            # Search both reviewed source families for every bounded upstream compound before
            # hydrating any hit. A zero result is a completed scientific outcome and therefore
            # does not prevent the remaining compounds from being searched.
            for compound_index, compound in enumerate(compounds, start=1):
                pubchem_cid = str(compound["pubchem_cid"]).upper()
                canonical_name = str(compound["canonical_name"]).strip()
                verified_terms = [
                    canonical_name,
                    *[
                        str(value).strip()
                        for value in compound.get("verified_synonyms", [])
                        if str(value).strip()
                    ],
                ][:3]
                query_subject = " OR ".join(f'"{value}"' for value in verified_terms)
                for source_index, (operation, source_system) in enumerate(
                    source_searches, start=1
                ):
                    if operation not in request.available_tools:
                        continue
                    if any(
                        call_matches(
                            item,
                            tool_name=operation,
                            pubchem_cid=pubchem_cid,
                        )
                        for item in completed
                    ):
                        continue
                    query = (
                        f"({query_subject}) chemical perturbation gene expression"
                        if operation == "search_transcriptomic_sources"
                        else f"({query_subject}) compound perturbation"
                    )
                    return planned(
                        operation,
                        {
                            "source_system": source_system,
                            "query": query,
                            "biological_target": canonical_name,
                            "endpoint_modality": "chemical perturbation transcriptomics",
                            "sampled_identifiers": [pubchem_cid],
                            "verified_compound_names": verified_terms,
                            "maximum_results": 1,
                        },
                        compound_index * 10 + source_index,
                    )

            # Hydrate one bounded candidate per compound/source search. Search arguments carry
            # the upstream CID so the verified metadata row can be joined without asking the
            # model to infer identity from a title or accession.
            for search_item in completed:
                search_tool = str(search_item.get("tool_name") or "")
                if search_tool not in {name for name, _source in source_searches}:
                    continue
                arguments = search_item.get("normalized_arguments") or {}
                sampled = arguments.get("sampled_identifiers", [])
                matched_cid = next(
                    (
                        str(value).upper()
                        for value in sampled
                        if re.fullmatch(r"CID:[1-9][0-9]{0,11}", str(value), flags=re.I)
                    ),
                    None,
                )
                if matched_cid is None:
                    continue
                compound = next(
                    (
                        item
                        for item in compounds
                        if str(item["pubchem_cid"]).upper() == matched_cid
                    ),
                    None,
                )
                if compound is None:
                    continue
                output = search_item.get("output") or {}
                observations = output.get("observations", []) if isinstance(output, dict) else []
                stable_identifier = next(
                    (
                        str(item.get("stable_source_identifier"))
                        for item in observations
                        if isinstance(item, dict)
                        and str(item.get("stable_source_identifier") or "").startswith(
                            ("GDS_UID:", "GSE")
                        )
                    ),
                    None,
                )
                if stable_identifier is None:
                    continue
                hydration_tool = (
                    "fetch_transcriptomic_source_metadata"
                    if search_tool == "search_transcriptomic_sources"
                    else "inspect_lincs_signature_metadata"
                )
                if hydration_tool not in request.available_tools:
                    continue
                if any(
                    call_matches(
                        item,
                        tool_name=hydration_tool,
                        pubchem_cid=matched_cid,
                        stable_identifier=stable_identifier,
                    )
                    for item in completed
                ):
                    continue
                identity_terms = [
                    matched_cid,
                    *(
                        [str(compound.get("inchikey"))]
                        if compound.get("inchikey")
                        else []
                    ),
                ]
                hydration_arguments = {
                    "source_system": (
                        "ncbi-geo-series"
                        if hydration_tool == "fetch_transcriptomic_source_metadata"
                        else "lincs-l1000"
                    ),
                    "stable_identifier": stable_identifier,
                    "sampled_identifiers": identity_terms,
                    "biological_target": str(compound["canonical_name"]),
                    "verified_compound_names": [
                        str(compound["canonical_name"]),
                        *[
                            str(value)
                            for value in compound.get("verified_synonyms", [])
                            if str(value).strip()
                        ],
                    ][:5],
                    "endpoint_modality": "chemical perturbation transcriptomics",
                    "maximum_results": 1,
                }
                return planned(
                    hydration_tool,
                    hydration_arguments,
                    1_000 + len(completed),
                )
            return None

        if request.agent_name == "Chemical Identity and Structure Source Discovery Agent":
            groups = request.context.get("compound_identifier_groups", [])
            if not isinstance(groups, list):
                return None
            for operation_index, operation in enumerate(
                ("resolve_compound_identity_sample", "resolve_compound_synonyms"), start=1
            ):
                completed_values = completed_identifiers(operation, "sampled_identifiers")
                for group_index, group in enumerate(groups, start=1):
                    if not isinstance(group, dict):
                        continue
                    identifier_type = group.get("identifier_type")
                    identifiers = [
                        value
                        for value in group.get("identifiers", [])
                        if isinstance(value, str) and value not in completed_values
                    ]
                    if identifiers and operation in request.available_tools:
                        return planned(
                            operation,
                            {
                                "source_system": "pubchem-compound",
                                "sampled_identifiers": identifiers[:5],
                                "identifier_type": identifier_type,
                                "maximum_results": 5,
                            },
                            operation_index * 100 + group_index,
                        )
            return None

        if request.agent_name == "Supporting Metadata Discovery Agent":
            identifiers = [
                value
                for value in request.context.get("supporting_metadata_identifiers", [])
                if isinstance(value, str)
            ]
            completed_values = completed_identifiers(
                "inspect_supporting_metadata", "source_identifiers"
            )
            remaining = [value for value in identifiers if value not in completed_values]
            if remaining:
                batch_index = len(completed_values) // 5 + 1
                return planned(
                    "inspect_supporting_metadata",
                    {
                        "source_system": "ncbi-supporting-metadata",
                        "source_identifiers": remaining[:5],
                        "maximum_results": 5,
                    },
                    batch_index,
                )
        return None

    @staticmethod
    def _estimated_scientific_source_requests(request: ProviderToolRequest) -> int:
        """Bound the external reads a typed source tool can start before invocation."""

        batch_operations = {
            "fetch_activity_source_metadata",
            "inspect_activity_identifier_fields",
            "extract_activity_result_rows",
            "fetch_transcriptomic_source_metadata",
            "inspect_supporting_metadata",
            "inspect_official_file_listing",
            "inspect_linked_publications",
            "inspect_source_access",
        }
        if request.tool_name in batch_operations:
            identifiers = request.arguments.get("source_identifiers", [])
            return len(identifiers) if isinstance(identifiers, list) else 1
        non_source_tools = {
            "build_compound_mapping_manifest",
            "compare_verified_identity_fields",
            "classify_verified_record_availability",
            "audit_coverage",
        }
        return 0 if request.tool_name in non_source_tools else 1

    @staticmethod
    def _discovery_turn_policy(
        request: AgentRunRequest, history: list[dict]
    ) -> tuple[str, list[str]]:
        """Select the bounded discovery substage and expose only valid tools."""

        configured = request.context.get("stage_tool_sets")
        if not isinstance(configured, dict) or not configured:
            return "provider_default", list(request.available_tools)

        tool_history = [item for item in history if isinstance(item.get("tool_name"), str)]
        completed = [item for item in tool_history if item.get("status") == "completed"]
        if request.agent_name == "Activity Evidence Discovery Agent":
            modalities = {
                str((item.get("normalized_arguments") or {}).get("endpoint_modality"))
                for item in completed
                if item.get("tool_name") == "search_activity_sources"
                and (item.get("normalized_arguments") or {}).get("endpoint_modality")
            }
            required = {str(item) for item in request.context.get("candidate_modalities", [])} or {
                "binding",
                "agonism",
                "antagonism",
            }
            if modalities != required:
                substage = "candidate_search"
            else:
                substage = "final_output"
            configured_tools = configured.get(substage, [])
            if substage == "candidate_search":
                configured_tools = ["search_activity_sources"]
            return substage, [
                name for name in configured_tools if name in set(request.available_tools)
            ]

        if request.agent_name == "Transcriptomic Evidence Discovery Agent":
            # Compound search and candidate hydration are deterministic orchestration. The
            # provider receives only the compact persisted results for its final bounded review;
            # it cannot fall back to a target-first or model-invented scientific query.
            return "final_output", []

        if request.agent_name == "Chemical Identity and Structure Source Discovery Agent":
            substage = "identity_inspection" if not completed else "final_output"
            configured_tools = configured.get(substage, [])
            return substage, [
                name for name in configured_tools if name in set(request.available_tools)
            ]

        if request.agent_name == "Supporting Metadata Discovery Agent":
            substage = "metadata_inspection" if not completed else "final_output"
            configured_tools = configured.get(substage, [])
            return substage, [
                name for name in configured_tools if name in set(request.available_tools)
            ]

        substage = "search_planning"
        if tool_history:
            latest = tool_history[-1]
            tool_name = latest["tool_name"]
            output = latest.get("output") if isinstance(latest.get("output"), dict) else {}
            if tool_name == "search_geo_series":
                result_count = int(
                    output.get("new_accession_count", output.get("result_count", 0)) or 0
                )
                substage = "candidate_validation" if result_count > 0 else "final_output"
            elif tool_name == "validate_geo_accessions":
                substage = (
                    "candidate_inspection"
                    if int(output.get("public_valid_count", 0) or 0) > 0
                    else "final_output"
                )
            elif tool_name == "inspect_geo_candidates":
                substage = (
                    "final_comparison"
                    if int(output.get("inspected_count", 0) or 0) > 0
                    else "final_output"
                )
            elif tool_name in {"compare_dataset_candidates", "fetch_publication_metadata"}:
                substage = "final_output"

        configured_tools = configured.get(substage, [])
        if not isinstance(configured_tools, list):
            configured_tools = []
        allowed = set(request.available_tools)
        return substage, [name for name in configured_tools if name in allowed]

    @staticmethod
    def _context_component_estimates(provider, request, history) -> dict[str, int]:
        """Return safe approximate token counts without persisting model reasoning."""

        estimator = getattr(provider, "estimate_context_components", None)
        if callable(estimator):
            estimates = estimator(request, list(history))
            if isinstance(estimates, dict) and all(
                isinstance(key, str) and isinstance(value, int) and value >= 0
                for key, value in estimates.items()
            ):
                return estimates
        endpoint = {
            "endpoint_name": request.context.get("endpoint_name"),
            "biological_goal": request.context.get("biological_goal"),
        }

        def estimate(value: Any) -> int:
            return max(1, (len(canonical_json(redact(value))) + 3) // 4)

        return {
            "system_instructions": estimate({"sha256_only": True}),
            "endpoint_definition": estimate(endpoint),
            "exposed_tool_schemas": estimate(request.available_tools),
            "conversation_history": estimate(
                [item.get("discovery_substage") for item in history[-6:]]
            ),
            "tool_results": estimate(history[-6:]),
            "structured_output_schema": estimate(request.output_schema_name),
        }

    @staticmethod
    def _add_usage(left: UsageReport, right: UsageReport) -> UsageReport:
        if (
            left.provider_invocations == 0
            and left.input_tokens == 0
            and left.output_tokens == 0
            and left.cached_tokens == 0
            and left.cost_cents == 0
            and not left.provider_request_ids
            and not left.provider_response_ids
        ):
            return right.model_copy(deep=True)
        statuses = {left.usage_status, right.usage_status}
        if "usage_partial" in statuses or statuses == {"usage_recorded", "usage_unavailable"}:
            usage_status = "usage_partial"
        elif "usage_recorded" in statuses:
            usage_status = "usage_recorded"
        else:
            usage_status = "usage_unavailable"
        return UsageReport(
            usage_status=usage_status,
            input_tokens=left.input_tokens + right.input_tokens,
            output_tokens=left.output_tokens + right.output_tokens,
            cached_tokens=left.cached_tokens + right.cached_tokens,
            cost_cents=left.cost_cents + right.cost_cents,
            provider_request_ids=left.provider_request_ids + right.provider_request_ids,
            provider_response_ids=left.provider_response_ids + right.provider_response_ids,
            provider_invocations=left.provider_invocations + right.provider_invocations,
        )

    @staticmethod
    def _budget_error(request: AgentRunRequest, usage: UsageReport) -> str | None:
        if usage.input_tokens > request.budget.maximum_input_tokens:
            return "input_token_budget_exceeded"
        if usage.output_tokens > request.budget.maximum_output_tokens:
            return "output_token_budget_exceeded"
        if usage.cost_cents > request.budget.maximum_cost_cents:
            return "cost_budget_exceeded"
        return None

    @staticmethod
    def _estimated_next_turn_budget_error(
        request: AgentRunRequest,
        usage: UsageReport,
        last_turn_usage: UsageReport | None,
        *,
        estimated_next_input: int | None = None,
    ) -> tuple[str, dict[str, float | int]] | None:
        """Use the last measured turn as a conservative estimate for the next model turn."""

        if last_turn_usage is None:
            return None
        estimates = (
            (
                "input_token_budget_precheck",
                usage.input_tokens,
                (
                    estimated_next_input
                    if estimated_next_input is not None
                    else last_turn_usage.input_tokens
                ),
                request.budget.maximum_input_tokens,
            ),
            (
                "output_token_budget_precheck",
                usage.output_tokens,
                last_turn_usage.output_tokens,
                request.budget.maximum_output_tokens,
            ),
            (
                "cost_budget_precheck",
                usage.cost_cents,
                last_turn_usage.cost_cents,
                request.budget.maximum_cost_cents,
            ),
        )
        for code, consumed, estimated_next, maximum in estimates:
            if estimated_next > 0 and consumed + estimated_next > maximum:
                return code, {
                    "consumed": consumed,
                    "estimated_next_turn": estimated_next,
                    "configured_maximum": maximum,
                }
        return None

    @staticmethod
    def _budget_message(code: str) -> str:
        if code.startswith(("input_token_", "output_token_")):
            return "Live agent run stopped at the configured cumulative token budget."
        if code.startswith("cost_"):
            return "Live agent run stopped at the configured cumulative cost budget."
        return "Agent run exceeded its token or cost budget."

    @staticmethod
    def _failure(
        status,
        code,
        message,
        trace,
        usage,
        turns,
        tool_calls,
        started,
        *,
        retryable: bool | None = None,
        category: str = "agent_runtime",
        source_diagnostic=None,
        tool_diagnostic=None,
    ):
        return AgentRunResult(
            status=status,
            usage=usage,
            trace=trace,
            error=NormalizedAgentError(
                code=code,
                safe_message=message,
                retryable=(
                    retryable
                    if retryable is not None
                    else code in {"provider_timeout", "provider_failure", "tool_timeout"}
                ),
                category=category,
                source_diagnostic=source_diagnostic,
                tool_diagnostic=tool_diagnostic,
            ),
            turns=turns,
            tool_calls=tool_calls,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
