"""Controlled provider runtime: budgets, typed tools, traces, retries, and persistence."""

from __future__ import annotations

import hashlib
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
    require_build,
    utc_text,
    versioned_payload,
)
from .tools import ToolRegistry

SECRET_KEYS = {"authorization", "api_key", "apikey", "password", "secret", "token"}


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
        safe_request = redact(request.model_dump(mode="json"))
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

        emit("agent_run.started", "running", provider=provider.name)
        retries = 0
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
            turns += 1
            emit("provider.turn.started", "running", turn=turns)
            try:
                turn = self._provider_turn(
                    provider,
                    request,
                    history,
                    interruption_request(),
                    timeout=max(0.01, request.budget.timeout_seconds - elapsed),
                )
            except (ProviderTimeout, FutureTimeout):
                emit("provider.turn.failed", "timed_out", turn=turns)
                result = self._failure(
                    AgentRunStatus.TIMED_OUT,
                    "provider_timeout",
                    "Provider exceeded the configured timeout.",
                    trace,
                    usage,
                    turns,
                    tool_calls,
                    started,
                )
                break
            except ProviderFailure as exc:
                emit("provider.turn.failed", "failed", turn=turns, retryable=exc.retryable)
                if exc.retryable and retries < request.budget.retry_count:
                    retries += 1
                    emit("provider.retry", "retrying", retry=retries)
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
                )
                break
            usage = self._add_usage(usage, turn.usage)
            emit("provider.turn.completed", "completed", turn=turns, kind=turn.kind)
            budget_error = self._budget_error(request, usage)
            if budget_error:
                result = self._failure(
                    AgentRunStatus.BUDGET_EXCEEDED,
                    budget_error,
                    "Agent run exceeded its token or cost budget.",
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
                if requested.tool_name not in request.available_tools:
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
                        run_id, request, requested, tool_result, tool_calls
                    )
                else:
                    call_id, tool_result = self._invoke_tool(run_id, request, requested, tool_calls)
                emit(
                    "tool_call.completed",
                    tool_result.status.value,
                    tool_name=requested.tool_name,
                    tool_call_id=call_id,
                    replayed=tool_result.replayed,
                )
                history.append(
                    {
                        "tool_name": requested.tool_name,
                        "status": tool_result.status.value,
                        "output": tool_result.output,
                        "error": tool_result.error.model_dump(mode="json")
                        if tool_result.error
                        else None,
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
                        detail_json=canonical_json(versioned_payload(agent_run_id=run_id)),
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
                stored_arguments = prior.arguments_json
                if stored_arguments != canonical_json(versioned_payload(arguments=arguments)):
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
        with self.database.session() as session:
            row = session.get(ToolCallRow, call_id)
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
    def _failure(status, code, message, trace, usage, turns, tool_calls, started):
        return AgentRunResult(
            status=status,
            usage=usage,
            trace=trace,
            error=NormalizedAgentError(
                code=code,
                safe_message=message,
                retryable=code in {"provider_timeout", "provider_failure", "tool_timeout"},
                category="agent_runtime",
            ),
            turns=turns,
            tool_calls=tool_calls,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
