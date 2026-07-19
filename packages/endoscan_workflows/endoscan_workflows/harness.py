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
                result = self._failureëù¶‰žËkºwµçAÉ¥½È¹ÍÑ…ÑÕÌ€„ôQ½½±…±±MÑ…ÑÕÌ¹=5A1Q¹Ù…±Õ”½È¹½ÐÁÉ¥½È¹É•ÍÕ±Ñ}©Í½¸è(€€€€€€€€€€€€€€€€€€€É…¥Í”]½É­™±½Ý½¹™±¥Ð ‰AÉ¥½È±½¥…°Ñ½½°…±°‘¥¹½Ð½µÁ±•Ñ”ÍÕ•ÍÍ™Õ±±ä¸ˆ¤(€€€€€€€€€€€€€€€É•Á±…ä€ôQ½½±I•ÍÕ±Ð¹µ½‘•±}Ù…±¥‘…Ñ•}©Í½¸¡ÁÉ¥½È¹É•ÍÕ±Ñ}©Í½¸¤¹µ½‘•±}½Áä (€€€€€€€€€€€€€€€€€€€ÕÁ‘…Ñ”õì‰É•Á±…å•ˆèQÉÕ•ô(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€É•ÑÕÉ¸ÁÉ¥½È¹¥°É•Á±…ä(€€€€€€€€€€€…±±}¥€ô‘•Ñ•Éµ¥¹¥ÍÑ¥}¥ ‰Ñ½½°ˆ°É•ÅÕ•ÍÐ¹Ý½É­™±½Ý}¥°Ñ½½±}É•ÅÕ•ÍÐ¹Ñ½½±}¹…µ”°­•ä¤(€€€€€€€€€€€É½Ü€ôQ½½±…±±I½Ü (€€€€€€€€€€€€€€€¥õ…±±}¥°(€€€€€€€€€€€€€€€Ý½É­™±½Ý}¥õÉ•ÅÕ•ÍÐ¹Ý½É­™±½Ý}¥°(€€€€€€€€€€€€€€€…•¹Ñ}ÉÕ¹}¥õÉÕ¹}¥°(€€€€€€€€€€€€€€€Ñ½½±}¹…µ”õÑ½½±}É•ÅÕ•ÍÐ¹Ñ½½±}¹…µ”°(€€€€€€€€€€€€€€€Ñ½½±}Ù•ÉÍ¥½¸õÑ½½°¹‘•™¥¹¥Ñ¥½¸¹¥µÁ±•µ•¹Ñ…Ñ¥½¹}Ù•ÉÍ¥½¸°(€€€€€€€€€€€€€€€Á•Éµ¥ÍÍ¥½¹}Í½Á•}©Í½¸õ…¹½¹¥…±}©Í½¸ (€€€€€€€€€€€€€€€€€€€Ù•ÉÍ¥½¹•‘}Á…å±½…¡Á•Éµ¥ÍÍ¥½¹ÌõÉ•ÅÕ•ÍÐ¹½¹Ñ•áÐ¹•Ð ‰Á•Éµ¥ÍÍ¥½¹}Í½Á”ˆ°mt¤¤(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€…ÉÕµ•¹ÑÍ}©Í½¸õ…¹½¹¥…±}©Í½¸¡Ù•ÉÍ¥½¹•‘}Á…å±½…¡…ÉÕµ•¹ÑÌõ…ÉÕµ•¹ÑÌ¤¤°(€€€€€€€€€€€€€€€É•ÍÕ±Ñ}©Í½¸õ9½¹”°(€€€€€€€€€€€€€€€ÍÑ…ÑÕÌõQ½½±…±±MÑ…ÑÕÌ¹IU99%9¹Ù…±Õ”°(€€€€€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äõ­•ä°(€€€€€€€€€€€€€€€‘ÕÉ…Ñ¥½¹}µÌôÀ°(€€€€€€€€€€€€€€€É•…Ñ•‘}…ÐõÕÑ}Ñ•áÐ ¤°(€€€€€€€€€€€€€€€½µÁ±•Ñ•‘}…Ðõ9½¹”°(€€€€€€€€€€€€¤(€€€€€€€€€€€Í•ÍÍ¥½¸¹…‘¡É½Ü¤(€€€€€€€€€€€‰Õ¥±€ôÉ•ÅÕ¥É•}‰Õ¥±¡Í•ÍÍ¥½¸°É•ÅÕ•ÍÐ¹Ý½É­™±½Ý}¥¤(€€€€€€€€€€€…ÁÁ•¹‘}•Ù•¹Ð (€€€€€€€€€€€€€€€Í•ÍÍ¥½¸°(€€€€€€€€€€€€€€€‰Õ¥±°(€€€€€€€€€€€€€€€•Ù•¹Ñ}ÑåÁ”ô‰Ñ½½±}…±°¹ÍÑ…ÉÑ•ˆ°(€€€€€€€€€€€€€€€…Ñ½É}ÑåÁ”ô‰Ñ½½°ˆ°(€€€€€€€€€€€€€€€…Ñ½É}¥õÑ½½±}É•ÅÕ•ÍÐ¹Ñ½½±}¹…µ”°(€€€€€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äõ˜‰Ñ½½°µ…±°éí…±±}¥‘ôéÍÑ…ÉÑ•ˆ°(€€€€€€€€€€€€€€€Á…å±½…õì(€€€€€€€€€€€€€€€€€€€€‰Ñ½½±}…±±}¥ˆè…±±}¥°(€€€€€€€€€€€€€€€€€€€€‰…•¹Ñ}ÉÕ¹}¥ˆèÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€€‰Ñ½½°ˆèÑ½½±}É•ÅÕ•ÍÐ¹Ñ½½±}¹…µ”°(€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€€€€™É½µ}ÍÑ…Ñ”õ‰Õ¥±¹ÕÉÉ•¹Ñ}ÍÑ…”°(€€€€€€€€€€€€€€€Ñ½}ÍÑ…Ñ”õ‰Õ¥±¹ÕÉÉ•¹Ñ}ÍÑ…”°(€€€€€€€€€€€€¤(€€€€€€€É•ÍÕ±Ð€ôÍ•±˜¹Ñ½½±Ì¹¥¹Ù½­” (€€€€€€€€€€€Q½½±%¹Ù½…Ñ¥½¸ (€€€€€€€€€€€€€€€Ñ½½±}¹…µ”õÑ½½±}É•ÅÕ•ÍÐ¹Ñ½½±}¹…µ”°(€€€€€€€€€€€€€€€…ÉÕµ•¹ÑÌõÑ½½±}É•ÅÕ•ÍÐ¹…ÉÕµ•¹ÑÌ°(€€€€€€€€€€€€€€€Ý½É­™±½Ý}¥õÉ•ÅÕ•ÍÐ¹Ý½É­™±½Ý}¥°(€€€€€€€€€€€€€€€ÍÑ•Á}¥õÉ•ÅÕ•ÍÐ¹ÍÑ•Á}¥°(€€€€€€€€€€€€€€€Ý½É­™±½Ý}ÍÑ…”õ]½É­™±½ÝMÑ…Ñ”¡É•ÅÕ•ÍÐ¹½¹Ñ•áÑl‰Ý½É­™±½Ý}ÍÑ…”‰t¤°(€€€€€€€€€€€€€€€Á•Éµ¥ÍÍ¥½¹}Í½Á”õÉ•ÅÕ•ÍÐ¹½¹Ñ•áÐ¹•Ð ‰Á•Éµ¥ÍÍ¥½¹}Í½Á”ˆ°mt¤°(€€€€€€€€€€€€€€€ÉÕ¹}½¹Ñ•áÐõì(€€€€€€€€€€€€€€€€€€€€‰ÉÕ¹}µ½‘”ˆèÉ•ÅÕ•ÍÐ¹½¹Ñ•áÐ¹•Ð ‰ÉÕ¹}µ½‘”ˆ°€‰É•Á±…äˆ¤°(€€€€€€€€€€€€€€€€€€€€‰É•™É•Í¡}Í½ÕÉ•}µ•Ñ…‘…Ñ„ˆè‰½½° (€€€€€€€€€€€€€€€€€€€€€€€É•ÅÕ•ÍÐ¹½¹Ñ•áÐ¹•Ð ‰É•™É•Í¡}Í½ÕÉ•}µ•Ñ…‘…Ñ„ˆ°…±Í”¤(€€€€€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äõ­•ä°(€€€€€€€€€€€€¤(€€€€€€€€¤(€€€€€€€½É¥¥¹…±}…ÉÕµ•¹ÑÌ€ôÉ•‘…Ð¡É•ÍÕ±Ð¹½É¥¥¹…±}…ÉÕµ•¹ÑÌ½ÈÑ½½±}É•ÅÕ•ÍÐ¹…ÉÕµ•¹ÑÌ¤(€€€€€€€¹½Éµ…±¥é•‘}…ÉÕµ•¹ÑÌ€ôÉ•‘…Ð¡É•ÍÕ±Ð¹¹½Éµ…±¥é•‘}…ÉÕµ•¹ÑÌ½ÈÑ½½±}É•ÅÕ•ÍÐ¹…ÉÕµ•¹ÑÌ¤(€€€€€€€¹½Éµ…±¥é…Ñ¥½¹}Ý…É¹¥¹Ì€ôl(€€€€€€€€€€€Ý…É¹¥¹œ¹µ½‘•±}‘ÕµÀ¡µ½‘”ô‰©Í½¸ˆ¤™½ÈÝ…É¹¥¹œ¥¸É•ÍÕ±Ð¹¹½Éµ…±¥é…Ñ¥½¹}Ý…É¹¥¹Ì(€€€€€€€t(€€€€€€€É•ÍÕ±Ð€ôÉ•ÍÕ±Ð¹µ½‘•±}½Áä (€€€€€€€€€€€ÕÁ‘…Ñ”õì(€€€€€€€€€€€€€€€€‰½É¥¥¹…±}…ÉÕµ•¹ÑÌˆè½É¥¥¹…±}…ÉÕµ•¹ÑÌ°(€€€€€€€€€€€€€€€€‰¹½Éµ…±¥é•‘}…ÉÕµ•¹ÑÌˆè¹½Éµ…±¥é•‘}…ÉÕµ•¹ÑÌ°(€€€€€€€€€€€ô(€€€€€€€€¤(€€€€€€€Ý¥Ñ Í•±˜¹‘…Ñ…‰…Í”¹Í•ÍÍ¥½¸ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€É½Ü€ôÍ•ÍÍ¥½¸¹•Ð¡Q½½±…±±I½Ü°…±±}¥¤(€€€€€€€€€€€É½Ü¹…ÉÕµ•¹ÑÍ}©Í½¸€ô…¹½¹¥…±}©Í½¸ (€€€€€€€€€€€€€€€Ù•ÉÍ¥½¹•‘}Á…å±½… (€€€€€€€€€€€€€€€€€€€…ÉÕµ•¹ÑÌõ…ÉÕµ•¹ÑÌ°(€€€€€€€€€€€€€€€€€€€½É¥¥¹…±}…ÉÕµ•¹ÑÌõ½É¥¥¹…±}…ÉÕµ•¹ÑÌ°(€€€€€€€€€€€€€€€€€€€¹½Éµ…±¥é•‘}…ÉÕµ•¹ÑÌõ¹½Éµ…±¥é•‘}…ÉÕµ•¹ÑÌ°(€€€€€€€€€€€€€€€€€€€¹½Éµ…±¥é…Ñ¥½¹}Ý…É¹¥¹Ìõ¹½Éµ…±¥é…Ñ¥½¹}Ý…É¹¥¹Ì°(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€¤(€€€€€€€€€€€É½Ü¹É•ÍÕ±Ñ}©Í½¸€ôÉ•ÍÕ±Ð¹µ½‘•±}‘ÕµÁ}©Í½¸ ¤(€€€€€€€€€€€É½Ü¹ÍÑ…ÑÕÌ€ôÉ•ÍÕ±Ð¹ÍÑ…ÑÕÌ¹Ù…±Õ”(€€€€€€€€€€€É½Ü¹‘ÕÉ…Ñ¥½¹}µÌ€ôÉ•ÍÕ±Ð¹‘ÕÉ…Ñ¥½¹}µÌ(€€€€€€€€€€€É½Ü¹½µÁ±•Ñ•‘}…Ð€ôÕÑ}Ñ•áÐ ¤(€€€€€€€€€€€‰Õ¥±€ôÉ•ÅÕ¥É•}‰Õ¥±¡Í•ÍÍ¥½¸°É•ÅÕ•ÍÐ¹Ý½É­™±½Ý}¥¤(€€€€€€€€€€€…ÁÁ•¹‘}•Ù•¹Ð (€€€€€€€€€€€€€€€Í•ÍÍ¥½¸°(€€€€€€€€€€€€€€€‰Õ¥±°(€€€€€€€€€€€€€€€•Ù•¹Ñ}ÑåÁ”ô‰Ñ½½±}…±°¹™¥¹¥Í¡•ˆ°(€€€€€€€€€€€€€€€…Ñ½É}ÑåÁ”ô‰Ñ½½°ˆ°(€€€€€€€€€€€€€€€…Ñ½É}¥õÑ½½±}É•ÅÕ•ÍÐ¹Ñ½½±}¹…µ”°(€€€€€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äõ˜‰Ñ½½°µ…±°éí…±±}¥‘ôé™¥¹¥Í¡•ˆ°(€€€€€€€€€€€€€€€Á…å±½…õì(€€€€€€€€€€€€€€€€€€€€‰Ñ½½±}…±±}¥ˆè…±±}¥°(€€€€€€€€€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆèÉ•ÍÕ±Ð¹ÍÑ…ÑÕÌ¹Ù…±Õ”°(€€€€€€€€€€€€€€€€€€€€‰‘ÕÉ…Ñ¥½¹}µÌˆèÉ•ÍÕ±Ð¹‘ÕÉ…Ñ¥½¹}µÌ°(€€€€€€€€€€€€€€€€€€€€‰¹½Éµ…±¥é…Ñ¥½¹}Ý…É¹¥¹}½Õ¹Ðˆè±•¸¡¹½Éµ…±¥é…Ñ¥½¹}Ý…É¹¥¹Ì¤°(€€€€€€€€€€€€€€€€€€€€‰¹½Éµ…±¥é…Ñ¥½¹}Ý…É¹¥¹}½‘•Ìˆèl(€€€€€€€€€€€€€€€€€€€€€€€Ý…É¹¥¹l‰½‘”‰t™½ÈÝ…É¹¥¹œ¥¸¹½Éµ…±¥é…Ñ¥½¹}Ý…É¹¥¹Ì(€€€€€€€€€€€€€€€€€€€t°(€€€€€€€€€€€€€€€€€€€€‰Í½ÕÉ•}‘¥…¹½ÍÑ¥Œˆè€ (€€€€€€€€€€€€€€€€€€€€€€€É•ÍÕ±Ð¹Í½ÕÉ•}‘¥…¹½ÍÑ¥Œ¹µ½‘•±}‘ÕµÀ¡µ½‘”ô‰©Í½¸ˆ¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜É•ÍÕ±Ð¹Í½ÕÉ•}‘¥…¹½ÍÑ¥Œ(€€€€€€€€€€€€€€€€€€€€€€€•±Í”9½¹”(€€€€€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€€€€€€‰Í½ÕÉ•}‘¥…¹½ÍÑ¥Ìˆèl(€€€€€€€€€€€€€€€€€€€€€€€¥Ñ•´¹•Ð ‰Í½ÕÉ•}‘¥…¹½ÍÑ¥Œˆ¤(€€€€€€€€€€€€€€€€€€€€€€€™½È¥Ñ•´¥¸€¡É•ÍÕ±Ð¹½ÕÑÁÕÐ½Èíô¤¹•Ð ‰É•ÍÕ±ÑÌˆ°mt¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡¥Ñ•´°‘¥Ð¤(€€€€€€€€€€€€€€€€€€€€€€€…¹¥Í¥¹ÍÑ…¹”¡¥Ñ•´¹•Ð ‰Í½ÕÉ•}‘¥…¹½ÍÑ¥Œˆ¤°‘¥Ð¤(€€€€€€€€€€€€€€€€€€€t°(€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€€€€™É½µ}ÍÑ…Ñ”õ‰Õ¥±¹ÕÉÉ•¹Ñ}ÍÑ…”°(€€€€€€€€€€€€€€€Ñ½}ÍÑ…Ñ”õ‰Õ¥±¹ÕÉÉ•¹Ñ}ÍÑ…”°(€€€€€€€€€€€€¤(€€€€€€€É•ÑÕÉ¸…±±}¥°É•ÍÕ±Ð((€€€‘•˜}É•½É‘}ÁÉ½¡¥‰¥Ñ•‘}…±°¡Í•±˜°ÉÕ¹}¥°É•ÅÕ•ÍÐ°Ñ½½±}É•ÅÕ•ÍÐ°É•ÍÕ±Ð°½É‘¥¹…°¤€´øÍÑÈè(€€€€€€€­•ä€ôÑ½½±}É•ÅÕ•ÍÐ¹¥‘•µÁ½Ñ•¹å}­•ä½È˜‰ÑÕÉ¸µí½É‘¥¹…±ôˆ(€€€€€€€…±±}¥€ô‘•Ñ•Éµ¥¹¥ÍÑ¥}¥ ‰Ñ½½°ˆ°É•ÅÕ•ÍÐ¹Ý½É­™±½Ý}¥°Ñ½½±}É•ÅÕ•ÍÐ¹Ñ½½±}¹…µ”°­•ä¤(€€€€€€€Ý¥Ñ Í•±˜¹‘…Ñ…‰…Í”¹Í•ÍÍ¥½¸ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€É½Ü€ôQ½½±…±±I½Ü (€€€€€€€€€€€€€€€¥õ…±±}¥°(€€€€€€€€€€€€€€€Ý½É­™±½Ý}¥õÉ•ÅÕ•ÍÐ¹Ý½É­™±½Ý}¥°(€€€€€€€€€€€€€€€…•¹Ñ}ÉÕ¹}¥õÉÕ¹}¥°(€€€€€€€€€€€€€€€Ñ½½±}¹…µ”õÑ½½±}É•ÅÕ•ÍÐ¹Ñ½½±}¹…µ”°(€€€€€€€€€€€€€€€Ñ½½±}Ù•ÉÍ¥½¸ô‰Õ¹É•¥ÍÑ•É•ˆ°(€€€€€€€€€€€€€€€Á•Éµ¥ÍÍ¥½¹}Í½Á•}©Í½¸õ…¹½¹¥…±}©Í½¸¡Ù•ÉÍ¥½¹•‘}Á…å±½…¡Á•Éµ¥ÍÍ¥½¹Ìõmt¤¤°(€€€€€€€€€€€€€€€…ÉÕµ•¹ÑÍ}©Í½¸õ…¹½¹¥…±}©Í½¸ (€€€€€€€€€€€€€€€€€€€Ù•ÉÍ¥½¹•‘}Á…å±½…¡…ÉÕµ•¹ÑÌõÉ•‘…Ð¡Ñ½½±}É•ÅÕ•ÍÐ¹…ÉÕµ•¹ÑÌ¤¤(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€É•ÍÕ±Ñ}©Í½¸õÉ•ÍÕ±Ð¹µ½‘•±}‘ÕµÁ}©Í½¸ ¤°(€€€€€€€€€€€€€€€ÍÑ…ÑÕÌõQ½½±…±±MÑ…ÑÕÌ¹AI=!%	%Q¹Ù…±Õ”°(€€€€€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äõ­•ä°(€€€€€€€€€€€€€€€‘ÕÉ…Ñ¥½¹}µÌôÀ°(€€€€€€€€€€€€€€€É•…Ñ•‘}…ÐõÕÑ}Ñ•áÐ ¤°(€€€€€€€€€€€€€€€½µÁ±•Ñ•‘}…ÐõÕÑ}Ñ•áÐ ¤°(€€€€€€€€€€€€¤(€€€€€€€€€€€Í•ÍÍ¥½¸¹…‘¡É½Ü¤(€€€€€€€€€€€‰Õ¥±€ôÉ•ÅÕ¥É•}‰Õ¥±¡Í•ÍÍ¥½¸°É•ÅÕ•ÍÐ¹Ý½É­™±½Ý}¥¤(€€€€€€€€€€€…ÁÁ•¹‘}•Ù•¹Ð (€€€€€€€€€€€€€€€Í•ÍÍ¥½¸°(€€€€€€€€€€€€€€€‰Õ¥±°(€€€€€€€€€€€€€€€•Ù•¹Ñ}ÑåÁ”ô‰Ñ½½±}…±°¹ÁÉ½¡¥‰¥Ñ•ˆ°(€€€€€€€€€€€€€€€…Ñ½É}ÑåÁ”ô‰Ñ½½°ˆ°(€€€€€€€€€€€€€€€…Ñ½É}¥õÑ½½±}É•ÅÕ•ÍÐ¹Ñ½½±}¹…µ”°(€€€€€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äõ˜‰Ñ½½°µ…±°éí…±±}¥‘ôéÁÉ½¡¥‰¥Ñ•ˆ°(€€€€€€€€€€€€€€€Á…å±½…õì‰Ñ½½±}…±±}¥ˆè…±±}¥°€‰Ñ½½°ˆèÑ½½±}É•ÅÕ•ÍÐ¹Ñ½½±}¹…µ•ô°(€€€€€€€€€€€€€€€™É½µ}ÍÑ…Ñ”õ‰Õ¥±¹ÕÉÉ•¹Ñ}ÍÑ…”°(€€€€€€€€€€€€€€€Ñ½}ÍÑ…Ñ”õ‰Õ¥±¹ÕÉÉ•¹Ñ}ÍÑ…”°(€€€€€€€€€€€€¤(€€€€€€€É•ÑÕÉ¸…±±}¥((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜}‘¥Í½Ù•Éå}ÑÕÉ¹}Á½±¥ä (€€€€€€€É•ÅÕ•ÍÐè•¹ÑIÕ¹I•ÅÕ•ÍÐ°¡¥ÍÑ½Éäè±¥ÍÑm‘¥Ñt(€€€€¤€´øÑÕÁ±•mÍÑÈ°±¥ÍÑmÍÑÉutè(€€€€€€€€ˆˆ‰M•±•ÐÑ¡”‰½Õ¹‘•‘¥Í½Ù•ÉäÍÕ‰ÍÑ…”…¹•áÁ½Í”½¹±äÙ…±¥Ñ½½±Ì¸ˆˆˆ((€€€€€€€½¹™¥ÕÉ•€ôÉ•ÅÕ•ÍÐ¹½¹Ñ•áÐ¹•Ð ‰ÍÑ…•}Ñ½½±}Í•ÑÌˆ¤(€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡½¹™¥ÕÉ•°‘¥Ð¤½È¹½Ð½¹™¥ÕÉ•è(€€€€€€€€€€€É•ÑÕÉ¸€‰ÁÉ½Ù¥‘•É}‘•™…Õ±Ðˆ°±¥ÍÐ¡É•ÅÕ•ÍÐ¹…Ù…¥±…‰±•}Ñ½½±Ì¤((€€€€€€€Ñ½½±}¡¥ÍÑ½Éä€ôm¥Ñ•´™½È¥Ñ•´¥¸¡¥ÍÑ½Éä¥˜¥Í¥¹ÍÑ…¹”¡¥Ñ•´¹•Ð ‰Ñ½½±}¹…µ”ˆ¤°ÍÑÈ¥t(€€€€€€€ÍÕ‰ÍÑ…”€ô€‰Í•…É¡}Á±…¹¹¥¹œˆ(€€€€€€€¥˜Ñ½½±}¡¥ÍÑ½Éäè(€€€€€€€€€€€±…Ñ•ÍÐ€ôÑ½½±}¡¥ÍÑ½Éål´Åt(€€€€€€€€€€€Ñ½½±}¹…µ”€ô±…Ñ•ÍÑl‰Ñ½½±}¹…µ”‰t(€€€€€€€€€€€½ÕÑÁÕÐ€ô±…Ñ•ÍÐ¹•Ð ‰½ÕÑÁÕÐˆ¤¥˜¥Í¥¹ÍÑ…¹”¡±…Ñ•ÍÐ¹•Ð ‰½ÕÑÁÕÐˆ¤°‘¥Ð¤•±Í”íô(€€€€€€€€€€€¥˜Ñ½½±}¹…µ”€ôô€‰Í•…É¡}•½}Í•É¥•Ìˆè(€€€€€€€€€€€€€€€É•ÍÕ±Ñ}½Õ¹Ð€ô¥¹Ð (€€€€€€€€€€€€€€€€€€€½ÕÑÁÕÐ¹•Ð ‰¹•Ý}…•ÍÍ¥½¹}½Õ¹Ðˆ°½ÕÑÁÕÐ¹•Ð ‰É•ÍÕ±Ñ}½Õ¹Ðˆ°€À¤¤½È€À(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€ÍÕ‰ÍÑ…”€ô€‰…¹‘¥‘…Ñ•}Ù…±¥‘…Ñ¥½¸ˆ¥˜É•ÍÕ±Ñ}½Õ¹Ð€ø€À•±Í”€‰™¥¹…±}½ÕÑÁÕÐˆ(€€€€€€€€€€€•±¥˜Ñ½½±}¹…µ”€ôô€‰Ù…±¥‘…Ñ•}•½}…•ÍÍ¥½¹Ìˆè(€€€€€€€€€€€€€€€ÍÕ‰ÍÑ…”€ô€ (€€€€€€€€€€€€€€€€€€€€‰…¹‘¥‘…Ñ•}¥¹ÍÁ•Ñ¥½¸ˆ(€€€€€€€€€€€€€€€€€€€¥˜¥¹Ð¡½ÕÑÁÕÐ¹•Ð ‰ÁÕ‰±¥}Ù…±¥‘}½Õ¹Ðˆ°€À¤½È€À¤€ø€À(€€€€€€€€€€€€€€€€€€€•±Í”€‰™¥¹…±}½ÕÑÁÕÐˆ(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€•±¥˜Ñ½½±}¹…µ”€ôô€‰¥¹ÍÁ•Ñ}•½}…¹‘¥‘…Ñ•Ìˆè(€€€€€€€€€€€€€€€ÍÕ‰ÍÑ…”€ô€ (€€€€€€€€€€€€€€€€€€€€‰™¥¹…±}½µÁ…É¥Í½¸ˆ(€€€€€€€€€€€€€€€€€€€¥˜¥¹Ð¡½ÕÑÁÕÐ¹•Ð ‰¥¹ÍÁ•Ñ•‘}½Õ¹Ðˆ°€À¤½È€À¤€ø€À(€€€€€€€€€€€€€€€€€€€•±Í”€‰™¥¹…±}½ÕÑÁÕÐˆ(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€•±¥˜Ñ½½±}¹…µ”¥¸ì‰½µÁ…É•}‘…Ñ…Í•Ñ}…¹‘¥‘…Ñ•Ìˆ°€‰™•Ñ¡}ÁÕ‰±¥…Ñ¥½¹}µ•Ñ…‘…Ñ„‰ôè(€€€€€€€€€€€€€€€ÍÕ‰ÍÑ…”€ô€‰™¥¹…±}½ÕÑÁÕÐˆ((€€€€€€€½¹™¥ÕÉ•‘}Ñ½½±Ì€ô½¹™¥ÕÉ•¹•Ð¡ÍÕ‰ÍÑ…”°mt¤(€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡½¹™¥ÕÉ•‘}Ñ½½±Ì°±¥ÍÐ¤è(€€€€€€€€€€€½¹™¥ÕÉ•‘}Ñ½½±Ì€ômt(€€€€€€€…±±½Ý•€ôÍ•Ð¡É•ÅÕ•ÍÐ¹…Ù…¥±…‰±•}Ñ½½±Ì¤(€€€€€€€É•ÑÕÉ¸ÍÕ‰ÍÑ…”°m¹…µ”™½È¹…µ”¥¸½¹™¥ÕÉ•‘}Ñ½½±Ì¥˜¹…µ”¥¸…±±½Ý•‘t((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜}½¹Ñ•áÑ}½µÁ½¹•¹Ñ}•ÍÑ¥µ…Ñ•Ì¡ÁÉ½Ù¥‘•È°É•ÅÕ•ÍÐ°¡¥ÍÑ½Éä¤€´ø‘¥ÑmÍÑÈ°¥¹Ñtè(€€€€€€€€ˆˆ‰I•ÑÕÉ¸Í…™”…ÁÁÉ½á¥µ…Ñ”Ñ½­•¸½Õ¹ÑÌÝ¥Ñ¡½ÕÐÁ•ÉÍ¥ÍÑ¥¹œµ½‘•°É•…Í½¹¥¹œ¸ˆˆˆ((€€€€€€€•ÍÑ¥µ…Ñ½È€ô•Ñ…ÑÑÈ¡ÁÉ½Ù¥‘•È°€‰•ÍÑ¥µ…Ñ•}½¹Ñ•áÑ}½µÁ½¹•¹ÑÌˆ°9½¹”¤(€€€€€€€¥˜…±±…‰±”¡•ÍÑ¥µ…Ñ½È¤è(€€€€€€€€€€€•ÍÑ¥µ…Ñ•Ì€ô•ÍÑ¥µ…Ñ½È¡É•ÅÕ•ÍÐ°±¥ÍÐ¡¡¥ÍÑ½Éä¤¤(€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡•ÍÑ¥µ…Ñ•Ì°‘¥Ð¤…¹…±° (€€€€€€€€€€€€€€€¥Í¥¹ÍÑ…¹”¡­•ä°ÍÑÈ¤…¹¥Í¥¹ÍÑ…¹”¡Ù…±Õ”°¥¹Ð¤…¹Ù…±Õ”€øô€À(€€€€€€€€€€€€€€€™½È­•ä°Ù…±Õ”¥¸•ÍÑ¥µ…Ñ•Ì¹¥Ñ•µÌ ¤(€€€€€€€€€€€€¤è(€€€€€€€€€€€€€€€É•ÑÕÉ¸•ÍÑ¥µ…Ñ•Ì(€€€€€€€•¹‘Á½¥¹Ð€ôì(€€€€€€€€€€€€‰•¹‘Á½¥¹Ñ}¹…µ”ˆèÉ•ÅÕ•ÍÐ¹½¹Ñ•áÐ¹•Ð ‰•¹‘Á½¥¹Ñ}¹…µ”ˆ¤°(€€€€€€€€€€€€‰‰¥½±½¥…±}½…°ˆèÉ•ÅÕ•ÍÐ¹½¹Ñ•áÐ¹•Ð ‰‰¥½±½¥…±}½…°ˆ¤°(€€€€€€€ô((€€€€€€€‘•˜•ÍÑ¥µ…Ñ”¡Ù…±Õ”è¹ä¤€´ø¥¹Ðè(€€€€€€€€€€€É•ÑÕÉ¸µ…à Ä°€¡±•¸¡…¹½¹¥…±}©Í½¸¡É•‘…Ð¡Ù…±Õ”¤¤¤€¬€Ì¤€¼¼€Ð¤((€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€‰ÍåÍÑ•µ}¥¹ÍÑÉÕÑ¥½¹Ìˆè•ÍÑ¥µ…Ñ”¡ì‰Í¡„ÈÔÙ}½¹±äˆèQÉÕ•ô¤°(€€€€€€€€€€€€‰•¹‘Á½¥¹Ñ}‘•™¥¹¥Ñ¥½¸ˆè•ÍÑ¥µ…Ñ”¡•¹‘Á½¥¹Ð¤°(€€€€€€€€€€€€‰•áÁ½Í•‘}Ñ½½±}Í¡•µ…Ìˆè•ÍÑ¥µ…Ñ”¡É•ÅÕ•ÍÐ¹…Ù…¥±…‰±•}Ñ½½±Ì¤°(€€€€€€€€€€€€‰½¹Ù•ÉÍ…Ñ¥½¹}¡¥ÍÑ½Éäˆè•ÍÑ¥µ…Ñ” (€€€€€€€€€€€€€€€m¥Ñ•´¹•Ð ‰‘¥Í½Ù•Éå}ÍÕ‰ÍÑ…”ˆ¤™½È¥Ñ•´¥¸¡¥ÍÑ½Éål´Øéut(€€€€€€€€€€€€¤°(€€€€€€€€€€€€‰Ñ½½±}É•ÍÕ±ÑÌˆè•ÍÑ¥µ…Ñ”¡¡¥ÍÑ½Éål´Øét¤°(€€€€€€€€€€€€‰ÍÑÉÕÑÕÉ•‘}½ÕÑÁÕÑ}Í¡•µ„ˆè•ÍÑ¥µ…Ñ”¡É•ÅÕ•ÍÐ¹½ÕÑÁÕÑ}Í¡•µ…}¹…µ”¤°(€€€€€€€ô((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜}…‘‘}ÕÍ…”¡±•™ÐèUÍ…•I•Á½ÉÐ°É¥¡ÐèUÍ…•I•Á½ÉÐ¤€´øUÍ…•I•Á½ÉÐè(€€€€€€€¥˜€ (€€€€€€€€€€€±•™Ð¹ÁÉ½Ù¥‘•É}¥¹Ù½…Ñ¥½¹Ì€ôô€À(€€€€€€€€€€€…¹±•™Ð¹¥¹ÁÕÑ}Ñ½­•¹Ì€ôô€À(€€€€€€€€€€€…¹±•™Ð¹½ÕÑÁÕÑ}Ñ½­•¹Ì€ôô€À(€€€€€€€€€€€…¹±•™Ð¹…¡•‘}Ñ½­•¹Ì€ôô€À(€€€€€€€€€€€…¹±•™Ð¹½ÍÑ}•¹ÑÌ€ôô€À(€€€€€€€€€€€…¹¹½Ð±•™Ð¹ÁÉ½Ù¥‘•É}É•ÅÕ•ÍÑ}¥‘Ì(€€€€€€€€€€€…¹¹½Ð±•™Ð¹ÁÉ½Ù¥‘•É}É•ÍÁ½¹Í•}¥‘Ì(€€€€€€€€¤è(€€€€€€€€€€€É•ÑÕÉ¸É¥¡Ð¹µ½‘•±}½Áä¡‘••ÀõQÉÕ”¤(€€€€€€€ÍÑ…ÑÕÍ•Ì€ôí±•™Ð¹ÕÍ…•}ÍÑ…ÑÕÌ°É¥¡Ð¹ÕÍ…•}ÍÑ…ÑÕÍô(€€€€€€€¥˜€‰ÕÍ…•}Á…ÉÑ¥…°ˆ¥¸ÍÑ…ÑÕÍ•Ì½ÈÍÑ…ÑÕÍ•Ì€ôôì‰ÕÍ…•}É•½É‘•ˆ°€‰ÕÍ…•}Õ¹…Ù…¥±…‰±”‰ôè(€€€€€€€€€€€ÕÍ…•}ÍÑ…ÑÕÌ€ô€‰ÕÍ…•}Á…ÉÑ¥…°ˆ(€€€€€€€•±¥˜€‰ÕÍ…•}É•½É‘•ˆ¥¸ÍÑ…ÑÕÍ•Ìè(€€€€€€€€€€€ÕÍ…•}ÍÑ…ÑÕÌ€ô€‰ÕÍ…•}É•½É‘•ˆ(€€€€€€€•±Í”è(€€€€€€€€€€€ÕÍ…•}ÍÑ…ÑÕÌ€ô€‰ÕÍ…•}Õ¹…Ù…¥±…‰±”ˆ(€€€€€€€É•ÑÕÉ¸UÍ…•I•Á½ÉÐ (€€€€€€€€€€€ÕÍ…•}ÍÑ…ÑÕÌõÕÍ…•}ÍÑ…ÑÕÌ°(€€€€€€€€€€€¥¹ÁÕÑ}Ñ½­•¹Ìõ±•™Ð¹¥¹ÁÕÑ}Ñ½­•¹Ì€¬É¥¡Ð¹¥¹ÁÕÑ}Ñ½­•¹Ì°(€€€€€€€€€€€½ÕÑÁÕÑ}Ñ½­•¹Ìõ±•™Ð¹½ÕÑÁÕÑ}Ñ½­•¹Ì€¬É¥¡Ð¹½ÕÑÁÕÑ}Ñ½­•¹Ì°(€€€€€€€€€€€…¡•‘}Ñ½­•¹Ìõ±•™Ð¹…¡•‘}Ñ½­•¹Ì€¬É¥¡Ð¹…¡•‘}Ñ½­•¹Ì°(€€€€€€€€€€€½ÍÑ}•¹ÑÌõ±•™Ð¹½ÍÑ}•¹ÑÌ€¬É¥¡Ð¹½ÍÑ}•¹ÑÌ°(€€€€€€€€€€€ÁÉ½Ù¥‘•É}É•ÅÕ•ÍÑ}¥‘Ìõ±•™Ð¹ÁÉ½Ù¥‘•É}É•ÅÕ•ÍÑ}¥‘Ì€¬É¥¡Ð¹ÁÉ½Ù¥‘•É}É•ÅÕ•ÍÑ}¥‘Ì°(€€€€€€€€€€€ÁÉ½Ù¥‘•É}É•ÍÁ½¹Í•}¥‘Ìõ±•™Ð¹ÁÉ½Ù¥‘•É}É•ÍÁ½¹Í•}¥‘Ì€¬É¥¡Ð¹ÁÉ½Ù¥‘•É}É•ÍÁ½¹Í•}¥‘Ì°(€€€€€€€€€€€ÁÉ½Ù¥‘•É}¥¹Ù½…Ñ¥½¹Ìõ±•™Ð¹ÁÉ½Ù¥‘•É}¥¹Ù½…Ñ¥½¹Ì€¬É¥¡Ð¹ÁÉ½Ù¥‘•É}¥¹Ù½…Ñ¥½¹Ì°(€€€€€€€€¤((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜}‰Õ‘•Ñ}•ÉÉ½È¡É•ÅÕ•ÍÐè•¹ÑIÕ¹I•ÅÕ•ÍÐ°ÕÍ…”èUÍ…•I•Á½ÉÐ¤€´øÍÑÈð9½¹”è(€€€€€€€¥˜ÕÍ…”¹¥¹ÁÕÑ}Ñ½­•¹Ì€øÉ•ÅÕ•ÍÐ¹‰Õ‘•Ð¹µ…á¥µÕµ}¥¹ÁÕÑ}Ñ½­•¹Ìè(€€€€€€€€€€€É•ÑÕÉ¸€‰¥¹ÁÕÑ}Ñ½­•¹}‰Õ‘•Ñ}•á••‘•ˆ(€€€€€€€¥˜ÕÍ…”¹½ÕÑÁÕÑ}Ñ½­•¹Ì€øÉ•ÅÕ•ÍÐ¹‰Õ‘•Ð¹µ…á¥µÕµ}½ÕÑÁÕÑ}Ñ½­•¹Ìè(€€€€€€€€€€€É•ÑÕÉ¸€‰½ÕÑÁÕÑ}Ñ½­•¹}‰Õ‘•Ñ}•á••‘•ˆ(€€€€€€€¥˜ÕÍ…”¹½ÍÑ}•¹ÑÌ€øÉ•ÅÕ•ÍÐ¹‰Õ‘•Ð¹µ…á¥µÕµ}½ÍÑ}•¹ÑÌè(€€€€€€€€€€€É•ÑÕÉ¸€‰½ÍÑ}‰Õ‘•Ñ}•á••‘•ˆ(€€€€€€€É•ÑÕÉ¸9½¹”((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜}•ÍÑ¥µ…Ñ•‘}¹•áÑ}ÑÕÉ¹}‰Õ‘•Ñ}•ÉÉ½È (€€€€€€€É•ÅÕ•ÍÐè•¹ÑIÕ¹I•ÅÕ•ÍÐ°(€€€€€€€ÕÍ…”èUÍ…•I•Á½ÉÐ°(€€€€€€€±…ÍÑ}ÑÕÉ¹}ÕÍ…”èUÍ…•I•Á½ÉÐð9½¹”°(€€€€€€€€¨°(€€€€€€€•ÍÑ¥µ…Ñ•‘}¹•áÑ}¥¹ÁÕÐè¥¹Ðð9½¹”€ô9½¹”°(€€€€¤€´øÑÕÁ±•mÍÑÈ°‘¥ÑmÍÑÈ°™±½…Ðð¥¹Ñutð9½¹”è(€€€€€€€€ˆˆ‰UÍ”Ñ¡”±…ÍÐµ•…ÍÕÉ•ÑÕÉ¸…Ì„½¹Í•ÉÙ…Ñ¥Ù”•ÍÑ¥µ…Ñ”™½ÈÑ¡”¹•áÐµ½‘•°ÑÕÉ¸¸ˆˆˆ((€€€€€€€¥˜±…ÍÑ}ÑÕÉ¹}ÕÍ…”¥Ì9½¹”è(€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€•ÍÑ¥µ…Ñ•Ì€ô€ (€€€€€€€€€€€€ (€€€€€€€€€€€€€€€€‰¥¹ÁÕÑ}Ñ½­•¹}‰Õ‘•Ñ}ÁÉ•¡•¬ˆ°(€€€€€€€€€€€€€€€ÕÍ…”¹¥¹ÁÕÑ}Ñ½­•¹Ì°(€€€€€€€€€€€€€€€€ (€€€€€€€€€€€€€€€€€€€•ÍÑ¥µ…Ñ•‘}¹•áÑ}¥¹ÁÕÐ(€€€€€€€€€€€€€€€€€€€¥˜•ÍÑ¥µ…Ñ•‘}¹•áÑ}¥¹ÁÕÐ¥Ì¹½Ð9½¹”(€€€€€€€€€€€€€€€€€€€•±Í”±…ÍÑ}ÑÕÉ¹}ÕÍ…”¹¥¹ÁÕÑ}Ñ½­•¹Ì(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€É•ÅÕ•ÍÐ¹‰Õ‘•Ð¹µ…á¥µÕµ}¥¹ÁÕÑ}Ñ½­•¹Ì°(€€€€€€€€€€€€¤°(€€€€€€€€€€€€ (€€€€€€€€€€€€€€€€‰½ÕÑÁÕÑ}Ñ½­•¹}‰Õ‘•Ñ}ÁÉ•¡•¬ˆ°(€€€€€€€€€€€€€€€ÕÍ…”¹½ÕÑÁÕÑ}Ñ½­•¹Ì°(€€€€€€€€€€€€€€€±…ÍÑ}ÑÕÉ¹}ÕÍ…”¹½ÕÑÁÕÑ}Ñ½­•¹Ì°(€€€€€€€€€€€€€€€É•ÅÕ•ÍÐ¹‰Õ‘•Ð¹µ…á¥µÕµ}½ÕÑÁÕÑ}Ñ½­•¹Ì°(€€€€€€€€€€€€¤°(€€€€€€€€€€€€ (€€€€€€€€€€€€€€€€‰½ÍÑ}‰Õ‘•Ñ}ÁÉ•¡•¬ˆ°(€€€€€€€€€€€€€€€ÕÍ…”¹½ÍÑ}•¹ÑÌ°(€€€€€€€€€€€€€€€±…ÍÑ}ÑÕÉ¹}ÕÍ…”¹½ÍÑ}•¹ÑÌ°(€€€€€€€€€€€€€€€É•ÅÕ•ÍÐ¹‰Õ‘•Ð¹µ…á¥µÕµ}½ÍÑ}•¹ÑÌ°(€€€€€€€€€€€€¤°(€€€€€€€€¤(€€€€€€€™½È½‘”°½¹ÍÕµ•°•ÍÑ¥µ…Ñ•‘}¹•áÐ°µ…á¥µÕ´¥¸•ÍÑ¥µ…Ñ•Ìè(€€€€€€€€€€€¥˜•ÍÑ¥µ…Ñ•‘}¹•áÐ€ø€À…¹½¹ÍÕµ•€¬•ÍÑ¥µ…Ñ•‘}¹•áÐ€øµ…á¥µÕ´è(€€€€€€€€€€€€€€€É•ÑÕÉ¸½‘”°ì(€€€€€€€€€€€€€€€€€€€€‰½¹ÍÕµ•ˆè½¹ÍÕµ•°(€€€€€€€€€€€€€€€€€€€€‰•ÍÑ¥µ…Ñ•‘}¹•áÑ}ÑÕÉ¸ˆè•ÍÑ¥µ…Ñ•‘}¹•áÐ°(€€€€€€€€€€€€€€€€€€€€‰½¹™¥ÕÉ•‘}µ…á¥µÕ´ˆèµ…á¥µÕ´°(€€€€€€€€€€€€€€€ô(€€€€€€€É•ÑÕÉ¸9½¹”((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜}‰Õ‘•Ñ}µ•ÍÍ…”¡½‘”èÍÑÈ¤€´øÍÑÈè(€€€€€€€¥˜½‘”¹ÍÑ…ÉÑÍÝ¥Ñ   ‰¥¹ÁÕÑ}Ñ½­•¹|ˆ°€‰½ÕÑÁÕÑ}Ñ½­•¹|ˆ¤¤è(€€€€€€€€€€€É•ÑÕÉ¸€‰1¥Ù”…•¹ÐÉÕ¸ÍÑ½ÁÁ•…ÐÑ¡”½¹™¥ÕÉ•ÕµÕ±…Ñ¥Ù”Ñ½­•¸‰Õ‘•Ð¸ˆ(€€€€€€€¥˜½‘”¹ÍÑ…ÉÑÍÝ¥Ñ  ‰½ÍÑ|ˆ¤è(€€€€€€€€€€€É•ÑÕÉ¸€‰1¥Ù”…•¹ÐÉÕ¸ÍÑ½ÁÁ•…ÐÑ¡”½¹™¥ÕÉ•ÕµÕ±…Ñ¥Ù”½ÍÐ‰Õ‘•Ð¸ˆ(€€€€€€€É•ÑÕÉ¸€‰•¹ÐÉÕ¸•á••‘•¥ÑÌÑ½­•¸½È½ÍÐ‰Õ‘•Ð¸ˆ((€€€ÍÑ…Ñ¥µ•Ñ¡½(€€€‘•˜}™…¥±ÕÉ” (€€€€€€€ÍÑ…ÑÕÌ°(€€€€€€€½‘”°(€€€€€€€µ•ÍÍ…”°(€€€€€€€ÑÉ…”°(€€€€€€€ÕÍ…”°(€€€€€€€ÑÕÉ¹Ì°(€€€€€€€Ñ½½±}…±±Ì°(€€€€€€€ÍÑ…ÉÑ•°(€€€€€€€€¨°(€€€€€€€É•ÑÉå…‰±”è‰½½°ð9½¹”€ô9½¹”°(€€€€€€€…Ñ•½ÉäèÍÑÈ€ô€‰…•¹Ñ}ÉÕ¹Ñ¥µ”ˆ°(€€€€€€€Í½ÕÉ•}‘¥…¹½ÍÑ¥Œõ9½¹”°(€€€€¤è(€€€€€€€É•ÑÕÉ¸•¹ÑIÕ¹I•ÍÕ±Ð (€€€€€€€€€€€ÍÑ…ÑÕÌõÍÑ…ÑÕÌ°(€€€€€€€€€€€ÕÍ…”õÕÍ…”°(€€€€€€€€€€€ÑÉ…”õÑÉ…”°(€€€€€€€€€€€•ÉÉ½Èõ9½Éµ…±¥é•‘•¹ÑÉÉ½È (€€€€€€€€€€€€€€€½‘”õ½‘”°(€€€€€€€€€€€€€€€Í…™•}µ•ÍÍ…”õµ•ÍÍ…”°(€€€€€€€€€€€€€€€É•ÑÉå…‰±”ô (€€€€€€€€€€€€€€€€€€€É•ÑÉå…‰±”(€€€€€€€€€€€€€€€€€€€¥˜É•ÑÉå…‰±”¥Ì¹½Ð9½¹”(€€€€€€€€€€€€€€€€€€€•±Í”½‘”¥¸ì‰ÁÉ½Ù¥‘•É}Ñ¥µ•½ÕÐˆ°€‰ÁÉ½Ù¥‘•É}™…¥±ÕÉ”ˆ°€‰Ñ½½±}Ñ¥µ•½ÕÐ‰ô(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€…Ñ•½Éäõ…Ñ•½Éä°(€€€€€€€€€€€€€€€Í½ÕÉ•}‘¥…¹½ÍÑ¥ŒõÍ½ÕÉ•}‘¥…¹½ÍÑ¥Œ°(€€€€€€€€€€€€¤°(€€€€€€€€€€€ÑÕÉ¹ÌõÑÕÉ¹Ì°(€€€€€€€€€€€Ñ½½±}…±±ÌõÑ½½±}…±±Ì°(€€€€€€€€€€€‘ÕÉ…Ñ¥½¹}µÌõ¥¹Ð ¡Ñ¥µ”¹µ½¹½Ñ½¹¥Œ ¤€´ÍÑ…ÉÑ•¤€¨€ÄÀÀÀ¤°(€€€€€€€€¤(