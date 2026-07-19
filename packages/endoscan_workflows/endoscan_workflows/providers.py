"""Provider-neutral contract and deterministic Phase-0 fake provider."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .contracts import (
    AgentRunRequest,
    NormalizedAgentError,
    ProviderToolRequest,
    ProviderTurn,
    UsageReport,
)

_SAFE_PROVIDER_DETAIL = re.compile(r"^[A-Za-z0-9_.$:/\[\]-]{1,200}$")
_SECRET_LIKE_PROVIDER_DETAIL = re.compile(r"(?i)sk-[A-Za-z0-9_-]{6,}")
_DEVELOPER_MESSAGE_LIMIT = 800
_AUTHORIZATION_VALUE = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+")
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_NAMED_SECRET_VALUE = re.compile(
    r"(?i)\b(api[_ -]?key|token|password|secret)\b(\s*[:=]\s*)" r"(?:['\"]?)[^\s,'\";]+"
)
_CREDENTIAL_URL = re.compile(r"(?i)\b(https?://)([^\s/@:]+):([^\s/@]+)@")


def _safe_provider_detail(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    if _SECRET_LIKE_PROVIDER_DETAIL.search(text):
        return "[REDACTED]"
    return text if _SAFE_PROVIDER_DETAIL.fullmatch(text) else None


def sanitize_local_sdk_message(value: object | None) -> str | None:
    """Return a bounded developer diagnostic for a known local SDK configuration error."""
    if value is None:
        return None
    text = " ".join(str(value).replace("\x00", " ").split())
    text = _SECRET_LIKE_PROVIDER_DETAIL.sub("[REDACTED]", text)
    text = _AUTHORIZATION_VALUE.sub(r"\1[REDACTED]", text)
    text = _BEARER_VALUE.sub("Bearer [REDACTED]", text)
    text = _NAMED_SECRET_VALUE.sub(r"\1\2[REDACTED]", text)
    text = _CREDENTIAL_URL.sub(r"\1[REDACTED]@", text)
    if len(text) > _DEVELOPER_MESSAGE_LIMIT:
        text = f"{text[: _DEVELOPER_MESSAGE_LIMIT - 3]}..."
    return text or None


class ProviderFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        exception_class: str | None = None,
        http_status: int | None = None,
        provider_error_code: object | None = None,
        provider_error_type: object | None = None,
        provider_request_id: object | None = None,
        provider_parameter: object | None = None,
        developer_message: object | None = None,
        sdk_version: object | None = None,
        adapter_operation: object | None = None,
        adapter_model: object | None = None,
        adapter_max_turns: int | None = None,
        adapter_tool_count: int | None = None,
        adapter_output_schema: object | None = None,
        adapter_use_responses: bool | None = None,
        adapter_parallel_tool_calls: bool | None = None,
        adapter_store: bool | None = None,
        adapter_tracing_disabled: bool | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        safe_exception_class = _safe_provider_detail(exception_class)
        local_sdk_configuration_error = safe_exception_class == "UserError"
        self.trace_detail = {
            key: value
            for key, value in {
                "exception_class": safe_exception_class,
                "http_status": (
                    http_status
                    if isinstance(http_status, int) and 100 <= http_status <= 599
                    else None
                ),
                "provider_error_code": _safe_provider_detail(provider_error_code),
                "provider_error_type": _safe_provider_detail(provider_error_type),
                "provider_request_id": _safe_provider_detail(provider_request_id),
                "provider_parameter": _safe_provider_detail(provider_parameter),
                "developer_message": (
                    sanitize_local_sdk_message(developer_message)
                    if local_sdk_configuration_error
                    else None
                ),
                "sdk_version": (
                    _safe_provider_detail(sdk_version) if local_sdk_configuration_error else None
                ),
                "adapter_operation": (
                    _safe_provider_detail(adapter_operation)
                    if local_sdk_configuration_error
                    else None
                ),
                "adapter_model": (
                    _safe_provider_detail(adapter_model) if local_sdk_configuration_error else None
                ),
                "adapter_max_turns": (
                    adapter_max_turns
                    if local_sdk_configuration_error and isinstance(adapter_max_turns, int)
                    else None
                ),
                "adapter_tool_count": (
                    adapter_tool_count
                    if local_sdk_configuration_error and isinstance(adapter_tool_count, int)
                    else None
                ),
                "adapter_output_schema": (
                    _safe_provider_detail(adapter_output_schema)
                    if local_sdk_configuration_error
                    else None
                ),
                "adapter_use_responses": (
                    adapter_use_responses if local_sdk_configuration_error else None
                ),
                "adapter_parallel_tool_calls": (
                    adapter_parallel_tool_calls if local_sdk_configuration_error else None
                ),
                "adapter_store": (adapter_store if local_sdk_configuration_error else None),
                "adapter_tracing_disabled": (
                    adapter_tracing_disabled if local_sdk_configuration_error else None
                ),
            }.items()
            if value is not None
        }


class ProviderTimeout(TimeoutError):
    def __init__(self, message: str, *, exception_class: str | None = None):
        super().__init__(message)
        self.retryable = True
        safe_class = _safe_provider_detail(exception_class)
        self.trace_detail = {"exception_class": safe_class} if safe_class is not None else {}


class AgentProvider(Protocol):
    name: str

    def run_turn(
        self,
        request: AgentRunRequest,
        history: list[dict],
        *,
        interruption_requested: bool,
    ) -> ProviderTurn: ...


ProviderFactory = Callable[[], AgentProvider]


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ProviderFactory] = {}

    def register(self, name: str, factory: ProviderFactory) -> None:
        if name in self._providers:
            raise ValueError(f"provider {name!r} is already registered")
        self._providers[name] = factory

    def create(self, name: str) -> AgentProvider:
        try:
            return self._providers[name]()
        except KeyError as exc:
            raise ProviderFailure(f"Provider {name!r} is not configured.", retryable=False) from exc

    def configured(self) -> list[str]:
        return sorted(self._providers)


class FakeAgentProvider:
    """Stateless deterministic provider used for tests and prepared Phase-0 demos."""

    name = "fake"

    def __init__(self, script: list[ProviderTurn] | None = None):
        self.script = script
        self._transient_failures = 0

    def run_turn(
        self,
        request: AgentRunRequest,
        history: list[dict],
        *,
        interruption_requested: bool,
    ) -> ProviderTurn:
        if interruption_requested:
            return ProviderTurn(
                kind="error",
                error=NormalizedAgentError(
                    code="interrupted",
                    safe_message="Agent run was interrupted by the orchestrator.",
                    retryable=False,
                    category="interruption",
                ),
            )
        mode = str(request.context.get("fake_mode", "discovery"))
        if mode == "timeout":
            raise ProviderTimeout("Prepared fake-provider timeout.")
        if mode == "failure":
            raise ProviderFailure("Prepared fake-provider failure.", retryable=False)
        if mode == "transient_failure" and self._transient_failures == 0:
            self._transient_failures += 1
            raise ProviderFailure("Prepared transient provider failure.", retryable=True)
        if self.script is not None:
            index = len(history)
            if index >= len(self.script):
                raise ProviderFailure("Fake-provider script was exhausted.", retryable=False)
            return self.script[index]
        if mode == "malformed":
            return ProviderTurn(
                kind="output",
                output={"unexpected": True},
                usage=self._usage(len(history)),
            )
        if mode == "approval":
            approval = request.context.get("approval")
            if not isinstance(approval, dict):
                raise ProviderFailure("Prepared approval payload is missing.", retryable=False)
            return ProviderTurn(kind="approval", approval=approval, usage=self._usage(len(history)))
        if request.output_schema_name == "TrainingDatasetSpecification":
            return self._training_dataset_specification_turn(request, history)
        return self._discovery_turn(request, history)

    def _training_dataset_specification_turn(
        self, request: AgentRunRequest, history: list[dict]
    ) -> ProviderTurn:
        endpoint_name = str(request.context.get("endpoint_name", "Prepared endpoint"))
        biological_goal = str(request.context.get("biological_goal", ""))
        return ProviderTurn(
            kind="output",
            output={
                "contract_version": "1.0.0",
                "specification_id": "prepared-source-neutral-specification",
                "endpoint_name": endpoint_name,
                "biological_target": "Unresolved target requiring human review",
                "endpoint_modality": "Unresolved endpoint modality",
                "endpoint_definition": (
                    biological_goal
                    or "Prepared source-neutral endpoint definition requiring human review."
                ),
                "intended_prediction_task": (
                    "Predict endpoint-relative compound activity from compound-induced "
                    "transcriptomic responses for an explicitly approved biological context."
                ),
                "prediction_unit": "compound_cell_context_dose_time",
                "explicit_prediction_grain": None,
                "acceptable_activity_representations": [
                    "continuous_activity",
                    "binary_active_inactive",
                ],
                "acceptable_transcriptomic_representations": [
                    "processed differential signature",
                    "raw expression with matched controls",
                ],
                "compound_identity_requirements": ["PubChem CID", "InChIKey"],
                "chemical_structure_requirements": [
                    "canonical SMILES",
                    "isomeric SMILES where available",
                ],
                "experimental_context_requirements": [
                    "cell or tissue context",
                    "dose",
                    "exposure duration",
                    "control or reference definition",
                ],
                "mandatory_output_fields": [
                    "canonical_compound_id",
                    "canonical_smiles",
                    "inchikey",
                    "transcriptomic_signature",
                    "feature_schema",
                    "cell_or_tissue_context",
                    "dose",
                    "exposure_time",
                    "endpoint_activity_value",
                    "endpoint_modality",
                    "assay_id",
                    "provenance",
                    "quality_flags",
                ],
                "optional_output_fields": [
                    "preferred_name",
                    "isomeric_smiles",
                    "endpoint_activity_label",
                ],
                "allowed_missingness": {"isomeric_smiles": 1.0},
                "minimum_evidence_requirements": [
                    "official primary public records",
                    "human-reviewed endpoint modality",
                ],
                "minimum_coverage_requirements": {},
                "minimum_class_size_requirements": {},
                "permitted_biological_contexts": [],
                "excluded_modalities": [],
                "intended_scope_of_claim": (
                    "Prepared research-use scope restricted to the approved endpoint, evidence "
                    "modalities, and experimental contexts."
                ),
                "assumptions_requiring_human_approval": [
                    "Resolve the biological target and endpoint modality.",
                    "Approve the observation grain and acceptable context aggregation.",
                ],
                "unresolved_questions": [
                    "Which biological contexts are permitted?",
                    "Which activity representation is primary?",
                ],
                "requires_human_review": True,
            },
            usage=self._usage(len(history)),
        )

    def _discovery_turn(self, request: AgentRunRequest, history: list[dict]) -> ProviderTurn:
        sequence = [
            ProviderToolRequest(
                tool_name="inspect_endpoint_registry",
                arguments={"repository_root": "."},
                idempotency_key="discovery-registry-v1",
            ),
            ProviderToolRequest(
                tool_name="list_known_source_adapters",
                arguments={"repository_root": "."},
                idempotency_key="discovery-adapters-v1",
            ),
            ProviderToolRequest(
                tool_name="summarize_existing_endpoint_pipeline",
                arguments={"repository_root": ".", "endpoint_ids": ["ER", "AR"]},
                idempotency_key="discovery-pipelines-v1",
            ),
            ProviderToolRequest(
                tool_name="create_dataset_candidate_artifact",
                arguments={
                    "endpoint_name": request.context.get("endpoint_name", "Oxidative stress")
                },
                idempotency_key="discovery-prepared-candidates-v1",
            ),
        ]
        if len(history) < len(sequence):
            return ProviderTurn(
                kind="tool",
                tool_request=sequence[len(history)],
                usage=self._usage(len(history)),
            )
        endpoint_name = str(request.context.get("endpoint_name", "Oxidative stress"))
        fixture_path = Path(__file__).with_name("fixtures") / "phase1_oxidative_stress_replay.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        output = fixture["output"]
        output["endpoint_name"] = endpoint_name
        output["endpoint_definition_summary"] = str(request.context.get("biological_goal", ""))
        for index, candidate in enumerate(output["candidates"], start=1):
            candidate["title"] = (
                f"Prepared metadata candidate {chr(64 + index)} for {endpoint_name}"
            )
        return ProviderTurn(
            kind="output",
            output=output,
            usage=self._usage(len(history)),
        )

    @staticmethod
    def _usage(index: int) -> UsageReport:
        return UsageReport(
            input_tokens=120 + index * 10,
            output_tokens=40 + index * 5,
            cached_tokens=0,
            cost_cents=0.0,
            provider_request_ids=[f"fake-request-{index + 1}"],
        )


class SlowFakeAgentProvider(FakeAgentProvider):
    def __init__(self, delay_seconds: float):
        super().__init__()
        self.delay_seconds = delay_seconds

    def run_turn(self, *args, **kwargs) -> ProviderTurn:
        time.sleep(self.delay_seconds)
        return super().run_turn(*args, **kwargs)
