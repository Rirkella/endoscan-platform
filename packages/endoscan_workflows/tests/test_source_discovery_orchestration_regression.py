from __future__ import annotations

from collections import Counter

import httpx
import pytest
from pydantic import SecretStr

from endoscan_workflows.config import AgentConfiguration, AgentRunMode
from endoscan_workflows.contracts import (
    ApprovalDecision,
    ApprovalDecisionValue,
    EndpointBuildCreate,
    ProviderTurn,
    UsageReport,
    WorkflowKind,
    WorkflowState,
)
from endoscan_workflows.discovery_tools import DiscoveryToolService
from endoscan_workflows.harness import AgentHarness
from endoscan_workflows.providers import ProviderRegistry
from endoscan_workflows.reviewed_source_adapters import production_reviewed_source_registry
from endoscan_workflows.source_cache import SourceResponseCache
from endoscan_workflows.source_security import ScientificSourceClient
from endoscan_workflows.tools import phase1_tool_registry


def _approval_policy() -> dict:
    return {
        "policy_version": "1.0.0",
        "activity_representation": "Retain continuous values and version derived labels.",
        "observation_grain": "Keep compound and experimental contexts distinct.",
        "evidence_hierarchy": "Require direct measured activity with provenance.",
        "multiple_activity_assays": "Keep assay and modality records separate.",
        "conflicting_activity_records": "Preserve conflicts with flags.",
        "transcriptomic_contexts": "Keep cell, dose, duration and control distinct.",
        "repeated_transcriptomic_signatures": "Retain repeat provenance.",
        "quality_thresholds": "Defer thresholds until source distributions are inspected.",
        "minimum_usable_coverage": "Defer minimums until joinability is measured.",
        "missingness": "Permit explicit staging missingness only.",
        "mandatory_output_fields": [
            "canonical_compound_id",
            "canonical_smiles",
            "inchikey",
            "transcriptomic_response_vector",
            "transcriptomic_feature_schema",
            "cell_or_tissue_model",
            "dose",
            "exposure_duration",
            "transcriptomic_control_reference",
            "endpoint_activity_value",
            "endpoint_activity_label",
            "endpoint_modality",
            "assay_id",
            "assay_context",
            "complete_provenance",
            "quality_flags",
            "uncertainty_flags",
        ],
        "nullable_output_fields": ["endpoint_activity_value", "endpoint_activity_label"],
        "optional_output_fields": ["preferred_compound_name"],
        "population_constraints": [
            "At least one endpoint activity field is populated in finalized records."
        ],
        "source_discovery_requires_explicit_authorization": True,
    }


