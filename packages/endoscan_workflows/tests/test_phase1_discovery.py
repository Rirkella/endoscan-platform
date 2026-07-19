from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from endoscan_workflows.benchmark import (
    BENCHMARK_CASES,
    deterministic_fixture_observations,
    score_benchmark,
)
from endoscan_workflows.config import AgentConfiguration, AgentRunMode
from endoscan_workflows.contracts import (
    AgentRunResult,
    AgentRunStatus,
    ApprovalDecision,
    ApprovalDecisionValue,
    EndpointBuildCreate,
    UsageReport,
    WorkflowState,
)
from endoscan_workflows.discovery import (
    DatasetCandidate,
    DiscoveryOutput,
    EvidenceReference,
    discovery_request,
    validate_evidence_references,
)
from endoscan_workflows.harness import AgentHarness


def candidate(*, evidence: list[EvidenceReference], status: str = "recommended_for_human_review"):
    return DatasetCandidate(
        candidate_id="candidate-1",
        accession="GSE12345",
        title="Official candidate",
        source="NCBI GEO",
        organism=["Homo sapiens"],
        data_type="Transcriptomic series",
        sample_count=12,
        biological_context="Cultured human cells",
        treatment_control_evidence="Vehicle and treatment groups reported.",
        dose_time_evidence="Dose and time reported.",
        strengths=["Official accession"],
        limitations=["Human label review required"],
        recommendation_status=status,
        evidence_references=evidence,
        accession_verified=True,
        geo_validation_status="public_valid",
    )


def output(*, evidence: list[EvidenceReference]) -> DiscoveryOutput:
    return DiscoveryOutput(
        endpoint_name="Oxidative stress",
        endpoint_definition_summary="Response-defined endpoint",
        run_mode="live",
        live_discovery=True,
        search_strategy="Search and validate GEO series.",
        candidates=[
            candidate(evidence=evidence),
            candidate(evidence=evidence, status="alternative").model_copy(
                update={"candidate_id": "candidate-2", "accession": "GSE12346"}
            ),
        ],
        recommended_candidate_id="candidate-1",
        recommendation="Review candidate-1.",
        decision_summary="Candidate-1 has more complete metadata.",
        unresolved_questions=["Are labels acceptable?"],
        evidence_references=evidence,
        limitations=["No dataset was downloaded."],
        confidence_category="moderate",
    )


def test_unresolved_candidate_and_recommendation_evidence_are_downgraded() -> None:
    missing = EvidenceReference(
        source_artifact_id="missing-artifact",
        field_path="Series_title",
        supports_claim="Candidate title",
    )
    validated = validate_evidence_references(output(evidence=[missing]), {"stored-artifact"})
    assert validated.recommended_candidate_id is None
    assert validated.confidence_category == "low"
    assert validated.evidence_references == []
    assert all(
        item.recommendation_status == "insufficient_metadata" for item in validated.candidates
    )
    assert all(item.accession_verified is False for item in validated.candidates)


def test_resolved_evidence_preserves_recommendation() -> None:
    stored = EvidenceReference(
        source_artifact_id="stored-artifact",
        field_path="Series_title",
        supports_claim="Candidate title",
    )
    validated = validate_evidence_references(output(evidence=[stored]), {"stored-artifact"})
    assert validated.recommended_candidate_id == "candidate-1"
    assert validated.confidence_category == "moderate"


def test_configuration_falls_back_to_replay_without_api_key(monkeypatch) -> None:
    monkeypatch.setenv("ENDOSCAN_AGENT_PROVIDER", "openai")
    monkeypatch.setenv("ENDOSCAN_AGENT_MODE", "live")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    configuration = AgentConfiguration.from_env()
    assert configuration.run_mode is AgentRunMode.REPLAY
    assert configuration.live_enabled is False
    assert configuration.public_status()["api_key_present"] is False


def test_cached_configuration_also_requires_provider_credentials(monkeypatch) -> None:
    monkeypatch.setenv("ENDOSCAN_AGENT_PROVIDER", "openai")
    monkeypatch.setenv("ENDOSCAN_AGENT_MODE", "cached")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert AgentConfiguration.from_env().run_mode is AgentRunMode.REPLAY


def test_configuration_honors_bounded_live_settings(monkeypatch) -> None:
    monkeypatch.setenv("ENDOSCAN_AGENT_PROVIDER", "openai")
    monkeypatch.setenv("ENDOSCAN_AGENT_MODE", "live")
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-key-never-display")
    configuration = AgentConfiguration.from_env()
    assert configuration.run_mode is AgentRunMode.LIVE
    assert configuration.maximum_turns == 6
    assert configuration.maximum_tool_calls == 8
    assert configuration.maximum_input_tokens == 20000
    assert configuration.maximum_output_tokens == 2500
    assert configuration.maximum_cost_usd == 0.20
    assert configuration.timeout_seconds == 120
    assert configuration.retry_count == 0
    assert configuration.public_status()["configured_budget"]["retry_count"] == 0
    assert "unit-test-key-never-display" not in json.dumps(configuration.public_status())


