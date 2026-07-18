"""Typed, stage-bound tool registry with no shell, arbitrary paths, or network access."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
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
    ToolResult,
    WorkflowState,
)
from .errors import AgentPolicyError


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


class RegisteredTool:
    def __init__(
        self,
        definition: ToolDefinition,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        implementation: Callable[..., BaseModel | dict],
        *,
        contextual: bool = False,
    ):
        self.definition = definition
        self.input_model = input_model
        self.output_model = output_model
        self.implementation = implementation
        self.contextual = contextual


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
        try:
            tool = self.get(invocation.tool_name)
            if invocation.workflow_stage not in tool.definition.allowed_workflow_stages:
                raise AgentPolicyError("Tool is prohibited in the current workflow stage.")
            if not set(tool.definition.required_permissions).issubset(invocation.permission_scope):
                raise AgentPolicyError("Tool permission scope is insufficient.")
            typed_input = tool.input_model.model_validate(invocation.arguments)
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
                )
            except Exception:
                return ToolResult(
                    tool_name=invocation.tool_name,
                    status=ToolCallStatus.FAILED,
                    error=NormalizedAgentError(
                        code="tool_failure",
                        safe_message="Tool failed without exposing internal details.",
                        retryable=False,
                        category="tool",
                    ),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
        return ToolResult(
            tool_name=invocation.tool_name,
            status=ToolCallStatus.COMPLETED,
            output=output,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def phase0_tool_registry(repo_root: Path) -> ToolRegistry:
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
            fixture_label="Prepared deterministic Phase-0 fixture — not live discovery",
            candidates=[
                {
                    "candidate_id": "phase0-candidate-a",
                    "title": f"Prepared metadata candidate A for {request.endpoint_name}",
                    "source": "phase0://prepared-fixture/a",
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
                    "candidate_id": "phase0-candidate-b",
                    "title": f"Prepared metadata candidate B for {request.endpoint_name}",
                    "source": "phase0://prepared-fixture/b",
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
            "Return clearly labelled prepared Phase-0 candidate fixtures.",
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
                    implementation_version="phase0-v1",
                ),
                input_model,
                output_model,
                implementation,
            )
        )
    return registry


def phase1_tool_registry(repo_root: Path, discovery_service) -> ToolRegistry:
    """Phase-0 tools plus the six bounded Phase-1 scientific metadata tools."""
    from .discovery_tools import (
        CompareDatasetCandidatesInput,
        CompareDatasetCandidatesOutput,
        GeoAccessionInput,
        GeoSampleDesignOutput,
        GeoSeriesMetadataOutput,
        GeoValidationOutput,
        PublicationMetadataInput,
        PublicationMetadataOutput,
        SearchGeoSeriesInput,
        SearchGeoSeriesOutput,
    )

    registry = phase0_tool_registry(repo_root)
    discovery = [WorkflowState.DISCOVERING_DATA]
    specs = [
        (
            "search_geo_series",
            "Search official NCBI GEO Series metadata within bounded query and result limits.",
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
            "validate_geo_accession",
            "Confirm that one GSE accession resolves through the official GEO source.",
            GeoAccessionInput,
            GeoValidationOutput,
            discovery_service.validate_geo_accession,
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
                    implementation_version="phase1-v1",
                ),
                input_model,
                output_model,
                implementation,
                contextual=True,
            )
        )
    return registry
