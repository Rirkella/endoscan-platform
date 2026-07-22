from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from agents import AgentOutputSchema
from agents.exceptions import ModelBehaviorError, ModelRefusalError, UserError
from openai import (
    APIConnectionError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import SecretStr

from endoscan_workflows.config import AgentConfiguration, AgentRunMode
from endoscan_workflows.contracts import AgentBudget, AgentRunRequest, ModelConfiguration
from endoscan_workflows.harness import AgentHarness
from endoscan_workflows.openai_provider import TOOL_ENVELOPE, OpenAIAgentProvider, sdk_output_schema
from endoscan_workflows.providers import ProviderFailure, ProviderTimeout
from endoscan_workflows.tools import phase0_tool_registry
from endoscan_workflows.training_dataset import TrainingDatasetSpecification

REPO_ROOT = Path(__file__).resolve().parents[3]


def request() -> AgentRunRequest:
    return AgentRunRequest(
        workflow_id="build-test",
        step_id="step-test",
        agent_name="Dataset Discovery and Evaluation Agent",
        agent_version="phase1-v1",
        instruction_version="phase1-v1",
        instructions="Use only the provided bounded tool and return strict output.",
        model=ModelConfiguration(provider="openai", model_identifier="gpt-5.4-mini"),
        output_schema_name="DiscoveryOutput",
        available_tools=["inspect_endpoint_registry"],
        context={"endpoint_name": "Oxidative stress", "run_mode": "live"},
        budget=AgentBudget(maximum_turns=4, maximum_tool_calls=4),
    )


def valid_output() -> dict:
    base = {
        "title": "Official GEO candidate",
        "source": "NCBI GEO",
        "organism": ["Homo sapiens"],
        "data_type": "transcriptomic series",
        "sample_count": 12,
        "biological_context": "Cultured human cells",
        "treatment_control_evidence": "Treatment and vehicle groups require human review.",
        "dose_time_evidence": "Dose and time are reported in metadata.",
        "strengths": ["Official accession"],
        "limitations": ["Labels require scientist review"],
        "exclusion_reasons": [],
        "recommendation_status": "recommended_for_human_review",
        "evidence_references": [],
        "accession_verified": True,
        "license_verified": False,
    }
    return {
        "endpoint_name": "Oxidative stress",
        "endpoint_definition_summary": "Response-defined endpoint",
        "run_mode": "live",
        "simulation_label": None,
        "live_discovery": True,
        "search_strategy": "Search and validate official GEO series.",
        "queries_executed": ["oxidative stress transcriptome"],
        "candidates": [
            {"candidate_id": "candidate-1", "accession": "GSE12345", **base},
            {
                "candidate_id": "candidate-2",
                "accession": "GSE12346",
                **{**base, "recommendation_status": "alternative"},
            },
        ],
        "recommended_candidate_id": "candidate-1",
        "recommendation": "Review candidate-1; this is not scientific approval.",
        "decision_summary": "One candidate is stronger but both require review.",
        "rejected_candidates": [],
        "unresolved_questions": ["Are treatment labels scientifically acceptable?"],
        "requires_human_review": True,
        "evidence_references": [],
        "limitations": ["No dataset was downloaded."],
        "confidence_category": "moderate",
    }


def result(output, *, input_tokens: int = 0, output_tokens: int = 0):
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=7),
    )
    return SimpleNamespace(
        final_output=output,
        context_wrapper=SimpleNamespace(usage=usage),
        raw_responses=[SimpleNamespace(response_id="resp-safe")],
    )


def provider(runner) -> OpenAIAgentProvider:
    configuration = AgentConfiguration(
        provider="openai",
        run_mode=AgentRunMode.LIVE,
        api_key=SecretStr("unit-test-provider-secret"),
    )
    return OpenAIAgentProvider(
        configuration,
        phase0_tool_registry(REPO_ROOT),
        runner=runner,
    )


def status_error(error_class, status: int, code: str, error_type: str):
    response = httpx.Response(
        status,
        headers={"x-request-id": f"req_status_{status}"},
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
    )
    return error_class(
        "raw provider message must not persist",
        response=response,
        body={
            "code": code,
            "param": "model",
            "type": error_type,
            "message": "raw provider body must not persist",
        },
    )


