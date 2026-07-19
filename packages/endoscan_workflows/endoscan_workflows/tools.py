"""Typed, stage-bound tool registry with no shell, arbitrary paths, or network access."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import (
    IdempotencyClassification,
    NormalizedAgentError,
    SideEffectClassification,
    ToolCallStatus,
    ToolDefinition,
    ToolInvocation,
    ToolNormalizationWarning,
    ToolResult,
    WorkflowState,
)
from .controlled_vocabulary import (
    CONTROLLED_VOCABULARY_POLICY_VERSION,
    canonicalize_geo_study_type,
)
from .errors import AgentPolicyError
from .source_security import SourceTimeoutError, SourceToolError

logger = logging.getLogger("uvicorn.error.endoscan.workflow.source_tool")
SEARCH_TERM_NORMALIZATION_POLICY_VERSION = "phase1-search-term-normalization-v1"


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0.0"


class RepositoryInput(ToolInput):
    repository_root: str = Field(pattern=r"^\.$")


class RegistryInspection(ToolOutput):
    endpoint_count: int
    endpoints: list[dict[str, Any]]
    registry_mutated: bool = False


class AdapterInventory(ToolOutput):
    adapters: list[dict[str, str]]


class PipelineSummaryInput(RepositoryInput):
    endpoint_ids: list[str] = Field(min_length=1, max_length=10)


class PipelineSummary(ToolOutput):
    endpoints: list[dict[str, Any]]
    deterministic_kernel_preserved: bool = True


class CandidateFixtureInput(ToolInput):
    endpoint_name: str = Field(min_length=3, max_length=120)


class CandidateFixtureOutput(ToolOutput):
    fixture_label: str
    live_discovery: bool = False
    candidates: list[dict[str, Any]]


class EchoInput(ToolInput):
    value: str


class EchoOutput(ToolOutput):
    value: str


@dataclass(frozen=True)
class NormalizedToolArguments:
    arguments: dict[str, Any]
    warnings: tuple[ToolNormalizationWarning, ...] = ()


ArgumentNormalizer = Callable[[dict[str, Any]], NormalizedToolArguments]


def normalize_configured_string_lists(
    arguments: dict[str, Any],
    *,
    optional_fields: tuple[str, ...],
    required_fields: tuple[str, ...] = (),
) -> NormalizedToolArguments:
    """Normalize only explicitly safe string-list fields without inferring new values."""

    optional_field_set = set(optional_fields)
    configured_fields = tuple(dict.fromkeys((*required_fields, *optional_fields)))
    normalized_arguments = dict(arguments)
    warnings: list[ToolNormalizationWarning] = []
    for field in configured_fields:
        value = arguments.get(field)
        if not isinstance(value, list):
            continue
        normalized_values: list[Any] = []
        seen: set[str] = set()
        for index, item in enumerate(value):
            if not isinstance(item, str):
                normalized_values.append(item)
                continue
            normalized = item.strip()
            if normalized != item:
                warnings.append(
                    ToolNormalizationWarning(
                        code="search_term_whitespace_trimmed",
                        field=field,
                        original_index=index,
                        original=item,
                        normalized=normalized,
                        policy_version=SEARCH_TERM_NORMALIZATION_POLICY_VERSION,
                    )
                )
            if not normalized:
                warnings.append(
                    ToolNormalizationWarning(
                        code=(
                            "empty_optional_search_term_removed"
                            if field in optional_field_set
                            else "empty_required_search_term_removed"
                        ),
                        field=field,
                        original_index=index,
                        original=item,
                        normalized=None,
                        policy_version=SEARCH_TERM_NORMALIZATION_POLICY_VERSION,
                    )
                )
                continue
            if normalized in seen:
                warnings.append(
                    ToolNormalizationWarning(
                        code="duplicate_search_term_removed",
                        field=field,
                        original_index=index,
                        original=item,
                        normalized=normalized,
                        policy_version=SEARCH_TERM_NORMALIZATION_POLICY_VERSION,
                    )
                )
                continue
            seen.add(normalized)
            normalized_values.append(normalized)
        normalized_arguments[field] = normalized_values
    return NormalizedToolArguments(
        arguments=normalized_arguments,
        warnings=tuple(warnings),
    )


def normalize_geo_search_arguments(arguments: dict[str, Any]) -> NormalizedToolArguments:
    normalized = normalize_configured_string_lists(
        arguments,
        optional_fields=(
            "organism_alternatives",
            "cell_tissue_terms",
            "treatment_terms",
        ),
        required_fields=("scientific_terms",),
    )
    study_types = arguments.get("study_type_alternatives")
    if not isinstance(study_types, list):
        return normalized

    normalized_study_types: list[Any] = []
    normalized_arguments = dict(normalized.arguments)
    warnings = list(normalized.warnings)
    seen: set[str] = set()
    for index, item in enumerate(study_types):
        if not isinstance(item, str):
            normalized_study_types.append(item)
            continue
        match = canonicalize_geo_study_type(item)
        if match.canonical is None:
            normalized_study_types.append(match.comparison_value)
            warnings.append(
                ToolNormalizationWarning(
                    code="controlled_vocabulary_unknown_value",
                    field="study_type_alternatives",
                    original_index=index,
                    original=item,
                    normalized=match.comparison_value,
                    policy_version=CONTROLLED_VOCABULARY_POLICY_VERSION,
                )
            )
            continue
        if match.canonical != item:
            warnings.append(
                ToolNormalizationWarning(
                    code="controlled_vocabulary_alias_canonicalized",
                    field="study_type_alternatives",
                    original_index=index,
                    original=item,
                    normalized=match.canonical,
                    policy_version=CONTROLLED_VOCABULARY_POLICY_VERSION,
                )
            )
        if match.canonical in seen:
            warnings.append(
                ToolNormalizationWarning(
                    code="duplicate_search_term_removed",
                    field="study_type_alternatives",
                    original_index=index,
                    original=item,
                    normalized=match.canonical,
                    policy_version=CONTROLLED_VOCABULARY_POLICY_VERSION,
                )
            )
            continue
        seen.add(match.canonical)
        normalized_study_types.append(match.canonical)
    normalized_arguments["study_type_alternatives"] = normalized_study_types
    return NormalizedToolArguments(
        arguments=normalized_arguments,
        warnings=tuple(warnings),
    )


class RegisteredTool:
    def __init__(
        self,
        definition: ToolDefinition,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        implementation: Callable[..., BaseModel | dict],
        *,
        contextual: bool = False,
        argument_normalizer: ArgumentNormalizer | None = None,
    ):
        self.definition = definition
        self.input_model = input_model
        self.output_model = output_model
        self.implementation = implementation
        self.contextual = contextual
        self.argument_normalizer = argument_normalizer


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        if tool.definition.name in self._tools:
            raise ValueError(f"tool {tool.definition.name!r} is already registered")
        if tool.definition.side_effect is SideEffectClassification.PRODUCTION_WRITE:
            raise ValueError("Production-write tools are prohibited in the Phase-0 registry")
        self._tools[tool.definition.name] = tool

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise AgentPolicyError(f"Tool {name!r} is not allowlisted.") from exc

    def definitions(self) -> list[ToolDefinition]:
        return [self._tools[name].definition for name in sorted(self._tools)]

    def invoke(self, invocation: ToolInvocation) -> ToolResult:
        started = time.monotonic()
        original_arguments = dict(invocation.arguments)
        normalized = NormalizedToolArguments(arguments=original_arguments)
        try:
            tool = self.get(invocation.tool_name)
            if invocation.workflow_stage not in tool.definition.allowed_workflow_stages:
                raise AgentPolicyError("Tool is prohibited in the current workflow stage.")
            if not set(tool.definition.required_permissions).issubset(invocation.permission_scope):
                raise AgentPolicyError("Tool permission scope is insufficient.")
            if tool.argument_normalizer is not None:
                normalized = tool.argument_normalizer(original_arguments)
            typed_input = tool.input_model.model_validate(normalized.arguments)
        except (ValidationError, AgentPolicyError) as exc:
            return ToolResult(
                tool_name=invocation.tool_name,
                status=ToolCallStatus.PROHIBITED
                if isinstance(exc, AgentPolicyError)
                else ToolCallStatus.FAILED,
                error=NormalizedAgentError(
                    code="prohibited_tool"
                    if isinstance(exc, AgentPolicyError)
                    else "tool_input_invalid",
                    safe_message=str(exc),
                    retryable=False,
                    category="policy" if isinstance(exc, AgentPolicyError) else "validation",
                ),
                duration_ms=int((time.monotonic() - started) * 1000),
                original_arguments=original_arguments,
                normalized_arguments=normalized.arguments,
                normalization_warnings=list(normalized.warnings),
            )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                tool.implementation,
                *((typed_input, invocation) if tool.contextual else (typed_input,)),
            )
            try:
                raw = future.result(timeout=tool.definition.timeout_seconds)
                output = tool.output_model.model_validate(raw).model_dump(mode="json")
            except FutureTimeout:
                future.cancel()
                return ToolResult(
                    tool_name=invocation.tool_name,
                    status=ToolCallStatus.TIMED_OUT,
                    error=NormalizedAgentError(
                        code="tool_timeout",
                        safe_message="Tool exceeded its configured timeout.",
                        retryable=True,
                        category="timeout",
                    ),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    original_arguments=original_arguments,
                    normalized_arguments=normalized.arguments,
                    normalization_warnings=list(normalized.warnings),
                )
            except ValidationError:
                return ToolResult(
                    tool_name=invocation.tool_name,
                    status=ToolCallStatus.FAILED,
                    error=NormalizedAgentError(
                        code="tool_output_invalid",
                        safe_message="Tool returned output that failed its declared schema.",
                        retryable=False,
                        category="validation",
                    ),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    original_arguments=original_arguments,
                    normalized_arguments=normalized.arguments,
                    normalization_warnings=list(normalized.warnings),
                )
            except SourceToolError as exc:
                diagnostic = exc.diagnostic
                logger.warning(
                    "source_tool_failure tool_name=%s source_host=%s safe_url_path=%s "
                    "http_method=%s http_status=%s final_approved_host=%s content_type=%s "
                    "response_byte_count=%s exception_class=%s source_error_category=%s "
                    "retryable=%s attempt_number=%s request_duration_ms=%s developer_message=%s",
                    invocation.tool_name,
                    diagnostic.source_host if diagnostic else None,
                    diagnostic.safe_url_path if diagnostic else None,
                    diagnostic.http_method if diagnostic else "GET",
                    diagnostic.http_status if diagnostic else None,
                    diagnostic.final_approved_host if diagnostic else None,
                    diagnostic.content_type if diagnostic else None,
                    diagnostic.response_byte_count if diagnostic else None,
                    diagnostic.exception_class if diagnostic else type(exc).__name__,
                    diagnostic.source_error_category if diagnostic else "source_tool_failure",
                    exc.retryable,
                    diagnostic.attempt_number if diagnostic else 1,
                    diagnostic.request_duration_ms if diagnostic else 0,
                    diagnostic.developer_message if diagnostic else None,
                )
                return ToolResult(
                    tool_name=invocation.tool_name,
                    status=(
                        ToolCallStatus.TIMED_OUT
                        if isinstance(exc, SourceTimeoutError)
                        else ToolCallStatus.FAILED
                    ),
                    error=NormalizedAgentError(
                        code=(
                            f"source_{diagnostic.source_error_category}"
                            if diagnostic
                            else "source_tool_failure"
                        ),
       ×Oz¶‰Ëkºwµçh€€€€€€€™½È•¹‘Á½¥¹Ñ}¥¥¸É•ÅÕ•ÍĞ¹•¹‘Á½¥¹Ñ}¥‘Ìè(€€€€€€€€€€€½¹™¥}Á…Ñ¡Ì€ôÍ½ÉÑ• (€€€€€€€€€€€€€€€Á…Ñ ¹É•±…Ñ¥Ù•}Ñ¼¡É½½Ğ¤¹…Í}Á½Í¥à ¤(€€€€€€€€€€€€€€€™½ÈÁ…Ñ ¥¸€¡É½½Ğ€¼€‰Á¥Á•±¥¹•Ìˆ€¼€‰•¹‘Á½¥¹ÑÌˆ€¼•¹‘Á½¥¹Ñ}¥¤¹±½ˆ ‰½¹™¥œ¨¹å…µ°ˆ¤(€€€€€€€€€€€€¤(€€€€€€€€€€€ÍÕµµ…É¥•Ì¹…ÁÁ•¹ (€€€€€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€€€€€‰•¹‘Á½¥¹Ñ}¥ˆè•¹‘Á½¥¹Ñ}¥°(€€€€€€€€€€€€€€€€€€€€‰ÉÕ¹¹•Èˆè€‰Á¥Á•±¥¹•Ì½•¹‘Á½¥¹ÑÌ½H½ÉÕ¸¹Áäˆ°(€€€€€€€€€€€€€€€€€€€€‰½¹™¥Ìˆè½¹™¥}Á…Ñ¡Ì°(€€€€€€€€€€€€€€€€€€€€‰É½ÕÁ•‘}•Ù…±Õ…Ñ¥½¸ˆèQÉÕ”°(€€€€€€€€€€€€€€€€€€€€‰ÁÉ½‘ÕÑ¥½¹}É•¥ÍÑÉå}İÉ¥Ñ•}…±±½İ•ˆè…±Í”°(€€€€€€€€€€€€€€€ô(€€€€€€€€€€€€¤(€€€€€€€É•ÑÕÉ¸A¥Á•±¥¹•MÕµµ…Éä¡•¹‘Á½¥¹ÑÌõÍÕµµ…É¥•Ì¤((€€€‘•˜…¹‘¥‘…Ñ•Ì¡É•ÅÕ•ÍĞè…¹‘¥‘…Ñ•¥áÑÕÉ•%¹ÁÕĞ¤€´ø…¹‘¥‘…Ñ•¥áÑÕÉ•=ÕÑÁÕĞè(€€€€€€€É•ÑÕÉ¸…¹‘¥‘…Ñ•¥áÑÕÉ•=ÕÑÁÕĞ (€€€€€€€€€€€™¥áÑÕÉ•}±…‰•°ô‰AÉ•Á…É•‘•Ñ•Éµ¥¹¥ÍÑ¥ŒA¡…Í”´À™¥áÑÕÉ”ƒŠP¹½Ğ±¥Ù”‘¥Í½Ù•Éäˆ°(€€€€€€€€€€€…¹‘¥‘…Ñ•Ìõl(€€€€€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€€€€€‰…¹‘¥‘…Ñ•}¥ˆè€‰Á¡…Í”Àµ…¹‘¥‘…Ñ”µ„ˆ°(€€€€€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”ˆè˜‰AÉ•Á…É•µ•Ñ…‘…Ñ„…¹‘¥‘…Ñ”™½ÈíÉ•ÅÕ•ÍĞ¹•¹‘Á½¥¹Ñ}¹…µ•ôˆ°(€€€€€€€€€€€€€€€€€€€€‰Í½ÕÉ”ˆè€‰Á¡…Í”Àè¼½ÁÉ•Á…É•µ™¥áÑÕÉ”½„ˆ°(€€€€€€€€€€€€€€€€€€€€‰…•ÍÍ¥½¹}Ù•É¥™¥•ˆè…±Í”°(€€€€€€€€€€€€€€€€€€€€‰±¥•¹Í•}Ù•É¥™¥•ˆè…±Í”°(€€€€€€€€€€€€€€€€€€€€‰É•½µµ•¹‘…Ñ¥½¸ˆè€‰É•Ù¥•Üµ™¥ÉÍĞˆ°(€€€€€€€€€€€€€€€€€€€€‰±¥µ¥Ñ…Ñ¥½¹Ìˆèl(€€€€€€€€€€€€€€€€€€€€€€€€‰Må¹Ñ¡•Ñ¥Œİ½É­™±½Ü™¥áÑÕÉ”ì¹½Ğ„Í¥•¹Ñ¥™¥Œ‘…Ñ…Í•ĞÉ•½µµ•¹‘…Ñ¥½¸¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€‰•ÍÍ¥½¸°½¹ÑÉ½±Ì°±…‰•±Ì°…¹±¥•¹”É•ÅÕ¥É”™ÕÑÕÉ”±¥Ù”€ˆ(€€€€€€€€€€€€€€€€€€€€€€€€‰Ù•É¥™¥…Ñ¥½¸¸ˆ°(€€€€€€€€€€€€€€€€€€€t°(€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€€€€€‰…¹‘¥‘…Ñ•}¥ˆè€‰Á¡…Í”Àµ…¹‘¥‘…Ñ”µˆˆ°(€€€€€€€€€€€€€€€€€€€€‰Ñ¥Ñ±”ˆè˜‰AÉ•Á…É•µ•Ñ…‘…Ñ„…¹‘¥‘…Ñ”™½ÈíÉ•ÅÕ•ÍĞ¹•¹‘Á½¥¹Ñ}¹…µ•ôˆ°(€€€€€€€€€€€€€€€€€€€€‰Í½ÕÉ”ˆè€‰Á¡…Í”Àè¼½ÁÉ•Á…É•µ™¥áÑÕÉ”½ˆˆ°(€€€€€€€€€€€€€€€€€€€€‰…•ÍÍ¥½¹}Ù•É¥™¥•ˆè…±Í”°(€€€€€€€€€€€€€€€€€€€€‰±¥•¹Í•}Ù•É¥™¥•ˆè…±Í”°(€€€€€€€€€€€€€€€€€€€€‰É•½µµ•¹‘…Ñ¥½¸ˆè€‰…±Ñ•É¹…Ñ¥Ù”ˆ°(€€€€€€€€€€€€€€€€€€€€‰±¥µ¥Ñ…Ñ¥½¹Ìˆèl(€€€€€€€€€€€€€€€€€€€€€€€€‰Må¹Ñ¡•Ñ¥Œİ½É­™±½Ü™¥áÑÕÉ”ì¹½Ğ„Í¥•¹Ñ¥™¥Œ‘…Ñ…Í•ĞÉ•½µµ•¹‘…Ñ¥½¸¸ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€‰9¼‘…Ñ„¡…Ì‰••¸‘½İ¹±½…‘•½È…ÁÁÉ½Ù•™½ÈÑÉ…¥¹¥¹œ¸ˆ°(€€€€€€€€€€€€€€€€€€€t°(€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€t°(€€€€€€€€¤((€€€ÍÁ•Ì€ôl(€€€€€€€€ (€€€€€€€€€€€€‰¥¹ÍÁ•Ñ}•¹‘Á½¥¹Ñ}É•¥ÍÑÉäˆ°(€€€€€€€€€€€€‰%¹ÍÁ•Ğ½µµ¥ÑÑ••¹‘Á½¥¹ĞÉ•¥ÍÑÉäµ•Ñ…‘…Ñ„İ¥Ñ¡½ÕĞµÕÑ…Ñ¥½¸¸ˆ°(€€€€€€€€€€€I•Á½Í¥Ñ½Éå%¹ÁÕĞ°(€€€€€€€€€€€I•¥ÍÑÉå%¹ÍÁ•Ñ¥½¸°(€€€€€€€€€€€¥¹ÍÁ•Ñ}É•¥ÍÑÉä°(€€€€€€€€€€€l‰É•¥ÍÑÉäéÉ•…‰t°(€€€€€€€€€€€M¥‘•™™•Ñ±…ÍÍ¥™¥…Ñ¥½¸¹9=9°(€€€€€€€€¤°(€€€€€€€€ (€€€€€€€€€€€€‰±¥ÍÑ}­¹½İ¹}Í½ÕÉ•}…‘…ÁÑ•ÉÌˆ°(€€€€€€€€€€€€‰1¥ÍĞ•á¥ÍÑ¥¹œÍ½ÕÉ”…¹ÍÑ…¥¹œ…‘…ÁÑ•ÉÌ™É½´‰½Õ¹‘•É•Á½Í¥Ñ½ÉäÁ…Ñ¡Ì¸ˆ°(€€€€€€€€€€€I•Á½Í¥Ñ½Éå%¹ÁÕĞ°(€€€€€€€€€€€‘…ÁÑ•É%¹Ù•¹Ñ½Éä°(€€€€€€€€€€€±¥ÍÑ}…‘…ÁÑ•ÉÌ°(€€€€€€€€€€€l‰É•Á½Í¥Ñ½ÉäéÉ•…‰t°(€€€€€€€€€€€M¥‘•™™•Ñ±…ÍÍ¥™¥…Ñ¥½¸¹9=9°(€€€€€€€€¤°(€€€€€€€€ (€€€€€€€€€€€€‰ÍÕµµ…É¥é•}•á¥ÍÑ¥¹}•¹‘Á½¥¹Ñ}Á¥Á•±¥¹”ˆ°(€€€€€€€€€€€€‰MÕµµ…É¥é”‘•Ñ•Éµ¥¹¥ÍÑ¥ŒH½HÁ¥Á•±¥¹”½¹™¥ÕÉ…Ñ¥½¸İ¥Ñ¡½ÕĞÉÕ¹¹¥¹œ¥Ğ¸ˆ°(€€€€€€€€€€€A¥Á•±¥¹•MÕµµ…Éå%¹ÁÕĞ°(€€€€€€€€€€€A¥Á•±¥¹•MÕµµ…Éä°(€€€€€€€€€€€ÍÕµµ…É¥é•}Á¥Á•±¥¹•Ì°(€€€€€€€€€€€l‰É•Á½Í¥Ñ½ÉäéÉ•…‰t°(€€€€€€€€€€€M¥‘•™™•Ñ±…ÍÍ¥™¥…Ñ¥½¸¹9=9°(€€€€€€€€¤°(€€€€€€€€ (€€€€€€€€€€€€‰É•…Ñ•}‘…Ñ…Í•Ñ}…¹‘¥‘…Ñ•}…ÉÑ¥™…Ğˆ°(€€€€€€€€€€€€‰I•ÑÕÉ¸±•…É±ä±…‰•±±•ÁÉ•Á…É•A¡…Í”´À…¹‘¥‘…Ñ”™¥áÑÕÉ•Ì¸ˆ°(€€€€€€€€€€€…¹‘¥‘…Ñ•¥áÑÕÉ•%¹ÁÕĞ°(€€€€€€€€€€€…¹‘¥‘…Ñ•¥áÑÕÉ•=ÕÑÁÕĞ°(€€€€€€€€€€€…¹‘¥‘…Ñ•Ì°(€€€€€€€€€€€l‰™¥áÑÕÉ”éÉ•…‰t°(€€€€€€€€€€€M¥‘•™™•Ñ±…ÍÍ¥™¥…Ñ¥½¸¹IQ%Q}]I%Q°(€€€€€€€€¤°(€€€t(€€€™½È€ (€€€€€€€¹…µ”°(€€€€€€€‘•ÍÉ¥ÁÑ¥½¸°(€€€€€€€¥¹ÁÕÑ}µ½‘•°°(€€€€€€€½ÕÑÁÕÑ}µ½‘•°°(€€€€€€€¥µÁ±•µ•¹Ñ…Ñ¥½¸°(€€€€€€€Á•Éµ¥ÍÍ¥½¹Ì°(€€€€€€€Í¥‘•}•™™•Ğ°(€€€€¤¥¸ÍÁ•Ìè(€€€€€€€É•¥ÍÑÉä¹É•¥ÍÑ•È (€€€€€€€€€€€I•¥ÍÑ•É•‘Q½½° (€€€€€€€€€€€€€€€Q½½±•™¥¹¥Ñ¥½¸ (€€€€€€€€€€€€€€€€€€€¹…µ”õ¹…µ”°(€€€€€€€€€€€€€€€€€€€‘•ÍÉ¥ÁÑ¥½¸õ‘•ÍÉ¥ÁÑ¥½¸°(€€€€€€€€€€€€€€€€€€€¥¹ÁÕÑ}Í¡•µ…}¹…µ”õ¥¹ÁÕÑ}µ½‘•°¹}}¹…µ•}|°(€€€€€€€€€€€€€€€€€€€½ÕÑÁÕÑ}Í¡•µ…}¹…µ”õ½ÕÑÁÕÑ}µ½‘•°¹}}¹…µ•}|°(€€€€€€€€€€€€€€€€€€€É•ÅÕ¥É•‘}Á•Éµ¥ÍÍ¥½¹ÌõÁ•Éµ¥ÍÍ¥½¹Ì°(€€€€€€€€€€€€€€€€€€€Í¥‘•}•™™•ĞõÍ¥‘•}•™™•Ğ°(€€€€€€€€€€€€€€€€€€€¥‘•µÁ½Ñ•¹äõ%‘•µÁ½Ñ•¹å±…ÍÍ¥™¥…Ñ¥½¸¹%5A=Q9Q}]%Q!}-d°(€€€€€€€€€€€€€€€€€€€Ñ¥µ•½ÕÑ}Í•½¹‘ÌôÔ¸À°(€€€€€€€€€€€€€€€€€€€…±±½İ•‘}İ½É­™±½İ}ÍÑ…•Ìõ‘¥Í½Ù•Éä°(€€€€€€€€€€€€€€€€€€€¥µÁ±•µ•¹Ñ…Ñ¥½¹}Ù•ÉÍ¥½¸ô‰Á¡…Í”ÀµØÄˆ°(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€¥¹ÁÕÑ}µ½‘•°°(€€€€€€€€€€€€€€€½ÕÑÁÕÑ}µ½‘•°°(€€€€€€€€€€€€€€€¥µÁ±•µ•¹Ñ…Ñ¥½¸°(€€€€€€€€€€€€¤(€€€€€€€€¤(€€€É•ÑÕÉ¸É•¥ÍÑÉä(()‘•˜Á¡…Í”Å}Ñ½½±}É•¥ÍÑÉä¡É•Á½}É½½ĞèA…Ñ °‘¥Í½Ù•Éå}Í•ÉÙ¥”¤€´øQ½½±I•¥ÍÑÉäè(€€€€ˆˆ‰A¡…Í”´ÀÑ½½±ÌÁ±ÕÌ‰½Õ¹‘•A¡…Í”´Ä½µÁ…Ñ¥‰¥±¥Ñä…¹ÍÑ…•‘¥Í½Ù•ÉäÑ½½±Ì¸ˆˆˆ(€€€™É½´€¹‘¥Í½Ù•Éå}Ñ½½±Ì¥µÁ½ÉĞ€ (€€€€€€€½µÁ…É•…Ñ…Í•Ñ…¹‘¥‘…Ñ•Í%¹ÁÕĞ°(€€€€€€€½µÁ…É•…Ñ…Í•Ñ…¹‘¥‘…Ñ•Í=ÕÑÁÕĞ°(€€€€€€€•½•ÍÍ¥½¹%¹ÁÕĞ°(€€€€€€€•½•ÍÍ¥½¹Í%¹ÁÕĞ°(€€€€€€€•½…¹‘¥‘…Ñ•Í%¹ÍÁ•Ñ¥½¹=ÕÑÁÕĞ°(€€€€€€€•½M…µÁ±••Í¥¹=ÕÑÁÕĞ°(€€€€€€€•½M•É¥•Í5•Ñ…‘…Ñ…=ÕÑÁÕĞ°(€€€€€€€•½Y…±¥‘…Ñ¥½¹	…Ñ¡=ÕÑÁÕĞ°(€€€€€€€AÕ‰±¥…Ñ¥½¹5•Ñ…‘…Ñ…%¹ÁÕĞ°(€€€€€€€AÕ‰±¥…Ñ¥½¹5•Ñ…‘…Ñ…=ÕÑÁÕĞ°(€€€€€€€M•…É¡•½M•É¥•Í%¹ÁÕĞ°(€€€€€€€M•…É¡•½M•É¥•Í=ÕÑÁÕĞ°(€€€€¤((€€€É•¥ÍÑÉä€ôÁ¡…Í”Á}Ñ½½±}É•¥ÍÑÉä¡É•Á½}É½½Ğ¤(€€€‘¥Í½Ù•Éä€ôm]½É­™±½İMÑ…Ñ”¹%M=YI%9}Qt(€€€ÍÁ•Ì€ôl(€€€€€€€€ (€€€€€€€€€€€€‰Í•…É¡}•½}Í•É¥•Ìˆ°(€€€€€€€€€€€€ (€€€€€€€€€€€€€€€€‰M•…É ½™™¥¥…°9	$<M•É¥•Ìµ•Ñ…‘…Ñ„İ¥Ñ „ÑåÁ•‰½Õ¹‘•Á±…¸¸€ˆ(€€€€€€€€€€€€€€€€‰Q¡”Ñ½½°É•¹‘•ÉÌ9‰•Ñİ••¸½¹•ÁÑÌ…¹=Hİ¥Ñ¡¥¸…±Ñ•É¹…Ñ¥Ù•Ì¸€ˆ(€€€€€€€€€€€€€€€€‰½È½ÁÑ¥½¹…°™¥±Ñ•ÉÌ°½µ¥ĞÑ¡”™¥±Ñ•È½ÈÉ•ÑÕÉ¸mtİ¡•¸¹¼½¹ÍÑÉ…¥¹Ğ€ˆ(€€€€€€€€€€€€€€€€‰¥Ì¥¹Ñ•¹‘•ì¹•Ù•È¥¹±Õ‘”•µÁÑäÍÑÉ¥¹Ì°…¹ÁÉ½Ù¥‘”½¹±ä½¹É•Ñ”€ˆ(€€€€€€€€€€€€€€€€‰‰¥½±½¥…°Ñ•ÉµÌ¸ˆ(€€€€€€€€€€€€¤°(€€€€€€€€€€€M•…É¡•½M•É¥•Í%¹ÁÕĞ°(€€€€€€€€€€€M•…É¡•½M•É¥•Í=ÕÑÁÕĞ°(€€€€€€€€€€€‘¥Í½Ù•Éå}Í•ÉÙ¥”¹Í•…É¡}•½}Í•É¥•Ì°(€€€€€€€€€€€l‰Í½ÕÉ”é•¼éÉ•…‰t°(€€€€€€€€€€€M¥‘•™™•Ñ±…ÍÍ¥™¥…Ñ¥½¸¹aQI91}I°(€€€€€€€€¤°(€€€€€€€€ (€€€€€€€€€€€€‰™•Ñ¡}•½}Í•É¥•Í}µ•Ñ…‘…Ñ„ˆ°(€€€€€€€€€€€€‰•Ñ ½™™¥¥…°<M•É¥•Ìµ•Ñ…‘…Ñ„™½È½¹”Ù…±¥‘…Ñ•M…•ÍÍ¥½¸¸ˆ°(€€€€€€€€€€€•½•ÍÍ¥½¹%¹ÁÕĞ°(€€€€€€€€€€€•½M•É¥•Í5•Ñ…‘…Ñ…=ÕÑÁÕĞ°(€€€€€€€€€€€‘¥Í½Ù•Éå}Í•ÉÙ¥”¹™•Ñ¡}•½}Í•É¥•Í}µ•Ñ…‘…Ñ„°(€€€€€€€€€€€l‰Í½ÕÉ”é•¼éÉ•…‰t°(€€€€€€€€€€€M¥‘•™™•Ñ±…ÍÍ¥™¥…Ñ¥½¸¹aQI91}I°(€€€€€€€€¤°(€€€€€€€€ (€€€€€€€€€€€€‰Ù…±¥‘…Ñ•}•½}…•ÍÍ¥½¹Ìˆ°(€€€€€€€€€€€€ (€€€€€€€€€€€€€€€€‰Y…±¥‘…Ñ”½¹”Ñ¼™¥Ù”•áÁ±¥¥ĞM…•ÍÍ¥½¹Ì¥¹‘•Á•¹‘•¹Ñ±äÑ¡É½Õ Ñ¡”½™™¥¥…°€ˆ(€€€€€€€€€€€€€€€€‰<•ÍÍ¥½¸¥ÍÁ±…äµ…¡¥¹”µÉ•…‘…‰±”Ñ•áĞ½¹ÑÉ…Ğ¸UÍ”Ñ¡¥Ì‰½Õ¹‘•‰…Ñ €ˆ(€€€€€€€€€€€€€€€€‰¥µµ•‘¥…Ñ•±ä…™Ñ•ÈÍ•…É¡}•½}Í•É¥•Ì¸=¹±äÁÕ‰±¥}Ù…±¥É•ÍÕ±ÑÌµ…ä‰”É•½µµ•¹‘•¸ˆ(€€€€€€€€€€€€¤°(€€€€€€€€€€€•½•ÍÍ¥½¹Í%¹ÁÕĞ°(€€€€€€€€€€€•½Y…±¥‘…Ñ¥½¹	…Ñ¡=ÕÑÁÕĞ°(€€€€€€€€€€€‘¥Í½Ù•Éå}Í•ÉÙ¥”¹Ù…±¥‘…Ñ•}•½}…•ÍÍ¥½¹Ì°(€€€€€€€€€€€l‰Í½ÕÉ”é•¼éÉ•…‰t°(€€€€€€€€€€€M¥‘•™™•Ñ±…ÍÍ¥™¥…Ñ¥½¸¹aQI91}I°(€€€€€€€€¤°(€€€€€€€€ (€€€€€€€€€€€€‰¥¹ÍÁ•Ñ}•½}Í…µÁ±•}‘•Í¥¸ˆ°(€€€€€€€€€€€€ (€€€€€€€€€€€€€€€€‰áÑÉ…ĞÑÉ•…Ñµ•¹Ğ°½¹ÑÉ½°°½¹Ñ•áĞ°‘½Í”°Ñ¥µ”°É•Á±¥…Ñ”…¹€ˆ(€€€€€€€€€€€€€€€€‰µ¥ÍÍ¥¹œµµ•Ñ…‘…Ñ„•Ù¥‘•¹”¸ˆ(€€€€€€€€€€€€¤°(€€€€€€€€€€€•½•ÍÍ¥½¹%¹ÁÕĞ°(€€€€€€€€€€€•½M…µÁ±••Í¥¹=ÕÑÁÕĞ°(€€€€€€€€€€€‘¥Í½Ù•Éå}Í•ÉÙ¥”¹¥¹ÍÁ•Ñ}•½}Í…µÁ±•}‘•Í¥¸°(€€€€€€€€€€€l‰Í½ÕÉ”é•¼éÉ•…‰t°(€€€€€€€€€€€M¥‘•™™•Ñ±…ÍÍ¥™¥…Ñ¥½¸¹aQI91}I°(€€€€€€€€¤°(€€€€€€€€ (€€€€€€€€€€€€‰¥¹ÍÁ•Ñ}•½}…¹‘¥‘…Ñ•Ìˆ°(€€€€€€€€€€€€ (€€€€€€€€€€€€€€€€‰%¹ÍÁ•Ğ½¹”Ñ¼™¥Ù”ÁÕ‰±¥}Ù…±¥<…¹‘¥‘…Ñ•Ì…Ì…¸¥¹‘•Á•¹‘•¹Ğ‰½Õ¹‘•‰…Ñ ¸€ˆ(€€€€€€€€€€€€€€€€‰I•ÑÕÉ¸½µÁ…ĞÙ•É¥™¥•µ•Ñ…‘…Ñ„°ÑÉ•…Ñµ•¹Ğ½½¹ÑÉ½°…¹Í…µÁ±”µ‘•Í¥¸™…ÑÌ°€ˆ(€€€€€€€€€€€€€€€€‰•Ù¥‘•¹”É•™•É•¹•Ì°…¹•áÁ±¥¥ĞÕ¹•ÉÑ…¥¹Ñäİ¥Ñ¡½ÕĞ‘•¥‘¥¹œÍÕ¥Ñ…‰¥±¥Ñä¸ˆ(€€€€€€€€€€€€¤°(€€€€€€€€€€€•½•ÍÍ¥½¹Í%¹ÁÕĞ°(€€€€€€€€€€€•½…¹‘¥‘…Ñ•Í%¹ÍÁ•Ñ¥½¹=ÕÑÁÕĞ°(€€€€€€€€€€€‘¥Í½Ù•Éå}Í•ÉÙ¥”¹¥¹ÍÁ•Ñ}•½}…¹‘¥‘…Ñ•Ì°(€€€€€€€€€€€l‰Í½ÕÉ”é•¼éÉ•…‰t°(€€€€€€€€€€€M¥‘•™™•Ñ±…ÍÍ¥™¥…Ñ¥½¸¹aQI91}I°(€€€€€€€€¤°(€€€€€€€€ (€€€€€€€€€€€€‰™•Ñ¡}ÁÕ‰±¥…Ñ¥½¹}µ•Ñ…‘…Ñ„ˆ°(€€€€€€€€€€€€‰•Ñ ‰¥‰±¥½É…Á¡¥Œµ•Ñ…‘…Ñ„½¹±ä™½È‘…Ñ…Í•Ğµ±¥¹­•AÕ‰5•¥‘•¹Ñ¥™¥•ÉÌ¸ˆ°(€€€€€€€€€€€AÕ‰±¥…Ñ¥½¹5•Ñ…‘…Ñ…%¹ÁÕĞ°(€€€€€€€€€€€AÕ‰±¥…Ñ¥½¹5•Ñ…‘…Ñ…=ÕÑÁÕĞ°(€€€€€€€€€€€‘¥Í½Ù•Éå}Í•ÉÙ¥”¹™•Ñ¡}ÁÕ‰±¥…Ñ¥½¹}µ•Ñ…‘…Ñ„°(€€€€€€€€€€€l‰Í½ÕÉ”éÁÕ‰µ•é±¥¹­•‰t°(€€€€€€€€€€€M¥‘•™™•Ñ±…ÍÍ¥™¥…Ñ¥½¸¹aQI91}I°(€€€€€€€€¤°(€€€€€€€€ (€€€€€€€€€€€€‰½µÁ…É•}‘…Ñ…Í•Ñ}…¹‘¥‘…Ñ•Ìˆ°(€€€€€€€€€€€€‰•Ñ•Éµ¥¹¥ÍÑ¥…±±ä¹½Éµ…±¥é”…¹½µÁ…É”…±É•…‘äÉ•ÑÉ¥•Ù•…¹‘¥‘…Ñ”µ•Ñ…‘…Ñ„¸ˆ°(€€€€€€€€€€€½µÁ…É•…Ñ…Í•Ñ…¹‘¥‘…Ñ•Í%¹ÁÕĞ°(€€€€€€€€€€€½µÁ…É•…Ñ…Í•Ñ…¹‘¥‘…Ñ•Í=ÕÑÁÕĞ°(€€€€€€€€€€€‘¥Í½Ù•Éå}Í•ÉÙ¥”¹½µÁ…É•}‘…Ñ…Í•Ñ}…¹‘¥‘…Ñ•Ì°(€€€€€€€€€€€l‰Í½ÕÉ”éµ•Ñ…‘…Ñ„é½µÁ…É”‰t°(€€€€€€€€€€€M¥‘•™™•Ñ±…ÍÍ¥™¥…Ñ¥½¸¹9=9°(€€€€€€€€¤°(€€€t(€€€™½È¹…µ”°‘•ÍÉ¥ÁÑ¥½¸°¥¹ÁÕÑ}µ½‘•°°½ÕÑÁÕÑ}µ½‘•°°¥µÁ±•µ•¹Ñ…Ñ¥½¸°Á•Éµ¥ÍÍ¥½¹Ì°•™™•Ğ¥¸ÍÁ•Ìè(€€€€€€€É•¥ÍÑÉä¹É•¥ÍÑ•È (€€€€€€€€€€€I•¥ÍÑ•É•‘Q½½° (€€€€€€€€€€€€€€€Q½½±•™¥¹¥Ñ¥½¸ (€€€€€€€€€€€€€€€€€€€¹…µ”õ¹…µ”°(€€€€€€€€€€€€€€€€€€€‘•ÍÉ¥ÁÑ¥½¸õ‘•ÍÉ¥ÁÑ¥½¸°(€€€€€€€€€€€€€€€€€€€¥¹ÁÕÑ}Í¡•µ…}¹…µ”õ¥¹ÁÕÑ}µ½‘•°¹}}¹…µ•}|°(€€€€€€€€€€€€€€€€€€€½ÕÑÁÕÑ}Í¡•µ…}¹…µ”õ½ÕÑÁÕÑ}µ½‘•°¹}}¹…µ•}|°(€€€€€€€€€€€€€€€€€€€É•ÅÕ¥É•‘}Á•Éµ¥ÍÍ¥½¹ÌõÁ•Éµ¥ÍÍ¥½¹Ì°(€€€€€€€€€€€€€€€€€€€Í¥‘•}•™™•Ğõ•™™•Ğ°(€€€€€€€€€€€€€€€€€€€¥‘•µÁ½Ñ•¹äõ%‘•µÁ½Ñ•¹å±…ÍÍ¥™¥…Ñ¥½¸¹%5A=Q9Q}]%Q!}-d°(€€€€€€€€€€€€€€€€€€€Ñ¥µ•½ÕÑ}Í•½¹‘ÌôÌÀ¸À°(€€€€€€€€€€€€€€€€€€€…±±½İ•‘}İ½É­™±½İ}ÍÑ…•Ìõ‘¥Í½Ù•Éä°(€€€€€€€€€€€€€€€€€€€¥µÁ±•µ•¹Ñ…Ñ¥½¹}Ù•ÉÍ¥½¸ô‰Á¡…Í”ÄµØÄˆ°(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€¥¹ÁÕÑ}µ½‘•°°(€€€€€€€€€€€€€€€½ÕÑÁÕÑ}µ½‘•°°(€€€€€€€€€€€€€€€¥µÁ±•µ•¹Ñ…Ñ¥½¸°(€€€€€€€€€€€€€€€½¹Ñ•áÑÕ…°õQÉÕ”°(€€€€€€€€€€€€€€€…ÉÕµ•¹Ñ}¹½Éµ…±¥é•Èô (€€€€€€€€€€€€€€€€€€€¹½Éµ…±¥é•}•½}Í•…É¡}…ÉÕµ•¹ÑÌ¥˜¹…µ”€ôô€‰Í•…É¡}•½}Í•É¥•Ìˆ•±Í”9½¹”(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€¤(€€€€€€€€¤(€€€É•ÑÕÉ¸•áÑ•¹‘}ÑÉ…¥¹¥¹}‘…Ñ…Í•Ñ}Ñ½½±}É•¥ÍÑÉä¡É•¥ÍÑÉä¤(()‘•˜•áÑ•¹‘}ÑÉ…¥¹¥¹}‘…Ñ…Í•Ñ}Ñ½½±}É•¥ÍÑÉä (€€€É•¥ÍÑÉäèQ½½±I•¥ÍÑÉä°…‘…ÁÑ•É}É•¥ÍÑÉäõ9½¹”(¤€´øQ½½±I•¥ÍÑÉäè(€€€€ˆˆ‰‘É•Ù¥•İ•°ÑåÁ•µÕ±Ñ¤µÍ½ÕÉ”Ñ½½±Ìİ¥Ñ¡½ÕĞ•¹…‰±¥¹œ…É‰¥ÑÉ…Éä¹•Ñİ½É¬…•ÍÌ¸ˆˆˆ((€€€™É½´€¹ÑÉ…¥¹¥¹}‘…Ñ…Í•Ñ}Ñ½½±Ì¥µÁ½ÉĞ€ (€€€€€€€Ñ¥Ù¥ÑåM½ÕÉ•M•…É¡%¹ÁÕĞ°(€€€€€€€½Ù•É…•Õ‘¥Ñ%¹ÁÕĞ°(€€€€€€€½Ù•É…•Õ‘¥Ñ=ÕÑÁÕĞ°(€€€€€€€%‘•¹Ñ¥Ñå¥•±‘%¹ÍÁ•Ñ¥½¹%¹ÁÕĞ°(€€€€€€€%‘•¹Ñ¥Ñå¥•±‘%¹ÍÁ•Ñ¥½¹=ÕÑÁÕĞ°(€€€€€€€5…ÁÁ¥¹5…¹¥™•ÍÑ%¹ÁÕĞ°(€€€€€€€5…ÁÁ¥¹5…¹¥™•ÍÑ=ÕÑÁÕĞ°(€€€€€€€I•½É‘Ù…¥±…‰¥±¥Ñå%¹ÁÕĞ°(€€€€€€€I•½É‘Ù…¥±…‰¥±¥Ñå=ÕÑÁÕĞ°(€€€€€€€M½ÕÉ•‘…ÁÑ•ÉI•¥ÍÑÉä°(€€€€€€€M½ÕÉ•M•…É¡=ÕÑÁÕĞ°(€€€€€€€M½ÕÉ•Y…±¥‘…Ñ¥½¹=ÕÑÁÕĞ°(€€€€€€€MÑ…‰±•M½ÕÉ•%‘•¹Ñ¥™¥•É%¹ÁÕĞ°(€€€€€€€QÉ…¹ÍÉ¥ÁÑ½µ¥M½ÕÉ•M•…É¡%¹ÁÕĞ°(€€€€€€€…Õ‘¥Ñ}½Ù•É…”°(€€€€€€€‰Õ¥±‘}½µÁ½Õ¹‘}µ…ÁÁ¥¹}µ…¹¥™•ÍĞ°(€€€€€€€¥¹ÍÁ•Ñ}Í½ÕÉ•}¥‘•¹Ñ¥Ñå}™¥•±‘Ì°(€€€€€€€¥¹ÍÁ•Ñ}Í½ÕÉ•}É•½É‘}…Ù…¥±…‰¥±¥Ñä°(€€€€¤((€€€…‘…ÁÑ•ÉÌ€ô…‘…ÁÑ•É}É•¥ÍÑÉä½ÈM½ÕÉ•‘…ÁÑ•ÉI•¥ÍÑÉä ¤(€€€‘¥Í½Ù•Éå}ÍÑ…•Ì€ôl(€€€€€€€]½É­™±½İMÑ…Ñ”¹%M=YI%9}Q%Y%Qe}Y%9°(€€€€€€€]½É­™±½İMÑ…Ñ”¹%M=YI%9}QI9MI%AQ=5%}Y%9°(€€€€€€€]½É­™±½İMÑ…Ñ”¹%M=YI%9}%9Q%Qe}9}MQIUQUI}M=UIL°(€€€€€€€]½É­™±½İMÑ…Ñ”¹%M=YI%9}MUAA=IQ%9}5QQ°(€€€€€€€]½É­™±½İMÑ…Ñ”¹Y1%Q%9}%M=YI}M=UIL°(€€€€€€€]½É­™±½İMÑ…Ñ”¹A}%IQ}%M=YId°(€€€t(€€€•Ù…±Õ…Ñ¥½¹}ÍÑ…•Ì€ôl(€€€€€€€]½É­™±½İMÑ…Ñ”¹Y1UQ%9})=%9	%1%Qd°(€€€€€€€]½É­™±½İMÑ…Ñ”¹%9Q%e%9}MM5	1e}AL°(€€€t((€€€‘•˜É•¥ÍÑ•È (€€€€€€€¹…µ”°(€€€€€€€‘•ÍÉ¥ÁÑ¥½¸°(€€€€€€€¥¹ÁÕÑ}µ½‘•°°(€€€€€€€½ÕÑÁÕÑ}µ½‘•°°(€€€€€€€¥µÁ±•µ•¹Ñ…Ñ¥½¸°(€€€€€€€ÍÑ…•Ì°(€€€€€€€•™™•ĞõM¥‘•™™•Ñ±…ÍÍ¥™¥…Ñ¥½¸¹9=9°(€€€€¤è(€€€€€€€É•¥ÍÑÉä¹É•¥ÍÑ•È (€€€€€€€€€€€I•¥ÍÑ•É•‘Q½½° (€€€€€€€€€€€€€€€Q½½±•™¥¹¥Ñ¥½¸ (€€€€€€€€€€€€€€€€€€€¹…µ”õ¹…µ”°(€€€€€€€€€€€€€€€€€€€‘•ÍÉ¥ÁÑ¥½¸õ‘•ÍÉ¥ÁÑ¥½¸°(€€€€€€€€€€€€€€€€€€€¥¹ÁÕÑ}Í¡•µ…}¹…µ”õ¥¹ÁÕÑ}µ½‘•°¹}}¹…µ•}|°(€€€€€€€€€€€€€€€€€€€½ÕÑÁÕÑ}Í¡•µ…}¹…µ”õ½ÕÑÁÕÑ}µ½‘•°¹}}¹…µ•}|°(€€€€€€€€€€€€€€€€€€€É•ÅÕ¥É•‘}Á•Éµ¥ÍÍ¥½¹Ìõm˜‰ÑÉ…¥¹¥¹œµ‘…Ñ…Í•Ğéí¹…µ•ô‰t°(€€€€€€€€€€€€€€€€€€€Í¥‘•}•™™•Ğõ•™™•Ğ°(€€€€€€€€€€€€€€€€€€€¥‘•µÁ½Ñ•¹äõ%‘•µÁ½Ñ•¹å±…ÍÍ¥™¥…Ñ¥½¸¹%5A=Q9Q}]%Q!}-d°(€€€€€€€€€€€€€€€€€€€Ñ¥µ•½ÕÑ}Í•½¹‘ÌôÌÀ¸À°(€€€€€€€€€€€€€€€€€€€…±±½İ•‘}İ½É­™±½İ}ÍÑ…•ÌõÍÑ…•Ì°(€€€€€€€€€€€€€€€€€€€¥µÁ±•µ•¹Ñ…Ñ¥½¹}Ù•ÉÍ¥½¸ô‰ÑÉ…¥¹¥¹œµ‘…Ñ…Í•ĞµØÄˆ°(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€¥¹ÁÕÑ}µ½‘•°°(€€€€€€€€€€€€€€€½ÕÑÁÕÑ}µ½‘•°°(€€€€€€€€€€€€€€€¥µÁ±•µ•¹Ñ…Ñ¥½¸°(€€€€€€€€€€€€¤(€€€€€€€€¤((€€€É•¥ÍÑ•È (€€€€€€€€‰Í•…É¡}…Ñ¥Ù¥Ñå}Í½ÕÉ•Ìˆ°(€€€€€€€€‰M•…É ½¹™¥ÕÉ•É•Ù¥•İ•…Ñ¥Ù¥Ñä…‘…ÁÑ•ÉÌİ¥Ñ „ÑåÁ•Ñ…É•ĞÉ•ÅÕ•ÍĞ¸ˆ°(€€€€€€€Ñ¥Ù¥ÑåM½ÕÉ•M•…É¡%¹ÁÕĞ°(€€€€€€€M½ÕÉ•M•…É¡=ÕÑÁÕĞ°(€€€€€€€…‘…ÁÑ•ÉÌ¹Í•…É¡}…Ñ¥Ù¥Ñä°(€€€€€€€‘¥Í½Ù•Éå}ÍÑ…•Ì°(€€€€€€€M¥‘•™™•Ñ±…ÍÍ¥™¥…Ñ¥½¸¹aQI91}I°(€€€€¤(€€€É•¥ÍÑ•È (€€€€€€€€‰Í•…É¡}ÑÉ…¹ÍÉ¥ÁÑ½µ¥}Í½ÕÉ•Ìˆ°(€€€€€€€€‰M•…É ½¹™¥ÕÉ•É•Ù¥•İ•¡•µ¥…°µÁ•ÉÑÕÉ‰…Ñ¥½¸ÑÉ…¹ÍÉ¥ÁÑ½µ¥Œ…‘…ÁÑ•ÉÌ¸ˆ°(€€€€€€€QÉ…¹ÍÉ¥ÁÑ½µ¥M½ÕÉ•M•…É¡%¹ÁÕĞ°(€€€€€€€M½ÕÉ•M•…É¡=ÕÑÁÕĞ°(€€€€€€€…‘…ÁÑ•ÉÌ¹Í•…É¡}ÑÉ…¹ÍÉ¥ÁÑ½µ¥Ì°(€€€€€€€‘¥Í½Ù•Éå}ÍÑ…•Ì°(€€€€€€€M¥‘•™™•Ñ±…ÍÍ¥™¥…Ñ¥½¸¹aQI91}I°(€€€€¤(€€€Ù…±¥‘…Ñ¥½¹}½Á•É…Ñ¥½¹Ì€ôl(€€€€€€€€‰Ù…±¥‘…Ñ•}…Ñ¥Ù¥Ñå}Í½ÕÉ”ˆ°(€€€€€€€€‰™•Ñ¡}…Ñ¥Ù¥Ñå}Í½ÕÉ•}µ•Ñ…‘…Ñ„ˆ°(€€€€€€€€‰¥¹ÍÁ•Ñ}…Ñ¥Ù¥Ñå}É•ÍÕ±Ñ}…Ù…¥±…‰¥±¥Ñäˆ°(€€€€€€€€‰¥¹ÍÁ•Ñ}…Ñ¥Ù¥Ñå}¥‘•¹Ñ¥™¥•É}™¥•±‘Ìˆ°(€€€€€€€€‰ÍÕµµ…É¥é•}…Ñ¥Ù¥Ñå}½ÕÑ½µ•Ìˆ°(€€€€€€€€‰¥¹ÍÁ•Ñ}½Õ¹Ñ•É}ÍÉ••¹}É•±…Ñ¥½¹Í¡¥ÁÌˆ°(€€€€€€€€‰Ù…±¥‘…Ñ•}ÑÉ…¹ÍÉ¥ÁÑ½µ¥}Í½ÕÉ”ˆ°(€€€€€€€€‰™•Ñ¡}ÑÉ…¹ÍÉ¥ÁÑ½µ¥}Í½ÕÉ•}µ•Ñ…‘…Ñ„ˆ°(€€€€€€€€‰¥¹ÍÁ•Ñ}Á•ÉÑÕÉ‰…Ñ¥½¹}‘•Í¥¸ˆ°(€€€€€€€€‰¥¹ÍÁ•Ñ}ÑÉ…¹ÍÉ¥ÁÑ½µ¥}¥‘•¹Ñ¥Ñå}™¥•±‘Ìˆ°(€€€€€€€€‰¥¹ÍÁ•Ñ}Í¥¹…ÑÕÉ•}½¹‘¥Ñ¥½¹Ìˆ°(€€€€€€€€‰¥¹ÍÁ•Ñ}ÁÉ½•ÍÍ•‘}µ…ÑÉ¥á}…Ù…¥±…‰¥±¥Ñäˆ°(€€€€€€€€‰¥¹ÍÁ•Ñ}É…İ}µ…ÑÉ¥á}…Ù…¥±…‰¥±¥Ñäˆ°(€€€€€€€€‰¥¹ÍÁ•Ñ}™•…ÑÕÉ•}Í¡•µ„ˆ°(€€€t(€€€™½È½Á•É…Ñ¥½¸¥¸Ù…±¥‘…Ñ¥½¹}½Á•É…Ñ¥½¹Ìè(€€€€€€€É•¥ÍÑ•È (€€€€€€€€€€€½Á•É…Ñ¥½¸°(€€€€€€€€€€€€‰%¹ÍÁ•Ğ½¹”ÍÑ…‰±”¥‘•¹Ñ¥™¥•ÈÑ¡É½Õ ¥ÑÌ½¹™¥ÕÉ•É•Ù¥•İ•Í½ÕÉ”…‘…ÁÑ•È¸ˆ°(€€€€€€€€€€€MÑ…‰±•M½ÕÉ•%‘•¹Ñ¥™¥•É%¹ÁÕĞ°(€€€€€€€€€€€M½ÕÉ•Y…±¥‘…Ñ¥½¹=ÕÑÁÕĞ°(€€€€€€€€€€€…‘…ÁÑ•ÉÌ¹Ù…±¥‘…Ñ”°(€€€€€€€€€€€‘¥Í½Ù•Éå}ÍÑ…•Ì°(€€€€€€€€€€€M¥‘•™™•Ñ±…ÍÍ¥™¥…Ñ¥½¸¹aQI91}I°(€€€€€€€€¤(€€€É•¥ÍÑ•È (€€€€€€€€‰¥¹ÍÁ•Ñ}Í½ÕÉ•}¥‘•¹Ñ¥Ñå}™¥•±‘Ìˆ°(€€€€€€€€‰½µÁ…É”‘•±…É•Í½ÕÉ”¥‘•¹Ñ¥Ñä™¥•±‘Ìİ¥Ñ¡½ÕĞÉ•Í½±Ù¥¹œ½µÁ½Õ¹‘Ì¸ˆ°(€€€€€€€%‘•¹Ñ¥Ñå¥•±‘%¹ÍÁ•Ñ¥½¹%¹ÁÕĞ°(€€€€€€€%‘•¹Ñ¥Ñå¥•±‘%¹ÍÁ•Ñ¥½¹=ÕÑÁÕĞ°(€€€€€€€¥¹ÍÁ•Ñ}Í½ÕÉ•}¥‘•¹Ñ¥Ñå}™¥•±‘Ì°(€€€€€€€l©‘¥Í½Ù•Éå}ÍÑ…•Ì°€©•Ù…±Õ…Ñ¥½¹}ÍÑ…•Ít°(€€€€¤(€€€É•¥ÍÑ•È (€€€€€€€€‰¥¹ÍÁ•Ñ}Í½ÕÉ•}É•½É‘}…Ù…¥±…‰¥±¥Ñäˆ°(€€€€€€€€‰±…ÍÍ¥™äÙ•É¥™¥•É•½É…•ÍÌİ¥Ñ¡½ÕĞ‘½İ¹±½…‘¥¹œÍ½ÕÉ”Ñ…‰±•Ì¸ˆ°(€€€€€€€I•½É‘Ù…¥±…‰¥±¥Ñå%¹ÁÕĞ°(€€€€€€€I•½É‘Ù…¥±…‰¥±¥Ñå=ÕÑÁÕĞ°(€€€€€€€¥¹ÍÁ•Ñ}Í½ÕÉ•}É•½É‘}…Ù…¥±…‰¥±¥Ñä°(€€€€€€€l©‘¥Í½Ù•Éå}ÍÑ…•Ì°€©•Ù…±Õ…Ñ¥½¹}ÍÑ…•Ít°(€€€€¤(€€€™½È½Á•É…Ñ¥½¸¥¸€ ‰‰Õ¥±‘}½µÁ½Õ¹‘}µ…ÁÁ¥¹}µ…¹¥™•ÍĞˆ°€‰É•Í½±Ù•}½µÁ½Õ¹‘}¥‘•¹Ñ¥Ñå}Í…µÁ±”ˆ¤è(€€€€€€€É•¥ÍÑ•È (€€€€€€€€€€€½Á•É…Ñ¥½¸°(€€€€€€€€€€€€‰	Õ¥±„‰½Õ¹‘•‘•Ñ•Éµ¥¹¥ÍÑ¥Œ¥‘•¹Ñ¥™¥•Èµ…ÁÁ¥¹œµ…¹¥™•ÍĞ™É½´ÍÕÁÁ±¥•É•½É‘Ì¸ˆ°(€€€€€€€€€€€5…ÁÁ¥¹5…¹¥™•ÍÑ%¹ÁÕĞ°(€€€€€€€€€€€5…ÁÁ¥¹5…¹¥™•ÍÑ=ÕÑÁÕĞ°(€€€€€€€€€€€‰Õ¥±‘}½µÁ½Õ¹‘}µ…ÁÁ¥¹}µ…¹¥™•ÍĞ°(€€€€€€€€€€€l©‘¥Í½Ù•Éå}ÍÑ…•Ì°€©•Ù…±Õ…Ñ¥½¹}ÍÑ…•Ít°(€€€€€€€€¤(€€€É•¥ÍÑ•È (€€€€€€€€‰…Õ‘¥Ñ}½Ù•É…”ˆ°(€€€€€€€€‰½µÁÕÑ”•á…Ğ‰½Õ¹‘•½Ù•É…”½¹±äİ¡•¸™Õ±°…ÁÁÉ½Ù•¥‘•¹Ñ¥™¥•ÈÑ…‰±•Ì…É”ÍÕÁÁ±¥•¸ˆ°(€€€€€€€½Ù•É…•Õ‘¥Ñ%¹ÁÕĞ°(€€€€€€€½Ù•É…•Õ‘¥Ñ=ÕÑÁÕĞ°(€€€€€€€…Õ‘¥Ñ}½Ù•É…”°(€€€€€€€•Ù…±Õ…Ñ¥½¹}ÍÑ…•Ì°(€€€€¤(€€€É•ÑÕÉ¸É•¥ÍÑÉä(