def test_latest_live_precheck_projection_is_allowed_under_new_input_budget() -> None:
    configuration = AgentConfiguration(
        provider="openai",
        run_mode=AgentRunMode.LIVE,
        api_key=SecretStr("offline-placeholder"),
    )
    request = discovery_request(
        workflow_id="build-offline-budget",
        step_id="step-offline-budget",
        endpoint_name="Oxidative stress",
        biological_goal="Bounded offline budget check.",
        configuration=configuration,
    )
    assert request.budget.maximum_input_tokens == 20000
    assert (
        AgentHarness._estimated_next_turn_budget_error(
            request,
            UsageReport(input_tokens=6703),
            UsageReport(input_tokens=3353),
            estimated_next_input=4218,
        )
        is None
    )


def test_discovery_substage_exposes_only_the_next_valid_tool() -> None:
    request = discovery_request(
        workflow_id="build-offline-stage-policy",
        step_id="step-offline-stage-policy",
        endpoint_name="Oxidative stress",
        biological_goal="Bounded offline stage policy check.",
        configuration=AgentConfiguration(
            provider="openai",
            run_mode=AgentRunMode.LIVE,
            api_key=SecretStr("offline-placeholder"),
        ),
    )
    assert AgentHarness._discovery_turn_policy(request, []) == (
        "search_planning",
        ["search_geo_series"],
    )
    histories = [
        {
            "tool_name": "search_geo_series",
            "output": {"result_count": 5, "new_accession_count": 5},
        },
        {
            "tool_name": "validate_geo_accessions",
            "output": {"public_valid_count": 5},
        },
        {
            "tool_name": "inspect_geo_candidates",
            "output": {"inspected_count": 5},
        },
        {
            "tool_name": "compare_dataset_candidates",
            "output": {"candidates": []},
        },
    ]
    expected = [
        ("candidate_validation", ["validate_geo_accessions"]),
        ("candidate_inspection", ["inspect_geo_candidates"]),
        ("final_comparison", ["compare_dataset_candidates"]),
        ("final_output", []),
    ]
    for history, policy in zip(histories, expected, strict=True):
        assert AgentHarness._discovery_turn_policy(request, [history]) == policy


def test_all_zero_searches_have_a_valid_no_candidate_contract() -> None:
    parsed = DiscoveryOutput(
        endpoint_name="Oxidative stress",
        endpoint_definition_summary="Transcriptomic oxidative-stress response.",
        run_mode="live",
        live_discovery=True,
        search_strategy="Two bounded focused searches returned no GEO Series.",
        queries_executed=["query-one", "query-two"],
        search_strategy_steps=[
            {
                "strategy_reason": "Focused human sequencing search.",
                "scientific_terms": ["oxidative stress"],
                "organism_alternatives": ["Homo sapiens"],
                "study_type_alternatives": ["sequencing"],
                "rendered_query": "query-one",
                "result_count": 0,
            },
            {
                "strategy_reason": "Relaxed one study-type filter.",
                "scientific_terms": ["oxidative stress"],
                "organism_alternatives": ["Homo sapiens"],
                "study_type_alternatives": [],
                "rendered_query": "query-two",
                "result_count": 0,
            },
        ],
        candidates=[],
        recommended_candidate_id=None,
        recommendation="No dataset recommendation under the bounded strategy.",
        decision_summary="Human review is required before revising or cancelling the search.",
        unresolved_questions=["Should one transcriptomic context filter be revised?"],
        evidence_references=[],
        limitations=["No candidate found under the current bounded strategy."],
        confidence_category="low",
    )
    assert parsed.candidates == []
    assert parsed.recommended_candidate_id is None
    assert parsed.requires_human_review is True


def test_no_candidate_output_enters_search_review_without_dataset_approval(
    workflow_runtime,
) -> None:
    _database, store, _providers, _harness, service = workflow_runtime
    no_candidate = DiscoveryOutput(
        endpoint_name="Oxidative stress",
        endpoint_definition_summary="Transcriptomic oxidative-stress response.",
        run_mode="replay",
        simulation_label="Offline no-candidate test fixture",
        live_discovery=False,
        search_strategy="All bounded searches returned zero GEO Series.",
        queries_executed=["query-one", "query-two"],
        candidates=[],
        recommended_candidate_id=None,
        recommendation="No dataset recommendation.",
        decision_summary="Review the bounded no-candidate outcome.",
        unresolved_questions=["Should one bounded filter be revised?"],
        evidence_references=[],
        limitations=["No candidate found under the current strategy."],
        confidence_category="low",
    )

    class NoCandidateHarness:
        def run(self, *_args, **_kwargs):
            return "run-offline-no-candidate", AgentRunResult(
                status=AgentRunStatus.COMPLETED,
                output=no_candidate.model_dump(mode="json"),
            )

    service.harness = NoCandidateHarness()
    created = service.create_build(
        EndpointBuildCreate(
            endpoint_name="No candidate endpoint",
            endpoint_slug="no-candidate-endpoint",
            biological_goal="Verify bounded zero-result human-review behavior.",
            created_by="test-admin",
            idempotency_key="no-candidate-review-build",
        )
    )
    started = service.start_build(
        created.id,
        expected_version=created.version,
        actor="test-admin",
        idempotency_key="no-candidate-review-start",
    )
    assert started.current_stage is WorkflowState.AWAITING_SEARCH_REVIEW
    assert started.pending_approval_id is not None
    assert not any(
        item["approval_type"] == "dataset_selection" for item in service.list_approvals(started.id)
    )
    search_review = next(
        item
        for item in service.list_approvals(started.id)
        if item["approval_type"] == "search_revision"
    )
    assert search_review["request"]["requested_action"] == (
        "Request a revised search or cancel the workflow."
    )
    revised = service.decide_approval(
        search_review["id"],
        ApprovalDecision(
            decision=ApprovalDecisionValue.REQUEST_REVISION,
            reviewer_id="test-admin",
            reviewer_comment="Broaden one bounded scientific filter.",
            expected_version=started.version,
            idempotency_key="no-candidate-revised-search",
            artifact_hashes=search_review["request"]["artifact_hashes"],
        ),
    )
    assert revised.current_stage is WorkflowState.DISCOVERING_DATA
    candidate_artifact = next(
        item
        for item in store.list_artifacts(started.id)
        if item.artifact_type == "dataset_candidates"
    )
    _descriptor, raw = store.get(candidate_artifact.id)
    assert json.loads(raw)["recommended_candidate_id"] is None