def test_valid_structured_output_and_normalized_usage() -> None:
    item = provider(
        lambda *_args, **_kwargs: result(valid_output(), input_tokens=1000, output_tokens=500)
    )
    turn = item.run_turn(request(), [], interruption_requested=False)
    assert turn.kind == "output"
    assert turn.output["recommended_candidate_id"] == "candidate-1"
    assert turn.usage.input_tokens == 1000
    assert turn.usage.output_tokens == 500
    assert turn.usage.cached_tokens == 7
    assert turn.usage.provider_request_ids == []
    assert turn.usage.provider_response_ids == ["resp-safe"]
    assert turn.usage.usage_status == "usage_recorded"
    assert turn.usage.provider_invocations == 1
    assert turn.usage.cost_cents == pytest.approx(0.3)


def test_tool_request_is_translated_without_executing_tool() -> None:
    envelope = {
        TOOL_ENVELOPE: True,
        "tool_name": "inspect_endpoint_registry",
        "arguments": {"endpoint_id": "ER"},
    }
    turn = provider(lambda *_args, **_kwargs: result(envelope)).run_turn(
        request(), [], interruption_requested=False
    )
    assert turn.kind == "tool"
    assert turn.tool_request.tool_name == "inspect_endpoint_registry"
    assert turn.tool_request.arguments == {"endpoint_id": "ER"}
    assert turn.tool_request.idempotency_key.startswith("openai-")


def test_follow_up_context_is_compact_but_preserves_evidence_bindings() -> None:
    history = [
        {
            "tool_name": "search_geo_series",
            "output": {
                "schema_version": "1.0.0",
                "retrieval_timestamp": "2026-07-18T00:00:00Z",
                "source_artifact_id": "artifact-safe-reference",
                "raw_soft_body": "RAW-SOFT-MUST-NOT-REACH-MODEL",
                "source_diagnostic": {"http_status": 200, "storage_path": "local/path"},
                "results": [
                    {"accession": f"GSE{10000 + index}", "summary": "x" * 2000}
                    for index in range(9)
                ],
            },
            "workflow_event_history": ["audit-event-must-not-reach-model"],
        }
    ]
    compact = OpenAIAgentProvider._compact_history(history)
    output = compact[0]["output"]
    assert "schema_version" not in output
    assert "retrieval_timestamp" not in output
    assert output["source_artifact_id"] == "artifact-safe-reference"
    assert len(output["results"]) == 5
    assert all("summary" not in item for item in output["results"])
    assert "x" * 2000 not in json.dumps(compact)
    assert "RAW-SOFT-MUST-NOT-REACH-MODEL" not in json.dumps(compact)
    assert "audit-event-must-not-reach-model" not in json.dumps(compact)
    assert "artifact-safe-reference" in json.dumps(compact)


def test_provider_timeout_is_normalized() -> None:
    def timeout(*_args, **_kwargs):
        raise TimeoutError("provider internals must not leak")

    with pytest.raises(ProviderTimeout, match="configured timeout") as raised:
        provider(timeout).run_turn(request(), [], interruption_requested=False)
    assert raised.value.retryable is True
    assert raised.value.trace_detail == {"exception_class": "TimeoutError"}


@pytest.mark.parametrize(
    ("error", "retryable", "status", "code"),
    [
        (
            status_error(AuthenticationError, 401, "invalid_api_key", "authentication_error"),
            False,
            401,
            "invalid_api_key",
        ),
        (
            status_error(PermissionDeniedError, 403, "model_access_denied", "permission_error"),
            False,
            403,
            "model_access_denied",
        ),
        (
            status_error(NotFoundError, 404, "model_not_found", "invalid_request_error"),
            False,
            404,
            "model_not_found",
        ),
        (
            status_error(BadRequestError, 400, "unsupported_parameter", "invalid_request_error"),
            False,
            400,
            "unsupported_parameter",
        ),
        (
            status_error(RateLimitError, 429, "rate_limit_exceeded", "rate_limit_error"),
            True,
            429,
            "rate_limit_exceeded",
        ),
        (
            status_error(InternalServerError, 500, "server_error", "server_error"),
            True,
            500,
            "server_error",
        ),
    ],
)
def test_api_status_retryability_is_authoritative(
    error: Exception, retryable: bool, status: int, code: str
) -> None:
    with pytest.raises(ProviderFailure) as raised:
        provider(lambda *_args, **_kwargs: (_ for _ in ()).throw(error)).run_turn(
            request(), [], interruption_requested=False
        )
    assert raised.value.retryable is retryable
    assert raised.value.trace_detail["http_status"] == status
    assert raised.value.trace_detail["provider_error_code"] == code
    assert raised.value.trace_detail["provider_request_id"] == f"req_status_{status}"