class ScenarioProvider:
    name = "openai"

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.calls: list[dict] = []

    def estimate_context_components(self, request, history) -> dict[str, int]:
        if (
            self.scenario == "budget"
            and request.agent_name == "Activity Evidence Discovery Agent"
            and history
        ):
            return {"compact_next_turn": 20_000}
        return {
            "instructions": 300,
            "requirements": 200,
            "stage_tools": 250 * len(request.available_tools),
            "compact_history": 120 * len(history),
            "output_schema": 300,
        }

    def run_turn(self, request, history, *, interruption_requested):
        assert interruption_requested is False
        self.calls.append(
            {
                "agent": request.agent_name,
                "history": len(history),
                "substage": request.context.get("discovery_substage"),
                "tools": tuple(request.available_tools),
            }
        )
        agent = request.agent_name
        if agent == "Activity Evidence Discovery Agent":
            if self.scenario == "failure" and not history:
                return self._tool(
                    "search_activity_sources",
                    {
                        "source_system": "Unrelated activity catalogue label",
                        "query": "example receptor binding agonism antagonism",
                        "biological_target": "Example receptor",
                        "endpoint_modality": "binding, agonism, antagonism",
                        "maximum_results": 3,
                    },
                    "invalid-activity-contract",
                )
            completed_modalities = {
                (item.get("normalized_arguments") or {}).get("endpoint_modality")
                for item in history
                if item.get("tool_name") == "search_activity_sources"
            }
            missing = [
                item
                for item in ("binding", "agonism", "antagonism")
                if item not in completed_modalities
            ]
            if missing:
                modality = missing[0]
                return self._tool(
                    "search_activity_sources",
                    {
                        "source_system": "pubchem-bioassay",
                        "query": f"example receptor {modality}",
                        "biological_target": "Example receptor",
                        "endpoint_modality": modality,
                        "maximum_results": 3,
                    },
                    f"activity-{modality}",
                )
            return self._output()
        if agent == "Transcriptomic Evidence Discovery Agent":
            completed = {item.get("tool_name") for item in history}
            for operation in ("search_transcriptomic_sources", "search_lincs_resources"):
                if operation not in completed:
                    return self._tool(
                        operation,
                        {
                            "query": (
                                "example receptor chemical perturbation"
                                if operation == "search_transcriptomic_sources"
                                else "example receptor LINCS perturbagen"
                            ),
                            "maximum_results": 3,
                        },
                        f"transcript-{operation}",
                    )
            return self._output()
        if agent == "Chemical Identity and Structure Source Discovery Agent":
            if not history:
                return self._tool(
                    "resolve_compound_identity_sample",
                    {
                        "source_system": "pubchem-compound",
                        "sampled_identifiers": ["123"],
                        "identifier_type": "cid",
                        "maximum_results": 2,
                    },
                    "identity-sample",
                )
            return self._output()
        if agent == "Supporting Metadata Discovery Agent":
            if not history:
                return self._tool(
                    "inspect_supporting_metadata",
                    {
                        "source_system": "ncbi-supporting-metadata",
                        "source_identifiers": ["1001"],
                        "maximum_results": 2,
                    },
                    "supporting-metadata",
                )
            return self._output()
        raise AssertionError(f"unexpected agent {agent}")

    @staticmethod
    def _tool(name: str, arguments: dict, key: str) -> ProviderTurn:
        return ProviderTurn(
            kind="tool",
            tool_request={
                "tool_name": name,
                "arguments": arguments,
                "idempotency_key": key,
            },
            usage=UsageReport(input_tokens=400, output_tokens=50, provider_invocations=1),
        )

    @staticmethod
    def _output() -> ProviderTurn:
        return ProviderTurn(
            kind="output",
            output={
                "schema_version": "1.0.0",
                "status": "completed",
                "relevance_assessments": [],
                "ranked_observation_ids": [],
                "modality_fit_explanations": [],
                "unresolved_scientific_concerns": [],
                "recommended_follow_up_inspections": [],
                "safe_summary": "Source-neutral offline review completed.",
            },
            usage=UsageReport(input_tokens=300, output_tokens=80, provider_invocations=1),
        )


