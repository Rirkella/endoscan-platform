"""Provider-neutral contract and deterministic offline provider."""

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
    StructuredOutputDiagnostic,
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
        structured_output_diagnostic: StructuredOutputDiagnostic | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        safe_exception_class = _safe_provider_detail(exception_class)
        safe_sdk_diagnostic = safe_exception_class in {"UserError", "ModelBehaviorError"}
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
                    sanitize_local_sdk_message(developer_message) if safe_sdk_diagnostic else None
                ),
                "sdk_version": (
                    _safe_provider_detail(sdk_version) if safe_sdk_diagnostic else None
                ),
                "adapter_operation": (
                    _safe_provider_detail(adapter_operation) if safe_sdk_diagnostic else None
                ),
                "adapter_model": (
                    _safe_provider_detail(adapter_model) if safe_sdk_diagnostic else None
                ),
                "adapter_max_turns": (
                    adapter_max_turns
                    if safe_sdk_diagnostic and isinstance(adapter_max_turns, int)
                    else None
                ),
                "adapter_tool_count": (
                    adapter_tool_count
                    if safe_sdk_diagnostic and isinstance(adapter_tool_count, int)
                    else None
                ),
                "adapter_output_schema": (
                    _safe_provider_detail(adapter_output_schema) if safe_sdk_diagnostic else None
                ),
                "adapter_use_responses": (adapter_use_responses if safe_sdk_diagnostic else None),
                "adapter_parallel_tool_calls": (
                    adapter_parallel_tool_calls if safe_sdk_diagnostic else None
                ),
                "adapter_store": (adapter_store if safe_sdk_diagnostic else None),
                "adapter_tracing_disabled": (
                    adapter_tracing_disabled if safe_sdk_diagnostic else None
                ),
                "structured_output_diagnostic": (
                    structured_output_diagnostic.model_dump(mode="json")
                    if safe_exception_class == "ModelBehaviorError"
                    and structured_output_diagnostic is not None
                    else None
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


class DeterministicOfflineProvider:
    """Stateless fixture-backed provider used for tests and offline demonstrations."""

    name = "offline_fixture"

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
        mode = str(request.context.get("offline_fixture_mode", "discovery"))
        if mode == "timeout":
            raise ProviderTimeout("Prepared offline-provider timeout.")
        if mode == "failure":
            raise ProviderFailure("Prepared offline-provider failure.", retryable=False)
        if mode == "transient_failure" and self._transient_failures == 0:
            self._transient_failures += 1
            raise ProviderFailure("Prepared transient provider failure.", retryable=True)
        if self.script is not None:
            index = len(history)
            if index >= len(self.script):
                raise ProviderFailure("Offline-provider script was exhausted.", retryable=False)
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
        if request.output_schema_name == "DatasetSpecificationAgentOutcome":
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
                "schema_version": "1.0.0",
                "status": "completed",
                "specification": {
                    "schema_version": "1.0.0",
                    "endpoint_name": endpoint_name,
                    "biological_target": "Unresolved target requiring human review",
                    "endpoint_modality": "Unresolved endpoint modality",
                    "endpoint_definition": biological_goal
                    or "Prepared source-neutral endpoint definition requiring human review.",
                    "intended_prediction_task": (
                        "Predict endpoint-relative compound activity from compound-induced "
                        "transcriptomic responses in an approved biological context."
                    ),
                    "candidate_prediction_grain": "compound_cell_context_dose_time",
                    "explicit_prediction_grain": None,
                    "acceptable_activity_evidence_types": [
                        "continuous_activity",
                        "binary_active_inactive",
                    ],
                    "acceptable_transcriptomic_evidence_types": [
                        "processed differential signature",
                        "raw expression with matched controls",
                    ],
                    "compound_identity_requirements": [
                        "canonical compound identifier",
                        "InChIKey",
                    ],
                    "chemical_structure_requirements": ["canonical SMILES", "InChIKey"],
                    "experimental_context_requirements": [
                        "cell or tissue context",
                        "dose",
                        "exposure duration",
                        "control or reference definition",
                    ],
                    "mandatory_target_table_fields": [
                        "canonical_compound_id",
                        "canonical_smiles",
                        "inchikey",
                        "transcriptomic_signature",
                        "endpoint_activity_value",
                        "provenance",
                    ],
                    "minimum_evidence_requirements": [
                        "official primary public records",
                        "human-reviewed endpoint modality",
                    ],
                    "intended_scope_of_claim": (
                        "Prepared research-use scope restricted to the approved endpoint and "
                        "experimental contexts."
                    ),
                    "explicit_exclusions": ["Modalities outside the approved endpoint"],
                    "explicit_ambiguities": ["Permitted biological contexts are unresolved."],
                    "assumptions": ["The proposed prediction grain requires human review."],
                    "human_decisions_required": [
                        "Approve the biological target and endpoint modality."
                    ],
                },
                "requires_human_review": True,
                "decision_summary": "Prepared source-neutral specification draft for review.",
                "blocking_questions": [],
                "approval_questions": ["Which biological contexts are permitted?"],
                "missing_core_elements": [],
                "unresolved_questions": [],
                "limitations": ["Prepared deterministic fixture, not a live discovery."],
                "failure_category": None,
                "safe_failure_summary": None,
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
        fixture_path = Path(__file__).with_name("fixtures") / "oxidative_stress_offline_replay.json"
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
            usage_status="usage_recorded",
            input_tokens=120 + index * 10,
            output_tokens=40 + index * 5,
            cached_tokens=0,
            cost_cents=0.0,
            provider_request_ids=[f"offline-fixture-request-{index + 1}"],
            provider_invocations=1,
        )


class SlowDeterministicOfflineProvider(DeterministicOfflineProvider):
    def __init__(self, delay_seconds: float):
        super().__init__()
        self.delay_seconds = delay_seconds

    def run_turn(self, *args, **kwargs) -> ProviderTurn:
        time.sleep(self.delay_seconds)
        return super().run_turn(*args, **kwargs)