def test_connection_failure_is_retryable() -> None:
    error = APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    with pytest.raises(ProviderFailure) as raised:
        provider(lambda *_args, **_kwargs: (_ for _ in ()).throw(error)).run_turn(
            request(), [], interruption_requested=False
        )
    assert raised.value.retryable is True
    assert raised.value.trace_detail == {"exception_class": "APIConnectionError"}


def test_unknown_adapter_exception_is_non_retryable_and_safe() -> None:
    error = RuntimeError("arbitrary repr with sk-unit-test-secret")
    with pytest.raises(ProviderFailure) as raised:
        provider(lambda *_args, **_kwargs: (_ for _ in ()).throw(error)).run_turn(
            request(), [], interruption_requested=False
        )
    assert raised.value.retryable is False
    assert raised.value.trace_detail == {"exception_class": "RuntimeError"}
    assert "sk-unit-test-secret" not in str(raised.value.trace_detail)


def test_user_error_preserves_only_bounded_redacted_developer_diagnostics() -> None:
    sdk_message = (
        "additionalProperties should not be set for object types. "
        "Authorization: Bearer local-token api_key=sk-unit-test-secret "
        "https://user:password@example.test/path"
    )
    with pytest.raises(ProviderFailure) as raised:
        provider(lambda *_args, **_kwargs: (_ for _ in ()).throw(UserError(sdk_message))).run_turn(
            request(), [], interruption_requested=False
        )
    detail = raised.value.trace_detail
    assert raised.value.retryable is False
    assert detail["exception_class"] == "UserError"
    assert detail["sdk_version"] == "0.18.2"
    assert detail["adapter_operation"] == "run_turn"
    assert detail["adapter_model"] == "gpt-5.4-mini"
    assert detail["adapter_max_turns"] == 1
    assert detail["adapter_tool_count"] == 1
    assert detail["adapter_output_schema"] == "DiscoveryOutput"
    assert detail["adapter_use_responses"] is True
    assert detail["adapter_parallel_tool_calls"] is False
    assert detail["adapter_store"] is False
    assert detail["adapter_tracing_disabled"] is True
    assert "additionalProperties should not be set" in detail["developer_message"]
    assert "local-token" not in detail["developer_message"]
    assert "sk-unit-test-secret" not in detail["developer_message"]
    assert "user:password" not in detail["developer_message"]
    assert len(detail["developer_message"]) <= 800


def test_developer_message_is_rejected_for_unknown_exception_classes() -> None:
    failure = ProviderFailure(
        "Safe normalized message.",
        exception_class="RuntimeError",
        developer_message="must not persist",
        sdk_version="0.18.2",
    )
    assert failure.trace_detail == {"exception_class": "RuntimeError"}


def test_api_status_failure_preserves_only_safe_diagnostics() -> None:
    response = httpx.Response(
        400,
        headers={"x-request-id": "req_safe-diagnostic"},
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
    )
    api_error = BadRequestError(
        "synthetic message must not be persisted",
        response=response,
        body={
            "code": "unsupported_value",
            "param": "temperature",
            "type": "invalid_request_error",
            "message": "synthetic body must not be persisted",
        },
    )

    with pytest.raises(ProviderFailure) as raised:
        provider(lambda *_args, **_kwargs: (_ for _ in ()).throw(api_error)).run_turn(
            request(), [], interruption_requested=False
        )

    assert raised.value.retryable is False
    assert raised.value.trace_detail == {
        "exception_class": "BadRequestError",
        "http_status": 400,
        "provider_error_code": "unsupported_value",
        "provider_error_type": "invalid_request_error",
        "provider_request_id": "req_safe-diagnostic",
        "provider_parameter": "temperature",
    }
    assert "synthetic" not in str(raised.value.trace_detail)


