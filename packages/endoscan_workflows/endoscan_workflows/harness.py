"""Controlled provider runtime: budgets, typed tools, traces, retries, and persistence."""

from __future__ import annotations

import hashlib
import logging
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
    ToolCallStatus,
    ToolInvocation,
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
            discovery_substage, exposed_tools = self._discovery_turn_policy(request, history)
            turn_request = request.model_copy(
                update={
                    "available_tools": exposed_tools,
                    "context": {
                        **request.context,
                        "discovery_substage": discovery_substage,
                        "tools_exposed": exposed_tools,
                        "tool_budget_remaining": max(
                            0, request.budget.maximum_tool_calls - tool_calls
                        ),
                    },
                }
            )
            context_components = self._context_component_estimates(provider, turn_request, history)
            component_estimate = sum(context_components.values())
            estimated_next_input = max(
                component_estimate,
                last_turn_usage.input_tokens if last_turn_usage else 0,
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
            provider_invocations += 1
            emit(
                "provider.turn.started",
                "running",
                turn=turn_number,
                provider_invocation=provider_invocations,
                retry_attempt=retries,
                discovery_substage=discovery_substage,
                tools_exposed=exposed_tools,
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
            emit(
                "provider.turn.completed",
                "completed",
                turn=turns,
                provider_invocation=provider_invocations,
                kind=turn.kind,
                discovery_substage=discovery_substage,
                tools_exposed=exposed_tools,
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
                tool_calls += 1
                requested = turn.tool_request
                if requested.tool_name not in turn_request.available_tools:
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
                        run_id, turn_request, requested, tool_calls
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
                    source_diagnostics=source_diagnostics,
                )
                history.append(
                    {
                        "tool_name": requested.tool_name,
                        "status": tool_result.status.value,
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
            update={"trace": trace, "duration_ms": int((time.monotonic() - started) * 1000)}
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

    def _invoke_tool(self, run_id, request, tool_request, ordinal) -> tuple[str, ToolResult]:
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
                    "refresh_source_metadata": bool(
                        request.context.get("refresh_source_metadata", False)
                    ),
                },
                idempotency_key=key,
            )
        )
        original_arguments = redact(result.original_arguments or tool_request.arguments)
        normalized_arguments = redact(result.normalized_arguments or tool_request.arguments)
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
    def _discovery_turn_policy(
        request: AgentRunRequest, history: list[dict]
    ) -> tuple[str, list[str]]:
        """Select the bounded discovery substage and expose only valid tools."""

        configured = request.context.get("stage_tool_sets")
        if not isinstance(configured, dict) or not configured:
            return "provider_default", list(request.available_tools)

        tool_history = [item for item in history if isinstance(item.get("tool_name"), str)]
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
        return UsageReport(
            input_tokens=left.input_tokens + right.input_tokens,
            output_tokens=left.output_tokens + right.output_tokens,
            cached_tokens=left.cached_tokens + right.cached_tokens,
            cost_cents=left.cost_cents + right.cost_cents,
            provider_request_ids=left.provider_request_ids + right.provider_request_ids,
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
            ),
            turns=turns,
            tool_calls=tool_calls,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
