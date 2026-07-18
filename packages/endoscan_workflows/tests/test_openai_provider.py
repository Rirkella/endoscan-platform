from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from endoscan_workflows.config import AgentConfiguration, AgentRunMode
from endoscan_workflows.contracts import AgentBudget, AgentRunRequest, ModelConfiguration
from endoscan_workflows.openai_provider import TOOL_ENVELOPE, OpenAIAgentProvider
from endoscan_workflows.providers import ProviderFailure, ProviderTimeout
from endoscan_workflows.tools import phase0_tool_registry

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
    assert turn.usage.provider_request_ids == ["resp-safe"]
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


def test_provider_timeout_is_normalized() -> None:
    def timeout(*_args, **_kwargs):
        raise TimeoutError("provider internals must not leak")

    with pytest.raises(ProviderTimeout, match="configured timeout"):
        provider(timeout).run_turn(request(), [], interruption_requested=False)


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