def test_safe_error_names_survive_while_secret_like_values_are_redacted() -> None:
    failure = ProviderFailure(
        "Safe normalized message.",
        retryable=False,
        provider_error_code="invalid_api_key",
        provider_request_id="req_prefix_sk-unit-secret_suffix",
        provider_parameter="api_key",
    )
    assert failure.trace_detail == {
        "provider_error_code": "invalid_api_key",
        "provider_request_id": "[REDACTED]",
        "provider_parameter": "api_key",
    }


def test_malformed_output_fails_safely() -> None:
    with pytest.raises(ProviderFailure, match="malformed structured output"):
        provider(lambda *_args, **_kwargs: result("not-json")).run_turn(
            request(), [], interruption_requested=False
        )


def test_cancellation_returns_interruption_without_calling_provider() -> None:
    called = False

    def runner(*_args, **_kwargs):
        nonlocal called
        called = True

    turn = provider(runner).run_turn(request(), [], interruption_requested=True)
    assert turn.kind == "error"
    assert turn.error.code == "interrupted"
    assert called is False


def test_missing_api_key_disables_provider_cleanly() -> None:
    configuration = AgentConfiguration(provider="openai", run_mode=AgentRunMode.REPLAY)
    item = OpenAIAgentProvider(configuration, phase0_tool_registry(REPO_ROOT))
    with pytest.raises(ProviderFailure, match="not configured"):
        item.run_turn(request(), [], interruption_requested=False)


def test_secret_is_never_exposed_in_status_or_error() -> None:
    secret = "unit-test-provider-secret"
    item = provider(lambda *_args, **_kwargs: result(object()))
    with pytest.raises(ProviderFailure) as raised:
        item.run_turn(request(), [], interruption_requested=False)
    assert secret not in str(raised.value)
    assert secret not in str(item.configuration.public_status())


def specification_request() -> AgentRunRequest:
    return request().model_copy(
        update={
            "agent_name": "Dataset Specification Agent",
            "output_schema_name": "DatasetSpecificationAgentOutcome",
            "available_tools": [],
        }
    )


def test_exact_legacy_specification_schema_remains_readable() -> None:
    with pytest.raises(UserError, match="Strict JSON schema is enabled"):
        AgentOutputSchema(TrainingDatasetSpecification)
    schema = sdk_output_schema("DatasetSpecificationAgentOutcome").json_schema()
    encoded = json.dumps(schema, separators=(",", ":"), sort_keys=True)
    assert len(encoded) < 8_000
    assert schema["additionalProperties"] is False
    assert '"additionalProperties":true' not in encoded


def test_compact_specification_review_schema_is_strict_and_bounded() -> None:
    schema = sdk_output_schema("DatasetSpecificationReviewOutcome")
    encoded = json.dumps(schema.json_schema(), separators=(",", ":"), sort_keys=True)
    assert len(encoded) < 5_000
    assert schema.is_strict_json_schema() is True
    assert schema.json_schema()["additionalProperties"] is False
    assert schema.json_schema()["required"] == list(schema.json_schema()["properties"])
    assert '"additionalProperties":true' not in encoded
    assert '"default"' not in encoded