def _run(workflow_runtime, scenario: str):
    database, store, _providers, _harness, service = workflow_runtime
    source_requests: list[str] = []

    def fixture(request: httpx.Request) -> httpx.Response:
        source_requests.append(str(request.url))
        if request.url.host == "pubchem.ncbi.nlm.nih.gov":
            return httpx.Response(
                200,
                json={
                    "PropertyTable": {
                        "Properties": [
                            {
                                "CID": 123,
                                "Title": "Synthetic compound",
                                "CanonicalSMILES": "CCO",
                                "IsomericSMILES": "CCO",
                                "InChIKey": "SYNTHETIC-INCHIKEY",
                            }
                        ]
                    }
                },
                request=request,
            )
        if request.url.path.endswith("esearch.fcgi"):
            query = str(request.url.params.get("term", "")).casefold()
            database_name = request.url.params.get("db")
            identifiers: list[str] = []
            if database_name == "pcassay" and scenario in {"success", "budget"}:
                identifiers = ["1001"]
            elif database_name != "pcassay" and scenario in {"success", "partial"}:
                identifiers = [] if scenario == "partial" and "lincs" in query else ["2001"]
            return httpx.Response(
                200,
                json={"esearchresult": {"idlist": identifiers}},
                request=request,
            )
        return httpx.Response(200, json={"linksets": []}, request=request)

    client = ScientificSourceClient(
        transport=httpx.MockTransport(fixture),
        maximum_attempts=1,
        sleep=lambda _seconds: None,
    )
    cache = SourceResponseCache(database)
    reviewed = production_reviewed_source_registry(client=client, cache=cache, artifacts=store)
    provider = ScenarioProvider(scenario)
    providers = ProviderRegistry()
    providers.register("openai", lambda: provider)
    tools = phase1_tool_registry(
        service.repo_root,
        DiscoveryToolService(cache, store, client),
        reviewed,
    )
    service.harness = AgentHarness(database, providers, tools)
    service.reviewed_source_adapters = reviewed
    service.agent_configuration = AgentConfiguration(
        provider="openai",
        worker_provider="openai",
        planner_provider="openai",
        run_mode=AgentRunMode.LIVE,
        api_key=SecretStr("offline-placeholder-never-read"),
        retry_count=0,
    )
    build = service.create_build(
        EndpointBuildCreate(
            endpoint_name="Receptor X binding, agonism, and antagonism",
            endpoint_slug=f"example-receptor-{scenario}",
            biological_goal=(
                "Determine which public compound-level receptor activity modalities can be "
                "connected to public compound-induced transcriptomic responses."
            ),
            created_by="offline-test",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            benchmark_mode="blind_training_dataset_discovery",
            idempotency_key=f"source-neutral-{scenario}",
        )
    )
    waiting = service.start_build(
        build.id,
        expected_version=0,
        actor="offline-test",
        idempotency_key=f"source-neutral-{scenario}:start",
    )
    approvals = service.list_approvals(build.id, pending_only=True)
    assert approvals, (
        waiting.current_stage.value,
        service.training_dataset_workflow(build.id).get("specification_semantic_validation"),
    )
    approval = approvals[0]
    approved = service.decide_approval(
        approval["id"],
        ApprovalDecision(
            decision=ApprovalDecisionValue.APPROVE,
            reviewer_id="offline-test",
            expected_version=waiting.version,
            idempotency_key=f"source-neutral-{scenario}:approve",
            artifact_hashes=approval["request"]["artifact_hashes"],
            dataset_specification_policy=_approval_policy(),
        ),
    )
    service.derive_training_dataset_requirements(
        build.id,
        actor="deterministic-orchestrator",
        idempotency_key=f"source-neutral-{scenario}:requirements",
    )
    service.authorize_source_discovery(
        build.id,
        expected_version=approved.version,
        actor="offline-test",
        idempotency_key=f"source-neutral-{scenario}:authorize",
        confirmed=True,
    )
    completed = service.run_authorized_source_discovery(build.id)
    return service, build.id, completed, provider, source_requests, client