def test_only_public_valid_geo_candidate_may_be_recommended() -> None:
    evidence = [
        EvidenceReference(
            source_artifact_id="stored-artifact",
            field_path="Series_title",
            supports_claim="Candidate title",
        )
    ]
    invalid = candidate(evidence=evidence).model_copy(
        update={"geo_validation_status": "not_public"}
    )
    with pytest.raises(ValidationError, match="Only a public_valid GEO candidate"):
        DiscoveryOutput(
            endpoint_name="Oxidative stress",
            endpoint_definition_summary="Response-defined endpoint",
            run_mode="live",
            live_discovery=True,
            search_strategy="Search and validate GEO series.",
            candidates=[invalid],
            recommended_candidate_id=invalid.candidate_id,
            recommendation="Review candidate-1.",
            decision_summary="Candidate appears relevant but is not public.",
            unresolved_questions=["Can the record be made public?"],
            evidence_references=evidence,
            limitations=["The record is not publicly retrievable."],
            confidence_category="low",
        )


def test_replay_workflow_stops_at_dataset_approval_with_phase1_artifacts(
    workflow_runtime,
) -> None:
    _database, store, _providers, _harness, service = workflow_runtime
    registry = service.repo_root / "registry" / "models" / "endpoints.json"
    registry_before = hashlib.sha256(registry.read_bytes()).hexdigest()
    created = service.create_build(
        EndpointBuildCreate(
            endpoint_name="Oxidative stress",
            endpoint_slug="phase1-replay",
            biological_goal="Prepare a bounded replay recommendation.",
            created_by="test",
            idempotency_key="phase1-replay-build",
        )
    )
    started = service.start_build(
        created.id,
        expected_version=created.version,
        actor="test",
        idempotency_key="phase1-replay-start",
    )
    assert started.current_stage is WorkflowState.AWAITING_DATASET_APPROVAL
    assert started.pending_approval_id
    artifacts = store.list_artifacts(started.id)
    by_type = {item.artifact_type: item for item in artifacts}
    assert {"dataset_candidates", "search_strategy", "agent_recommendation"} <= set(by_type)
    _descriptor, raw = store.get(by_type["dataset_candidates"].id)
    content = json.loads(raw)
    assert content["run_mode"] == "replay"
    assert content["live_discovery"] is False
    assert content["simulation_label"] == "Prepared validated replay fixture"
    approval = next(
        item
        for item in service.list_approvals(started.id)
        if item["approval_type"] == "dataset_selection"
    )
    assert len(approval["request"]["artifact_hashes"]) == 3
    assert hashlib.sha256(registry.read_bytes()).hexdigest() == registry_before


def test_phase1_benchmark_has_all_twelve_cases_and_required_metrics() -> None:
    assert len(BENCHMARK_CASES) == 12
    assert len({item.case_id for item in BENCHMARK_CASES}) == 12
    metrics = score_benchmark(deterministic_fixture_observations())
    assert metrics["case_count"] == 12
    assert metrics["real_accession_precision"] == 1.0
    assert metrics["unsupported_claim_rate"] == 0.0
    assert metrics["citation_validity"] == 1.0
    assert metrics["correct_rejection_or_escalation"] == 1.0
    assert metrics["tool_call_count"] == 12


def test_committed_replay_fixture_is_labeled_and_schema_valid() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "endoscan_workflows"
        / "fixtures"
        / "phase1_oxidative_stress_replay.json"
    )
    fixture = json.loads(path.read_text(encoding="utf-8"))
    parsed = DiscoveryOutput.model_validate(fixture["output"])
    assert fixture["fixture_kind"] == "prepared_validated_replay"
    assert fixture["live_scientific_discovery"] is False
    assert parsed.run_mode == "replay"
    assert parsed.live_discovery is False
    assert all(item.accession.startswith("SIM-") for item in parsed.candidates)