def test_structured_output_request_fingerprint_records_safe_runtime_contract() -> None:
    item = provider(lambda *_args, **_kwargs: result(valid_output()))
    base = request()
    expected = {
        "agent_name": base.agent_name,
        "output_schema_name": base.output_schema_name,
        "api_surface": "responses",
        "configured_model": base.model.model_identifier,
        "tool_count": len(base.available_tools),
        "tool_names": sorted(base.available_tools),
        "tool_choice_mode": "auto",
    }
    fingerprint = item.structured_output_fingerprint(
        base.model_copy(
            update={"context": {**base.context, "structured_output_boundary_contract": expected}}
        )
    )
    assert fingerprint.output_type_present is True
    assert fingerprint.strict_json_schema is True
    assert fingerprint.api_surface == "responses"
    assert fingerprint.boundary_runtime_contracts_match is True
    assert fingerprint.runtime_configuration_hash == (fingerprint.boundary_probe_configuration_hash)
    assert len(fingerprint.schema_hash) == 64
    assert fingerprint.schema_byte_size > 0
    persisted = fingerprint.model_dump_json().casefold()
    assert base.instructions.casefold() not in persisted
    assert "unit-test-provider-secret" not in persisted
    assert "raw_response" not in persisted


def test_structured_output_fingerprint_detects_wrong_tool_with_same_count() -> None:
    item = provider(lambda *_args, **_kwargs: result(valid_output()))
    base = request()
    expected = {
        "agent_name": base.agent_name,
        "output_schema_name": base.output_schema_name,
        "api_surface": "responses",
        "configured_model": base.model.model_identifier,
        "tool_count": len(base.available_tools),
        "tool_names": ["different_allowlisted_tool"],
        "tool_choice_mode": "auto",
    }
    fingerprint = item.structured_output_fingerprint(
        base.model_copy(
            update={"context": {**base.context, "structured_output_boundary_contract": expected}}
        )
    )
    assert fingerprint.boundary_runtime_contracts_match is False
    assert fingerprint.runtime_configuration_hash != fingerprint.boundary_probe_configuration_hash


@pytest.mark.parametrize(
    ("substage", "exposed_tools", "tool_choice_mode"),
    [
        ("candidate_search", ["search_activity_sources", "inspect_activity_source"], "auto"),
        ("final_output", [], "none"),
    ],
)
def test_stage_scoped_boundary_fingerprint_matches_exact_exposed_tools(
    substage: str, exposed_tools: list[str], tool_choice_mode: str
) -> None:
    item = provider(lambda *_args, **_kwargs: result(valid_output()))
    base = request().model_copy(
        update={
            "available_tools": [
                "search_activity_sources",
                "inspect_activity_source",
                "validate_activity_source",
            ],
        }
    )
    expected = {
        "agent_name": base.agent_name,
        "output_schema_name": base.output_schema_name,
        "api_surface": "responses",
        "configured_model": base.model.model_identifier,
        "tool_count": len(base.available_tools),
        "tool_names": sorted(base.available_tools),
        "tool_choice_mode": "auto",
    }
    base = base.model_copy(
        update={
            "context": {
                **base.context,
                "stage_tool_sets": {
                    "candidate_search": [
                        "search_activity_sources",
                        "inspect_activity_source",
                    ],
                    "final_output": [],
                },
                "structured_output_boundary_contract": expected,
            }
        }
    )
    scoped_context = AgentHarness._stage_scoped_turn_context(
        base,
        discovery_substage=substage,
        exposed_tools=exposed_tools,
        tool_calls=0,
    )
    scoped_request = base.model_copy(
        update={"available_tools": exposed_tools, "context": scoped_context}
    )
    fingerprint = item.structured_output_fingerprint(scoped_request)
    assert fingerprint.tool_count == len(exposed_tools)
    assert fingerprint.tool_choice_mode == tool_choice_mode
    assert fingerprint.boundary_runtime_contracts_match is True
    assert fingerprint.runtime_configuration_hash == fingerprint.boundary_probe_configuration_hash


def test_structured_output_request_fingerprint_detects_wrong_runtime_schema() -> None:
    item = provider(lambda *_args, **_kwargs: result(valid_output()))
    base = request()
    expected = {
        "agent_name": base.agent_name,
        "output_schema_name": "DatasetSpecificationReviewOutcome",
        "api_surface": "responses",
        "configured_model": base.model.model_identifier,
        "tool_count": len(base.available_tools),
        "tool_names": sorted(base.available_tools),
        "tool_choice_mode": "auto",
    }
    fingerprint = item.structured_output_fingerprint(
        base.model_copy(
            update={"context": {**base.context, "structured_output_boundary_contract": expected}}
        )
    )
    assert fingerprint.output_type_present is True
    assert fingerprint.boundary_runtime_contracts_match is False
    assert fingerprint.runtime_configuration_hash != (fingerprint.boundary_probe_configuration_hash)