@pytest.mark.parametrize("scenario", ["success", "partial", "empty", "failure", "budget"])
def test_source_neutral_offline_four_agent_regression_paths(workflow_runtime, scenario) -> None:
    service, build_id, completed, provider, source_requests, client = _run(
        workflow_runtime, scenario
    )
    try:
        assert completed.current_stage is WorkflowState.AWAITING_SOURCE_INVENTORY_REVIEW
        workflow = service.training_dataset_workflow(build_id)
        fragments = {item["agent_name"]: item["fragment"] for item in workflow["source_fragments"]}
        inventory = workflow["verified_source_inventory"]
        calls_by_agent = Counter(item["agent"] for item in provider.calls)
        assert workflow["assembly_strategies"] is None
        assert workflow["capability_matrix"] is not None
        assert workflow["gap_report"] is not None

        if scenario == "success":
            assert len(service.agent_runs(build_id)) == 4
            assert len(inventory["sources"]) >= 4
            assert calls_by_agent["Activity Evidence Discovery Agent"] == 4
            assert calls_by_agent["Transcriptomic Evidence Discovery Agent"] == 3
            activity_modalities = {
                item["search_outcome"]["query_scope"].get("endpoint_modality")
                for item in workflow["source_search_outcomes"]
                if item["search_outcome"]["operation"] == "search_activity_sources"
            }
            assert activity_modalities == {"binding", "agonism", "antagonism"}
            activity_turns = [
                item
                for item in provider.calls
                if item["agent"] == "Activity Evidence Discovery Agent"
            ]
            assert all(
                set(item["tools"])
                <= {"search_activity_sources", "inspect_epa_public_assay_target_mapping"}
                for item in activity_turns[:3]
            )
            assert "search_activity_sources" not in activity_turns[3]["tools"]
            transcript_turns = [
                item
                for item in provider.calls
                if item["agent"] == "Transcriptomic Evidence Discovery Agent"
            ]
            assert transcript_turns[0]["tools"] == (
                "search_transcriptomic_sources",
                "search_lincs_resources",
            )
            assert transcript_turns[1]["tools"] == ("search_lincs_resources",)
            assert not {
                "search_transcriptomic_sources",
                "search_lincs_resources",
            }.intersection(transcript_turns[2]["tools"])
            activity_request = service.agent_run(
                next(
                    item["id"]
                    for item in service.agent_runs(build_id)
                    if item["agent_name"] == "Activity Evidence Discovery Agent"
                )
            )["request"]
            compact = activity_request["context"]["validated_artifacts"]
            assert "requirement_summary" in compact
            assert "reviewed_adapter_capabilities" in compact
            assert "target_specification" not in compact
            assert "component_requirements" not in compact
        elif scenario == "partial":
            assert len(service.agent_runs(build_id)) == 4
            assert fragments["Activity Evidence Discovery Agent"]["agent_review_status"] == (
                "completed_no_candidates"
            )
            assert any(
                "transcriptomic" in role
                for source in inventory["sources"]
                for role in source["source_roles"]
            )
            assert calls_by_agent["Chemical Identity and Structure Source Discovery Agent"] > 0
            assert calls_by_agent["Supporting Metadata Discovery Agent"] > 0
        elif scenario == "empty":
            assert len(service.agent_runs(build_id)) == 2
            assert inventory["sources"] == []
            assert workflow["gap_report"]["classification"] == "completed_no_candidates"
            for agent in (
                "Chemical Identity and Structure Source Discovery Agent",
                "Supporting Metadata Discovery Agent",
            ):
                assert fragments[agent]["agent_review_status"] == "skipped_dependency_not_met"
                assert fragments[agent]["provider_invocations"] == 0
                assert fragments[agent]["tool_calls"] == 0
                assert fragments[agent]["scientific_source_requests"] == 0
                assert calls_by_agent[agent] == 0
        elif scenario == "failure":
            activity_run = next(
                item
                for item in service.agent_runs(build_id)
                if item["agent_name"] == "Activity Evidence Discovery Agent"
            )
            diagnostic = service.agent_run(activity_run["id"])["tool_calls"][0]["result"][
                "error"
            ]["tool_diagnostic"]
            assert diagnostic["invocation_stage"] == "input_validation"
            assert diagnostic["source_transport_started"] is False
            assert diagnostic["validation_error_category"] == "input_schema_validation"
            assert not any("pcassay" in request for request in source_requests)
            assert workflow["gap_report"]["classification"] == "discovery_execution_incomplete"
        else:
            fragment = fragments["Activity Evidence Discovery Agent"]
            assert fragment["agent_review_status"] == "budget_stopped"
            assert fragment["candidate_records"]
            assert fragment["observation_ids"]
            assert workflow["gap_report"]["classification"] == "discovery_execution_incomplete"

        before = (len(provider.calls), len(source_requests), len(service.agent_runs(build_id)))
        repeated = service.run_authorized_source_discovery(build_id)
        assert repeated.version == completed.version
        after = (len(provider.calls), len(source_requests), len(service.agent_runs(build_id)))
        assert after == before
    finally:
        client.close()
