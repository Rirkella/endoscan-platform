"""Typed, stage-bound tool registry with no shell, arbitrary paths, or network access."""

from __future__ import annotations

import hashlib
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
    ToolAccessPhase,
    ToolCallStatus,
    ToolDefinition,
    ToolInvocation,
    ToolInvocationFailureDiagnostic,
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
SEARCH_TERM_NORMALIZATION_POLICY_VERSION = "search-term-normalization-v1"
ACTIVITY_MODALITY_POLICY_VERSION = "activity-modality-v1"


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


def normalize_activity_search_arguments(arguments: dict[str, Any]) -> NormalizedToolArguments:
    """Canonicalize only harmless aliases for one controlled activity modality."""

    value = arguments.get("endpoint_modality")
    if not isinstance(value, str):
        return NormalizedToolArguments(arguments=dict(arguments))
    comparison = " ".join(value.strip().casefold().replace("_", " ").split())
    aliases = {
        "binding": "binding",
        "binding assay": "binding",
        "agonism": "agonism",
        "agonist": "agonism",
        "agonist activity": "agonism",
        "antagonism": "antagonism",
        "antagonist": "antagonism",
        "antagonist activity": "antagonism",
    }
    canonical = aliases.get(comparison, comparison)
    normalized = dict(arguments)
    normalized["endpoint_modality"] = canonical
    warnings: tuple[ToolNormalizationWarning, ...] = ()
    if canonical != value:
        warnings = (
            ToolNormalizationWarning(
                code="activity_modality_alias_canonicalized",
                field="endpoint_modality",
                original_index=0,
                original=value,
                normalized=canonical,
                policy_version=ACTIVITY_MODALITY_POLICY_VERSION,
            ),
        )
    return NormalizedToolArguments(arguments=normalized, warnings=warnings)


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
            raise ValueError("Production-write tools are prohibited in the offline tool registry")
        self._tools[tool.definition.name] = tool

    def get(self, name: str) -> RegisteredTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise AgentPolicyError(f"Tool {name!r} is not allowlisted.") from exc

    def definitions(self) -> list[ToolDefinition]:
        return [self._tools[name].definition for name in sorted(self._tools)]

    @staticmethod
    def _failure_diagnostic(
        invocation: ToolInvocation,
        tool: RegisteredTool | None,
        original_arguments: dict[str, Any],
        normalized_arguments: dict[str, Any],
        exc: Exception,
        *,
        invocation_stage: str,
        validation_error_category: str,
        adapter_resolution_status: str = "not_started",
        field_errors: list[dict[str, str]] | None = None,
        safe_message: str,
    ) -> ToolInvocationFailureDiagnostic:
        schema = tool.input_model.model_json_schema() if tool is not None else {}
        schema_hash = hashlib.sha256(
            json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ToolInvocationFailureDiagnostic(
            tool_name=invocation.tool_name,
            tool_schema_version=(
                tool.definition.implementation_version if tool is not None else "unresolved"
            ),
            tool_schema_hash=schema_hash,
            agent_role=str(invocation.run_context.get("agent_role") or "unknown-agent"),
            invocation_stage=invocation_stage,
            supplied_argument_field_names=sorted(original_arguments),
            normalized_argument_field_names=sorted(normalized_arguments),
            validation_error_category=validation_error_category,
            field_errors=list(field_errors or [])[:30],
            dependency_status=str(
                invocation.run_context.get("dependency_status") or "not_applicable"
            )[:120],
            adapter_resolution_status=adapter_resolution_status,
            source_transport_started=bool(getattr(exc, "source_transport_started", False)),
            exception_class=type(exc).__name__,
            safe_message=safe_message[:1000],
            retryable=bool(getattr(exc, "retryable", False)),
        )

    def invoke(self, invocation: ToolInvocation) -> ToolResult:
        started = time.monotonic()
        original_arguments = dict(invocation.arguments)
        normalized = NormalizedToolArguments(arguments=original_arguments)
        tool: RegisteredTool | None = None
        try:
            tool = self.get(invocation.tool_name)
            if (
                tool.definition.access_phase is ToolAccessPhase.POST_APPROVAL_EXTRACTION
                and invocation.workflow_stage is not WorkflowState.ASSEMBLING_APPROVED_DATASET
            ):
                raise AgentPolicyError(
                    "Post-approval extraction requires ASSEMBLING_APPROVED_DATASET."
                )
            if invocation.workflow_stage not in tool.definition.allowed_workflow_stages:
                raise AgentPolicyError("Tool is prohibited in the current workflow stage.")
            if not set(tool.definition.required_permissions).issubset(invocation.permission_scope):
                raise AgentPolicyError("Tool permission scope is insufficient.")
            if tool.argument_normalizer is not None:
                normalized = tool.argument_normalizer(original_arguments)
            typed_input = tool.input_model.model_validate(normalized.arguments)
        except (ValidationError, AgentPolicyError) as exc:
            if isinstance(exc, ValidationError):
                field_errors = [
                    {
                        "field": ".".join(str(item) for item in error.get("loc", ())) or "input",
                        "category": str(error.get("type", "validation_error"))[:120],
                        "message": str(error.get("msg", "Invalid value."))[:300],
                    }
                    for error in exc.errors(include_input=False, include_url=False)[:30]
                ]
                safe_message = "Tool input validation failed for: " + ", ".join(
                    item["field"] for item in field_errors
                )
                stage = "input_validation"
                category = "input_schema_validation"
            else:
                field_errors = []
                safe_message = str(exc)[:1000]
                stage = "policy_validation"
                category = "tool_policy_rejected"
            diagnostic = self._failure_diagnostic(
                invocation,
                tool,
                original_arguments,
                normalized.arguments,
                exc,
                invocation_stage=stage,
                validation_error_category=category,
                adapter_resolution_status="not_started",
                field_errors=field_errors,
                safe_message=safe_message,
            )
            return ToolResult(
                tool_name=invocation.tool_name,
                status=ToolCallStatus.PROHIBITED
                if isinstance(exc, AgentPolicyError)
                else ToolCallStatus.FAILED,
                error=NormalizedAgentError(
                    code="prohibited_tool"
                    if isinstance(exc, AgentPolicyError)
                    else "tool_input_invalid",
                    safe_message=safe_message,
                    retryable=False,
                    category="policy" if isinstance(exc, AgentPolicyError) else "validation",
                    tool_diagnostic=diagnostic,
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
                        safe_message=str(exc),
                        retryable=exc.retryable,
                        category="source_tool",
                        source_diagnostic=diagnostic,
                    ),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    original_arguments=original_arguments,
                    normalized_arguments=normalized.arguments,
                    normalization_warnings=list(normalized.warnings),
                    source_diagnostic=diagnostic,
                )
            except Exception as exc:
                safe_message = str(
                    getattr(exc, "safe_message", "Tool invocation failed before source transport.")
                )[:1000]
                invocation_stage = str(getattr(exc, "invocation_stage", "tool_implementation"))
                validation_category = str(
                    getattr(exc, "validation_error_category", "tool_implementation_failure")
                )
                diagnostic = self._failure_diagnostic(
                    invocation,
                    tool,
                    original_arguments,
                    normalized.arguments,
                    exc,
                    invocation_stage=invocation_stage,
                    validation_error_category=validation_category,
                    adapter_resolution_status=str(
                        getattr(exc, "adapter_resolution_status", "not_started")
                    ),
                    field_errors=getattr(exc, "field_errors", []),
                    safe_message=safe_message,
                )
                logger.warning(
                    "tool_failure tool_name=%s exception_class=%s stage=%s category=%s "
                    "source_transport_started=%s retryable=false",
                    invocation.tool_name,
                    type(exc).__name__,
                    diagnostic.invocation_stage,
                    diagnostic.validation_error_category,
                    diagnostic.source_transport_started,
                )
                return ToolResult(
                    tool_name=invocation.tool_name,
                    status=ToolCallStatus.FAILED,
                    error=NormalizedAgentError(
                        code="tool_failure",
                        safe_message=safe_message,
                        retryable=False,
                        category="tool",
                        tool_diagnostic=diagnostic,
                    ),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    original_arguments=original_arguments,
                    normalized_arguments=normalized.arguments,
                    normalization_warnings=list(normalized.warnings),
                )
        return ToolResult(
            tool_name=invocation.tool_name,
            status=ToolCallStatus.COMPLETED,
            output=output,
            duration_ms=int((time.monotonic() - started) * 1000),
            original_arguments=original_arguments,
            normalized_arguments=normalized.arguments,
            normalization_warnings=list(normalized.warnings),
        )


def offline_tool_registry(repo_root: Path) -> ToolRegistry:
    root = Path(repo_root).resolve()
    registry = ToolRegistry()
    discovery = [WorkflowState.DISCOVERING_DATA]

    def inspect_registry(_request: RepositoryInput) -> RegistryInspection:
        data = json.loads(
            (root / "registry" / "models" / "endpoints.json").read_text(encoding="utf-8")
        )
        records = data if isinstance(data, list) else data.get("endpoints", [])
        endpoints = [
            {
                "endpoint_id": item.get("endpoint_id"),
                "display_name": item.get("display_name") or item.get("name"),
                "status": item.get("status"),
                "input_type": item.get("input_type"),
            }
            for item in records
        ]
        return RegistryInspection(endpoint_count=len(endpoints), endpoints=endpoints)

    def list_adapters(_request: RepositoryInput) -> AdapterInventory:
        paths = sorted(
            path.relative_to(root).as_posix()
            for path in (root / "packages").rglob("*.py")
            if "adapter" in path.name.lower() or "staging" in path.parts
        )
        return AdapterInventory(
            adapters=[{"path": path, "mode": "read-only inspection"} for path in paths[:40]]
        )

    def summarize_pipelines(request: PipelineSummaryInput) -> PipelineSummary:
        summaries = []
        for endpoint_id in request.endpoint_ids:
            config_paths = sorted(
                path.relative_to(root).as_posix()
                for path in (root / "pipelines" / "endpoints" / endpoint_id).glob("config*.yaml")
            )
            summaries.append(
                {
                    "endpoint_id": endpoint_id,
                    "runner": "pipelines/endpoints/ER/run.py",
                    "configs": config_paths,
                    "grouped_evaluation": True,
                    "production_registry_write_allowed": False,
                }
            )
        return PipelineSummary(endpoints=summaries)

    def candidates(request: CandidateFixtureInput) -> CandidateFixtureOutput:
        return CandidateFixtureOutput(
            fixture_label="Prepared deterministic offline fixture — not live discovery",
            candidates=[
                {
                    "candidate_id": "offline-fixture-candidate-a",
                    "title": f"Prepared metadata candidate A for {request.endpoint_name}",
                    "source": "offline-fixture://prepared-fixture/a",
                    "accession_verified": False,
                    "license_verified": False,
                    "recommendation": "review-first",
                    "limitations": [
                        "Synthetic workflow fixture; not a scientific dataset recommendation.",
                        "Accession, controls, labels, and licence require future live "
                        "verification.",
                    ],
                },
                {
                    "candidate_id": "offline-fixture-candidate-b",
                    "title": f"Prepared metadata candidate B for {request.endpoint_name}",
                    "source": "offline-fixture://prepared-fixture/b",
                    "accession_verified": False,
                    "license_verified": False,
                    "recommendation": "alternative",
                    "limitations": [
                        "Synthetic workflow fixture; not a scientific dataset recommendation.",
                        "No data has been downloaded or approved for training.",
                    ],
                },
            ],
        )

    specs = [
        (
            "inspect_endpoint_registry",
            "Inspect committed endpoint registry metadata without mutation.",
            RepositoryInput,
            RegistryInspection,
            inspect_registry,
            ["registry:read"],
            SideEffectClassification.NONE,
        ),
        (
            "list_known_source_adapters",
            "List existing source and staging adapters from bounded repository paths.",
            RepositoryInput,
            AdapterInventory,
            list_adapters,
            ["repository:read"],
            SideEffectClassification.NONE,
        ),
        (
            "summarize_existing_endpoint_pipeline",
            "Summarize deterministic ER/AR pipeline configuration without running it.",
            PipelineSummaryInput,
            PipelineSummary,
            summarize_pipelines,
            ["repository:read"],
            SideEffectClassification.NONE,
        ),
        (
            "create_dataset_candidate_artifact",
            "Return clearly labelled prepared offline candidate fixtures.",
            CandidateFixtureInput,
            CandidateFixtureOutput,
            candidates,
            ["fixture:read"],
            SideEffectClassification.ARTIFACT_WRITE,
        ),
    ]
    for (
        name,
        description,
        input_model,
        output_model,
        implementation,
        permissions,
        side_effect,
    ) in specs:
        registry.register(
            RegisteredTool(
                ToolDefinition(
                    name=name,
                    description=description,
                    input_schema_name=input_model.__name__,
                    output_schema_name=output_model.__name__,
                    required_permissions=permissions,
                    side_effect=side_effect,
                    idempotency=IdempotencyClassification.IDEMPOTENT_WITH_KEY,
                    timeout_seconds=5.0,
                    allowed_workflow_stages=discovery,
                    implementation_version="offline-fixture-v1",
                ),
                input_model,
                output_model,
                implementation,
            )
        )
    return registry


def production_tool_registry(
    repo_root: Path,
    discovery_service,
    adapter_registry=None,
    *,
    lincs_metadata_provider=None,
    preapproval_provider_layer=None,
) -> ToolRegistry:
    """Offline tools plus bounded compatibility and staged discovery tools."""
    from .discovery_tools import (
        CompareDatasetCandidatesInput,
        CompareDatasetCandidatesOutput,
        GeoAccessionInput,
        GeoAccessionsInput,
        GeoCandidatesInspectionOutput,
        GeoSampleDesignOutput,
        GeoSeriesMetadataOutput,
        GeoValidationBatchOutput,
        PublicationMetadataInput,
        PublicationMetadataOutput,
        SearchGeoSeriesInput,
        SearchGeoSeriesOutput,
    )

    registry = offline_tool_registry(repo_root)
    discovery = [WorkflowState.DISCOVERING_DATA]
    specs = [
        (
            "search_geo_series",
            (
                "Search official NCBI GEO Series metadata with a typed bounded plan. "
                "The tool renders AND between concepts and OR within alternatives. "
                "For optional filters, omit the filter or return [] when no constraint "
                "is intended; never include empty strings, and provide only concrete "
                "biological terms."
            ),
            SearchGeoSeriesInput,
            SearchGeoSeriesOutput,
            discovery_service.search_geo_series,
            ["source:geo:read"],
            SideEffectClassification.EXTERNAL_READ,
        ),
        (
            "fetch_geo_series_metadata",
            "Fetch official GEO Series metadata for one validated GSE accession.",
            GeoAccessionInput,
            GeoSeriesMetadataOutput,
            discovery_service.fetch_geo_series_metadata,
            ["source:geo:read"],
            SideEffectClassification.EXTERNAL_READ,
        ),
        (
            "validate_geo_accessions",
            (
                "Validate one to five explicit GSE accessions independently through the official "
                "GEO Accession Display machine-readable text contract. Use this bounded batch "
                "immediately after search_geo_series. Only public_valid results may be recommended."
            ),
            GeoAccessionsInput,
            GeoValidationBatchOutput,
            discovery_service.validate_geo_accessions,
            ["source:geo:read"],
            SideEffectClassification.EXTERNAL_READ,
        ),
        (
            "inspect_geo_sample_design",
            (
                "Extract treatment, control, context, dose, time, replicate and "
                "missing-metadata evidence."
            ),
            GeoAccessionInput,
            GeoSampleDesignOutput,
            discovery_service.inspect_geo_sample_design,
            ["source:geo:read"],
            SideEffectClassification.EXTERNAL_READ,
        ),
        (
            "inspect_geo_candidates",
            (
                "Inspect one to five public_valid GEO candidates as an independent bounded batch. "
                "Return compact verified metadata, treatment/control and sample-design facts, "
                "evidence references, and explicit uncertainty without deciding suitability."
            ),
            GeoAccessionsInput,
            GeoCandidatesInspectionOutput,
            discovery_service.inspect_geo_candidates,
            ["source:geo:read"],
            SideEffectClassification.EXTERNAL_READ,
        ),
        (
            "fetch_publication_metadata",
            "Fetch bibliographic metadata only for dataset-linked PubMed identifiers.",
            PublicationMetadataInput,
            PublicationMetadataOutput,
            discovery_service.fetch_publication_metadata,
            ["source:pubmed:linked"],
            SideEffectClassification.EXTERNAL_READ,
        ),
        (
            "compare_dataset_candidates",
            "Deterministically normalize and compare already retrieved candidate metadata.",
            CompareDatasetCandidatesInput,
            CompareDatasetCandidatesOutput,
            discovery_service.compare_dataset_candidates,
            ["source:metadata:compare"],
            SideEffectClassification.NONE,
        ),
    ]
    for name, description, input_model, output_model, implementation, permissions, effect in specs:
        registry.register(
            RegisteredTool(
                ToolDefinition(
                    name=name,
                    description=description,
                    input_schema_name=input_model.__name__,
                    output_schema_name=output_model.__name__,
                    required_permissions=permissions,
                    side_effect=effect,
                    idempotency=IdempotencyClassification.IDEMPOTENT_WITH_KEY,
                    timeout_seconds=30.0,
                    allowed_workflow_stages=discovery,
                    implementation_version="discovery-v1",
                ),
                input_model,
                output_model,
                implementation,
                contextual=True,
                argument_normalizer=(
                    normalize_geo_search_arguments if name == "search_geo_series" else None
                ),
            )
        )
    return extend_training_dataset_tool_registry(
        registry,
        adapter_registry,
        lincs_metadata_provider=lincs_metadata_provider,
        preapproval_provider_layer=preapproval_provider_layer,
    )


def extend_training_dataset_tool_registry(
    registry: ToolRegistry,
    adapter_registry=None,
    *,
    lincs_metadata_provider=None,
    preapproval_provider_layer=None,
) -> ToolRegistry:
    """Add reviewed, typed multi-source tools without enabling arbitrary network access."""

    from .reviewed_source_adapters import (
        ActivityResultExtractionInput,
        ActivitySearchOperationInput,
        CompoundSourceOperationInput,
        ReviewedSourceAdapterRegistry,
        ReviewedSourceOperationInput,
        SupportingSourceOperationInput,
    )
    from .training_dataset import VerifiedSourceObservationBatch
    from .training_dataset_tools import (
        CoverageAuditInput,
        CoverageAuditOutput,
        IdentityFieldInspectionInput,
        IdentityFieldInspectionOutput,
        MappingManifestInput,
        MappingManifestOutput,
        RecordAvailabilityInput,
        RecordAvailabilityOutput,
        audit_coverage,
        build_compound_mapping_manifest,
        inspect_source_identity_fields,
        inspect_source_record_availability,
    )

    adapters = adapter_registry or ReviewedSourceAdapterRegistry()
    legacy_discovery_stages = [
        WorkflowState.DISCOVERING_ACTIVITY_EVIDENCE,
        WorkflowState.DISCOVERING_TRANSCRIPTOMIC_EVIDENCE,
        WorkflowState.DISCOVERING_IDENTITY_AND_STRUCTURE_SOURCES,
        WorkflowState.DISCOVERING_SUPPORTING_METADATA,
        WorkflowState.VALIDATING_DISCOVERED_SOURCES,
        WorkflowState.GAP_DIRECTED_DISCOVERY,
    ]
    evaluation_stages = [
        WorkflowState.EVALUATING_JOINABILITY,
        WorkflowState.IDENTIFYING_ASSEMBLY_GAPS,
        WorkflowState.COMPUTING_COMBINATION_COVERAGE,
    ]

    def register(
        name,
        description,
        input_model,
        output_model,
        implementation,
        stages,
        effect=SideEffectClassification.NONE,
        contextual=False,
        argument_normalizer=None,
        timeout_seconds=30.0,
        access_phase=ToolAccessPhase.PRE_APPROVAL_METADATA,
    ):
        registry.register(
            RegisteredTool(
                ToolDefinition(
                    name=name,
                    description=description,
                    input_schema_name=input_model.__name__,
                    output_schema_name=output_model.__name__,
                    required_permissions=[f"training-dataset:{name}"],
                    side_effect=effect,
                    idempotency=IdempotencyClassification.IDEMPOTENT_WITH_KEY,
                    timeout_seconds=timeout_seconds,
                    allowed_workflow_stages=stages,
                    implementation_version="training-dataset-v1",
                    access_phase=access_phase,
                ),
                input_model,
                output_model,
                implementation,
                contextual=contextual,
                argument_normalizer=argument_normalizer,
            )
        )

    source_operations = [
        "search_activity_sources",
        "validate_activity_source",
        "fetch_activity_source_metadata",
        "inspect_activity_result_availability",
        "inspect_activity_identifier_fields",
        "extract_activity_result_rows",
        "summarize_activity_outcomes",
        "inspect_counter_screen_relationships",
        "inspect_epa_public_invitrodb_release",
        "inspect_epa_public_database_package",
        "inspect_epa_public_assay_annotations",
        "inspect_epa_public_assay_target_mapping",
        "inspect_epa_public_summary_files",
        "inspect_epa_public_chemical_archive",
        "search_epa_assays",
        "inspect_epa_assay_metadata",
        "inspect_epa_activity_availability",
        "inspect_epa_compound_identifier_fields",
        "inspect_epa_release_manifest",
        "inspect_epa_related_assay_components",
        "search_transcriptomic_sources",
        "validate_transcriptomic_source",
        "fetch_transcriptomic_source_metadata",
        "inspect_perturbation_design",
        "inspect_transcriptomic_identity_fields",
        "inspect_signature_conditions",
        "inspect_processed_matrix_availability",
        "inspect_raw_matrix_availability",
        "inspect_feature_schema",
        "search_lincs_resources",
        "inspect_lincs_perturbagen_catalogue",
        "inspect_lincs_signature_metadata",
        "inspect_lincs_feature_space",
        "inspect_lincs_processed_signature_availability",
        "inspect_lincs_release_manifest",
        "inspect_source_identity_fields",
        "inspect_source_record_availability",
        "resolve_compound_identity_sample",
        "resolve_compound_synonyms",
        "inspect_supporting_metadata",
        "inspect_official_file_listing",
        "inspect_linked_publications",
        "inspect_source_access",
    ]
    candidate_search_operations = {
        "search_activity_sources",
        "search_epa_assays",
        "search_transcriptomic_sources",
        "search_lincs_resources",
    }
    coverage_operations = {
        "extract_activity_result_rows",
        "summarize_activity_outcomes",
        "inspect_lincs_perturbagen_catalogue",
        "inspect_lincs_signature_metadata",
        "inspect_lincs_feature_space",
    }
    for operation in source_operations:
        input_model = ReviewedSourceOperationInput
        argument_normalizer = None
        if operation == "search_activity_sources":
            input_model = ActivitySearchOperationInput
            argument_normalizer = normalize_activity_search_arguments
        elif operation == "extract_activity_result_rows":
            input_model = ActivityResultExtractionInput
        elif operation in {
            "inspect_source_identity_fields",
            "inspect_source_record_availability",
            "resolve_compound_identity_sample",
            "resolve_compound_synonyms",
        }:
            input_model = CompoundSourceOperationInput
        elif operation in {
            "inspect_supporting_metadata",
            "inspect_official_file_listing",
            "inspect_linked_publications",
            "inspect_source_access",
        }:
            input_model = SupportingSourceOperationInput
        register(
            operation,
            "Execute one typed operation through an approved reviewed official-source adapter.",
            input_model,
            VerifiedSourceObservationBatch,
            lambda request, invocation, selected=operation: adapters.execute(
                selected, request, invocation
            ),
            [
                *legacy_discovery_stages,
                *(
                    [WorkflowState.DISCOVERING_SOURCE_CANDIDATES]
                    if operation in candidate_search_operations
                    else [WorkflowState.HYDRATING_SOURCE_CANDIDATES]
                ),
                *(
                    [WorkflowState.COMPUTING_COMBINATION_COVERAGE]
                    if operation in coverage_operations
                    else []
                ),
            ],
            SideEffectClassification.EXTERNAL_READ,
            contextual=True,
            argument_normalizer=argument_normalizer,
            timeout_seconds=180.0,
        )
    register(
        "compare_verified_identity_fields",
        "Compare already verified source identity fields without resolving compounds.",
        IdentityFieldInspectionInput,
        IdentityFieldInspectionOutput,
        inspect_source_identity_fields,
        [*legacy_discovery_stages, WorkflowState.HYDRATING_SOURCE_CANDIDATES, *evaluation_stages],
    )
    register(
        "classify_verified_record_availability",
        "Classify already verified record access without downloading source tables.",
        RecordAvailabilityInput,
        RecordAvailabilityOutput,
        inspect_source_record_availability,
        [*legacy_discovery_stages, WorkflowState.HYDRATING_SOURCE_CANDIDATES, *evaluation_stages],
    )
    for operation in ("build_compound_mapping_manifest",):
        register(
            operation,
            "Build a bounded deterministic identifier mapping manifest from supplied records.",
            MappingManifestInput,
            MappingManifestOutput,
            build_compound_mapping_manifest,
            [
                *legacy_discovery_stages,
                WorkflowState.HYDRATING_SOURCE_CANDIDATES,
                *evaluation_stages,
            ],
        )
    register(
        "audit_coverage",
        "Compute exact bounded coverage only when full approved identifier tables are supplied.",
        CoverageAuditInput,
        CoverageAuditOutput,
        audit_coverage,
        evaluation_stages,
    )
    from .lincs_metadata import (
        ApprovedLincsSliceInput,
        ApprovedLincsSliceOutput,
        LincsCoverageInput,
        LincsCoverageOutput,
        LincsIndexInput,
        LincsIndexOutput,
        LincsMetadataRetrievalInput,
        LincsMetadataRetrievalOutput,
        build_lincs_indexes_tool,
        compute_lincs_coverage_tool,
        unavailable_lincs_level5_slice,
    )
    from .lincs_streaming import (
        LincsDiskCoverageInput,
        LincsDiskCoverageOutput,
        LincsDiskIndexInput,
        LincsDiskIndexOutput,
        LincsStreamingRetrievalOutput,
        StreamingLincsMetadataProvider,
    )

    streaming_lincs = isinstance(lincs_metadata_provider, StreamingLincsMetadataProvider)
    retrieval_output_model = (
        LincsStreamingRetrievalOutput if streaming_lincs else LincsMetadataRetrievalOutput
    )
    index_input_model = LincsDiskIndexInput if streaming_lincs else LincsIndexInput
    index_output_model = LincsDiskIndexOutput if streaming_lincs else LincsIndexOutput
    coverage_input_model = LincsDiskCoverageInput if streaming_lincs else LincsCoverageInput
    coverage_output_model = LincsDiskCoverageOutput if streaming_lincs else LincsCoverageOutput
    index_handler = lincs_metadata_provider.indexes if streaming_lincs else build_lincs_indexes_tool
    coverage_handler = (
        lincs_metadata_provider.coverage if streaming_lincs else compute_lincs_coverage_tool
    )

    if lincs_metadata_provider is not None:
        register(
            "retrieve_lincs_metadata_release",
            (
                "Retrieve every mandatory reviewed LINCS metadata file with immutable "
                "artifacts, manifest completion proof, cache, and bounded retry."
            ),
            LincsMetadataRetrievalInput,
            retrieval_output_model,
            lincs_metadata_provider.retrieve,
            [
                WorkflowState.DISCOVERING_SOURCE_CANDIDATES,
                WorkflowState.HYDRATING_SOURCE_CANDIDATES,
            ],
            SideEffectClassification.EXTERNAL_READ,
            contextual=True,
            timeout_seconds=180.0,
        )
    register(
        "build_lincs_metadata_indexes",
        "Build complete deterministic exact-match indexes over reviewed LINCS metadata.",
        index_input_model,
        index_output_model,
        index_handler,
        [
            WorkflowState.HYDRATING_SOURCE_CANDIDATES,
            WorkflowState.COMPUTING_COMBINATION_COVERAGE,
        ],
        contextual=streaming_lincs,
    )
    register(
        "compute_lincs_metadata_coverage",
        (
            "Compute exact compound/signature and context coverage from metadata only, "
            "without expression retrieval."
        ),
        coverage_input_model,
        coverage_output_model,
        coverage_handler,
        [WorkflowState.COMPUTING_COMBINATION_COVERAGE],
        contextual=streaming_lincs,
    )
    register(
        "slice_approved_lincs_level5_expression",
        (
            "Future batched partial Level-5 GCTX extraction for an immutable approved "
            "assembly recipe; unavailable during pre-approval metadata stage."
        ),
        ApprovedLincsSliceInput,
        ApprovedLincsSliceOutput,
        unavailable_lincs_level5_slice,
        [WorkflowState.ASSEMBLING_APPROVED_DATASET],
        SideEffectClassification.ARTIFACT_WRITE,
        timeout_seconds=180.0,
        access_phase=ToolAccessPhase.POST_APPROVAL_EXTRACTION,
    )
    if preapproval_provider_layer is not None:
        from .preapproval_providers import (
            PROVIDER_TOOL_NAMES,
            ProviderMetadataExecutionInput,
            ProviderMetadataExecutionOutput,
        )

        for provider_id, tool_name in PROVIDER_TOOL_NAMES.items():
            register(
                tool_name,
                (
                    f"Execute complete metadata-only {provider_id} discovery through its "
                    "reviewed versioned manifest; persist complete rows in SQLite artifacts."
                ),
                ProviderMetadataExecutionInput,
                ProviderMetadataExecutionOutput,
                lambda request, invocation, selected=provider_id: (
                    preapproval_provider_layer.execute(selected, request, invocation)
                ),
                [
                    WorkflowState.DISCOVERING_SOURCE_CANDIDATES,
                    WorkflowState.HYDRATING_SOURCE_CANDIDATES,
                ],
                SideEffectClassification.EXTERNAL_READ,
                contextual=True,
                timeout_seconds=180.0,
            )
    return registry