def test_offline_specification_boundary_fixtures_cover_invalid_shapes() -> None:
    fixtures = json.loads(
        (
            REPO_ROOT
            / "packages"
            / "endoscan_workflows"
            / "endoscan_workflows"
            / "fixtures"
            / "dataset_specification_outcomes.json"
        ).read_text(encoding="utf-8")
    )
    schema = sdk_output_schema("DatasetSpecificationAgentOutcome")
    valid = schema.validate_json(json.dumps(fixtures["valid"]))
    assert valid.status == "completed"
    assert len(fixtures["invalid"]) == 10
    for case in fixtures["invalid"]:
        with pytest.raises(ModelBehaviorError):
            schema.validate_json(case["raw"])


@pytest.mark.parametrize(
    ("handler_name", "error", "expected_status", "expected_category"),
    [
        (
            "invalid_final_output",
            ModelBehaviorError(
                "Invalid JSON raw={api_key: sk-unit-test-secret, scientific: do-not-store}"
            ),
            "invalid_model_output",
            "malformed_json",
        ),
        (
            "model_refusal",
            ModelRefusalError("refusal text must not persist"),
            "model_refused",
            "model_refusal",
        ),
    ],
)
def test_specification_error_handlers_are_terminal_safe_and_preserve_metadata(
    handler_name: str,
    error: Exception,
    expected_status: str,
    expected_category: str,
) -> None:
    calls = 0
    include_in_history = True
    raw_usage = SimpleNamespace(
        requests=1,
        input_tokens=321,
        output_tokens=45,
        input_tokens_details=SimpleNamespace(cached_tokens=12),
    )
    raw_response = SimpleNamespace(
        request_id="req_safe_123",
        response_id="resp_safe_456",
        usage=raw_usage,
        status="incomplete" if handler_name == "invalid_final_output" else "completed",
        incomplete_details=(
            SimpleNamespace(reason="max_output_tokens")
            if handler_name == "invalid_final_output"
            else None
        ),
        output=[SimpleNamespace(type="message", content=[])],
    )

    def runner(*_args, **kwargs):
        nonlocal calls, include_in_history
        calls += 1
        handled = kwargs["error_handlers"][handler_name](
            SimpleNamespace(
                error=error,
                run_data=SimpleNamespace(raw_responses=[raw_response]),
            )
        )
        include_in_history = handled.include_in_history
        return SimpleNamespace(
            final_output=handled.final_output,
            context_wrapper=SimpleNamespace(usage=raw_usage),
            raw_responses=[raw_response],
        )

    turn = provider(runner).run_turn(specification_request(), [], interruption_requested=False)
    assert calls == 1
    assert include_in_history is False
    assert turn.kind == "output"
    assert turn.output["status"] == expected_status
    assert turn.output["specification"] is None
    assert turn.diagnostic.failure_classification == (
        "response_incomplete" if handler_name == "invalid_final_output" else expected_category
    )
    assert turn.diagnostic.provider_request_ids == ["req_safe_123"]
    assert turn.diagnostic.provider_response_ids == ["resp_safe_456"]
    assert turn.diagnostic.usage.input_tokens == 321
    assert turn.diagnostic.usage.output_tokens == 45
    assert turn.diagnostic.usage.cached_tokens == 12
    assert turn.diagnostic.retryable is False
    assert turn.diagnostic.output_item_count == 1
    assert turn.diagnostic.output_item_types == ["message"]
    assert turn.diagnostic.text_output_present is False
    assert turn.diagnostic.bounded_text_length == 0
    assert turn.diagnostic.json_object_present is False
    assert turn.diagnostic.strict_mode is True
    assert turn.diagnostic.request_fingerprint is not None
    assert turn.diagnostic.request_fingerprint.output_type_present is True
    serialized = turn.model_dump_json()
    assert "sk-unit-test-secret" not in serialized
    assert "do-not-store" not in serialized
    assert "refusal text must not persist" not in serialized
