from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import inspect, select, text, update
from sqlalchemy.exc import DatabaseError

from endoscan_workflows.config import AgentConfiguration, AgentRunMode
from endoscan_workflows.contracts import (
    ActorType,
    ApprovalDecision,
    ApprovalDecisionValue,
    EndpointBuildCreate,
    ProviderTurn,
    TransitionRequest,
    UsageReport,
    WorkflowKind,
    WorkflowState,
)
from endoscan_workflows.database import WorkflowDatabase
from endoscan_workflows.discovery_tools import DiscoveryToolService
from endoscan_workflows.errors import (
    ArtifactIntegrityError,
    ArtifactTooLarge,
    GuardNotSatisfied,
    InvalidTransition,
    StaleWorkflowVersion,
    WorkflowConflict,
    WorkflowNotFound,
)
from endoscan_workflows.harness import AgentHarness
from endoscan_workflows.models import EndpointBuildRow, WorkflowErrorRow, WorkflowEventRow
from endoscan_workflows.providers import (
    ProviderFailure,
    ProviderRegistry,
    ProviderTimeout,
)
from endoscan_workflows.repository import load_versioned_json
from endoscan_workflows.reviewed_source_adapters import (
    REVIEWED_ADAPTER_DEFINITIONS,
    ReviewedSourceAdapter,
    ReviewedSourceAdapterRegistry,
    production_reviewed_source_registry,
)
from endoscan_workflows.source_cache import SourceResponseCache
from endoscan_workflows.source_security import ScientificSourceClient
from endoscan_workflows.tools import phase1_tool_registry
from endoscan_workflows.training_dataset import (
    EndpointDiscoveryMode,
    EndpointDiscoveryScope,
    EndpointDiscoveryScopeProvenance,
    EndpointSemanticModality,
    TrainingDatasetSpecification,
)


def create_build(service, key="phase0-test-create"):
    return service.create_build(
        EndpointBuildCreate(
            endpoint_name="Oxidative stress",
            endpoint_slug="oxidative-stress",
            biological_goal=(
                "Evaluate a response-defined oxidative-stress endpoint from transcriptomic "
                "signatures."
            ),
            created_by="test-admin",
            idempotency_key=key,
        )
    )


def start_build(service, build, key="phase0-test-start"):
    return service.start_build(
        build.id,
        expected_version=build.version,
        actor="test-admin",
        idempotency_key=key,
    )


def test_fresh_database_migrates_with_wal_foreign_keys_and_all_tables(tmp_path) -> None:
    database = WorkflowDatabase(tmp_path / "fresh.db")
    database.migrate()
    tables = set(inspect(database.engine).get_table_names())
    assert {
        "endpoint_builds",
        "workflow_steps",
        "workflow_events",
        "approvals",
        "artifacts",
        "agent_runs",
        "tool_calls",
        "human_decisions",
        "workflow_errors",
        "source_response_cache",
        "training_dataset_workflows",
        "alembic_version",
    }.issubset(tables)
    assert database.capability() == {
        "available": True,
        "journal_mode": "wal",
        "foreign_keys": True,
    }
    columns = {
        item["name"] for item in inspect(database.engine).get_columns("training_dataset_workflows")
    }
    assert {
        "endpoint_discovery_scope_json",
        "specification_draft_json",
        "specification_outcome_json",
        "specification_semantic_validation_json",
        "specification_compilation_outcome_json",
        "specification_review_record_json",
        "source_discovery_authorization_json",
        "source_discovery_budget_json",
        "source_observations_json",
        "source_fragments_json",
    }.issubset(columns)
    database.dispose()


def test_specification_outcome_migration_upgrades_phase1_database_in_place(tmp_path) -> None:
    database = WorkflowDatabase(tmp_path / "phase1-before-outcomes.db")
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE training_dataset_workflows ("
                "workflow_id VARCHAR(128) PRIMARY KEY, specification_json TEXT)"
            )
        )
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(64))"))
        connection.execute(
            text(
                "INSERT INTO alembic_version (version_num) "
                "VALUES ('0003_training_dataset_discovery')"
            )
        )
    database.migrate()
    columns = {
        item["name"] for item in inspect(database.engine).get_columns("training_dataset_workflows")
    }
    assert {
        "endpoint_discovery_scope_json",
        "specification_draft_json",
        "specification_outcome_json",
        "specification_semantic_validation_json",
        "specification_compilation_outcome_json",
        "specification_review_record_json",
    }.issubset(columns)
    database.dispose()


def training_specification() -> TrainingDatasetSpecification:
    return TrainingDatasetSpecification(
        specification_id="spec-offline-general",
        endpoint_name="Example functional endpoint",
        biological_target="Example target",
        endpoint_modality="functional response",
        endpoint_definition="Compound activity measured by a reviewed functional assay.",
        intended_prediction_task="Predict endpoint activity for an explicitly bounded context.",
        prediction_unit="compound_cell_context_dose_time",
        acceptable_activity_representations=["continuous_activity", "binary_active_inactive"],
        acceptable_transcriptomic_representations=["processed differential signature"],
        compound_identity_requirements=["PubChem CID", "InChIKey"],
        chemical_structure_requirements=["canonical SMILES"],
        experimental_context_requirements=["cell or tissue", "dose", "time", "control"],
        mandatory_output_fields=[
            "canonical_compound_id",
            "canonical_smiles",
            "transcriptomic_signature",
            "endpoint_activity_value",
            "provenance",
        ],
        minimum_evidence_requirements=["official primary public records"],
        intended_scope_of_claim="Research use for the explicit endpoint and contexts only.",
    )


def approved_specification_policy() -> dict:
    return {
        "policy_version": "1.0.0",
        "activity_representation": (
            "Retain continuous primary measurements and permit separately versioned derived "
            "binary or multiclass labels without discarding continuous values."
        ),
        "observation_grain": (
            "Keep compound by transcriptomic experimental context records distinct during "
            "discovery and initial ingestion."
        ),
        "evidence_hierarchy": (
            "Require direct experimentally measured endpoint activity; retain supporting "
            "evidence only in provenance and quality flags."
        ),
        "multiple_activity_assays": (
            "Retain assay-specific records separately and defer combination to a later "
            "human-approved curation policy."
        ),
        "conflicting_activity_records": (
            "Preserve every conflicting record with provenance and conflict flags; do not "
            "resolve conflicts during discovery."
        ),
        "transcriptomic_contexts": (
            "Keep cell or tissue, dose, duration, control and processing contexts as distinct "
            "observations."
        ),
        "repeated_transcriptomic_signatures": (
            "Retain repeated signatures separately and report replicate structure and quality "
            "before any aggregation policy."
        ),
        "quality_thresholds": (
            "Set no arbitrary numeric threshold before real source fields and distributions "
            "have been inspected."
        ),
        "minimum_usable_coverage": (
            "Set no arbitrary count or overlap minimum before deterministic joinability "
            "diagnostics are available."
        ),
        "missingness": (
            "Permit explicit staging missingness, but require finalized observations to satisfy "
            "the approved mandatory fields and population constraints."
        ),
        "mandatory_output_fields": [
            "canonical_compound_id",
            "preferred_compound_name",
            "canonical_smiles",
            "inchikey",
            "source_specific_compound_ids",
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
        "nullable_output_fields": [
            "preferred_compound_name",
            "endpoint_activity_value",
            "endpoint_activity_label",
        ],
        "optional_output_fields": ["isomeric_smiles"],
        "population_constraints": [
            "At least one of endpoint_activity_value or endpoint_activity_label is populated.",
            "Both activity fields are populated when a label derives from a retained value.",
            "Incomplete records may remain in staging but not enter the finalized table.",
        ],
        "source_discovery_requires_explicit_authorization": True,
    }


def test_training_dataset_draft_is_hint_free_durable_and_strategy_locked(
    workflow_runtime,
) -> None:
    _database, store, _providers, _harness, service = workflow_runtime
    build = service.create_build(
        EndpointBuildCreate(
            endpoint_name="X receptor antagonist",
            endpoint_slug="example-functional-endpoint",
            biological_goal=(
                "Construct compound-level public training data linking compounds, structures, "
                "transcriptomic response signatures and endpoint activity."
            ),
            created_by="test-admin",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            benchmark_mode="blind_training_dataset_discovery",
            idempotency_key="training-dataset-draft",
        )
    )
    workflow = service.training_dataset_workflow(build.id)
    context = workflow["initial_context"]
    assert build.current_stage is WorkflowState.DRAFT
    assert service.agent_runs(build.id) == []
    assert workflow["verified_source_inventory"] is None
    assert workflow["assembly_strategies"] is None
    assert context["source_hints"] == []
    assert context["article_hint"] is None
    assert context["doi_hint"] is None
    assert context["assay_id_hint"] is None
    assert context["expected_overlap_hint"] is None
    assert context["allowed_tools"]
    assert context["budgets"]["per_agent"]["provider_retries"] == 0
    assert {item.artifact_type for item in store.list_artifacts(build.id)} == {
        "endpoint_definition",
        "blind_context_audit",
        "endpoint_request_semantic_hints",
    }
    with pytest.raises(ValueError, match="specification"):
        service.validate_training_dataset_strategy_checkpoint(build.id)

    started = service.start_build(
        build.id,
        expected_version=0,
        actor="test-admin",
        idempotency_key="start-training-dataset",
    )
    assert started.current_stage is WorkflowState.AWAITING_DATASET_SPECIFICATION_REVIEW
    runs = service.agent_runs(build.id)
    assert runs == []
    workflow = service.training_dataset_workflow(build.id)
    assert workflow["specification_compilation_outcome"]["provider_invocations"] == 0
    assert workflow["specification_draft"]["biological_target"] == "X receptor"
    assert workflow["specification_review"]["status"] == "not_run"


def test_training_dataset_documents_survive_readback_and_requirements_are_deterministic(
    workflow_runtime,
) -> None:
    _database, _store, _providers, _harness, service = workflow_runtime
    build = service.create_build(
        EndpointBuildCreate(
            endpoint_name="X receptor antagonist",
            endpoint_slug="example-functional-endpoint",
            biological_goal="Construct a source-neutral public training-dataset plan.",
            created_by="test-admin",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            benchmark_mode="standard_training_dataset_discovery",
            idempotency_key="training-documents",
        )
    )
    spec = training_specification()
    stored = service.persist_training_dataset_document(
        build.id,
        document_name="specification",
        value=spec.model_dump(mode="json"),
        actor="Dataset Specification Agent",
        idempotency_key="persist-training-spec",
    )
    service.derive_training_dataset_requirements(
        build.id,
        actor="deterministic-orchestrator",
        idempotency_key="derive-training-requirements",
    )
    workflow = service.training_dataset_workflow(build.id)
    assert len(stored["sha256"]) == 64
    assert workflow["target_specification"]["specification_id"] == spec.specification_id
    roles = {item["role"] for item in workflow["component_requirements"]["requirements"]}
    assert {"endpoint_activity", "transcriptomic_matrix", "compound_identity"}.issubset(roles)


def test_training_dataset_specification_approval_precedes_discovery(
    workflow_runtime,
) -> None:
    _database, _store, _providers, _harness, service = workflow_runtime
    build = service.create_build(
        EndpointBuildCreate(
            endpoint_name="X receptor antagonist",
            endpoint_slug="example-functional-endpoint",
            biological_goal=(
                "Construct a compound-level public training dataset with transcriptomic responses."
            ),
            created_by="test-admin",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            benchmark_mode="blind_training_dataset_discovery",
            idempotency_key="training-spec-approval",
        )
    )
    waiting = service.start_build(
        build.id,
        expected_version=0,
        actor="test-admin",
        idempotency_key="training-spec-start",
    )
    assert waiting.current_stage is WorkflowState.AWAITING_DATASET_SPECIFICATION_REVIEW
    approval = service.list_approvals(build.id, pending_only=True)[0]
    assert approval["approval_type"] == "dataset_specification"
    assert len(approval["request"]["artifact_hashes"]) >= 5
    approved = service.decide_approval(
        approval["id"],
        ApprovalDecision(
            decision=ApprovalDecisionValue.APPROVE,
            reviewer_id="test-admin",
            expected_version=waiting.version,
            idempotency_key="training-spec-approved",
            artifact_hashes=approval["request"]["artifact_hashes"],
            dataset_specification_policy=approved_specification_policy(),
        ),
    )
    assert approved.current_stage is WorkflowState.DERIVING_COMPONENT_REQUIREMENTS
    derived = service.derive_training_dataset_requirements(
        build.id,
        actor="deterministic-orchestrator",
        idempotency_key="derive-approved-requirements",
    )
    assert len(derived["sha256"]) == 64
    waiting = service.get_build(build.id)
    assert waiting.current_stage is WorkflowState.DERIVING_COMPONENT_REQUIREMENTS
    workflow = service.training_dataset_workflow(build.id)
    assert workflow["target_specification"]["approved_policy_version"] == "1.0.0"
    assert workflow["target_specification"]["requires_human_review"] is False
    assert len(workflow["target_specification"]["approved_policy_decisions"]) == 10
    assert len(workflow["component_requirements"]["requirements"]) == 11
    assert workflow["assembly_strategies"] is None
    artifact_types = {
        item.artifact_type for item in service.artifact_store.list_artifacts(build.id)
    }
    assert "dataset_specification_human_policy" in artifact_types
    assert "training_dataset_specification" in artifact_types
    assert service.agent_runs(build.id) == []
    with pytest.raises(GuardNotSatisfied, match="dedicated reviewed-adapter authorization"):
        service.continue_training_dataset_workflow(
            build.id,
            expected_version=waiting.version,
            actor="test-admin",
            idempotency_key="authorize-source-discovery",
        )


def test_fixed_and_human_scoped_broad_builds_coexist_without_provider_or_source_calls(
    workflow_runtime,
) -> None:
    _database, _store, _providers, _harness, service = workflow_runtime
    fixed = service.create_build(
        EndpointBuildCreate(
            endpoint_name="Thyroid hormone receptor antagonist",
            endpoint_slug="thyroid-receptor-antagonist-fixed",
            biological_goal="Construct a compound-level transcriptomic training dataset.",
            created_by="test-admin",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            benchmark_mode="blind_training_dataset_discovery",
            idempotency_key="fixed-and-broad-fixed",
        )
    )
    fixed_waiting = service.start_build(
        fixed.id,
        expected_version=fixed.version,
        actor="test-admin",
        idempotency_key="fixed-and-broad-fixed-start",
    )
    fixed_workflow = service.training_dataset_workflow(fixed.id)
    assert fixed_workflow["endpoint_discovery_scope"]["mode"] == "fixed_modality"
    assert fixed_workflow["endpoint_discovery_scope"]["candidate_modalities"] == ["antagonism"]

    broad = service.create_build(
        EndpointBuildCreate(
            endpoint_name="Thyroid hormone receptor activity",
            endpoint_slug="thyroid-receptor-activity-broad",
            biological_goal=(
                "Determine which public compound-level thyroid hormone receptor activity "
                "modalities can be connected to public compound-induced transcriptomic "
                "responses to construct training datasets."
            ),
            created_by="test-admin",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            benchmark_mode="blind_training_dataset_discovery",
            idempotency_key="fixed-and-broad-broad",
        )
    )
    revision = service.start_build(
        broad.id,
        expected_version=broad.version,
        actor="test-admin",
        idempotency_key="fixed-and-broad-broad-start",
    )
    assert revision.current_stage is WorkflowState.AWAITING_DATASET_SPECIFICATION_REVISION
    scope = EndpointDiscoveryScope(
        mode=EndpointDiscoveryMode.BROAD_MODALITY_EXPLORATION,
        biological_target="Thyroid hormone receptor",
        fixed_modality=None,
        candidate_modalities=[
            EndpointSemanticModality.BINDING,
            EndpointSemanticModality.AGONISM,
            EndpointSemanticModality.ANTAGONISM,
        ],
        explicitly_excluded_modalities=[],
        preserve_modalities_separately=True,
        aggregation_allowed_later=True,
        aggregation_requires_human_approval=True,
        selection_deferred_until="assembly_strategy_review",
        scientific_scope=(
            "Explore binding, agonism, and antagonism separately and defer endpoint selection."
        ),
        provenance=[EndpointDiscoveryScopeProvenance.HUMAN_SCOPED_CONFIGURATION],
    )
    waiting = service.retry_training_dataset_specification(
        broad.id,
        expected_version=revision.version,
        actor="test-admin",
        idempotency_key="fixed-and-broad-broad-revision",
        endpoint_discovery_scope=scope,
    )
    assert waiting.current_stage is WorkflowState.AWAITING_DATASET_SPECIFICATION_REVIEW
    workflow = service.training_dataset_workflow(broad.id)
    assert workflow["endpoint_discovery_scope"] == scope.model_dump(mode="json")
    assert workflow["specification_draft"]["endpoint_modality"] is None
    assert workflow["component_requirements"] is None
    assert service.agent_runs(broad.id) == []
    pending = service.list_approvals(broad.id, pending_only=True)
    assert len(pending) == 1
    assert pending[0]["approval_type"] == "dataset_specification"

    approved = service.decide_approval(
        pending[0]["id"],
        ApprovalDecision(
            decision=ApprovalDecisionValue.APPROVE,
            reviewer_id="test-admin",
            expected_version=waiting.version,
            idempotency_key="fixed-and-broad-broad-approved",
            artifact_hashes=pending[0]["request"]["artifact_hashes"],
            dataset_specification_policy=approved_specification_policy(),
        ),
    )
    service.derive_training_dataset_requirements(
        broad.id,
        actor="deterministic-orchestrator",
        idempotency_key="fixed-and-broad-broad-requirements",
    )
    assert approved.current_stage is WorkflowState.DERIVING_COMPONENT_REQUIREMENTS
    requirements = service.training_dataset_workflow(broad.id)["component_requirements"]
    assert "modality-specific-evidence-preservation" in {
        item["requirement_id"] for item in requirements["requirements"]
    }
    unchanged = service.get_build(fixed.id)
    assert unchanged.version == fixed_waiting.version
    assert unchanged.current_stage is fixed_waiting.current_stage
    assert service.agent_runs(fixed.id) == []


def test_reviewed_source_authorization_is_explicit_optimistic_and_idempotent(
    workflow_runtime,
) -> None:
    _database, _store, _providers, _harness, service = workflow_runtime
    service.agent_configuration = AgentConfiguration(
        provider="openai",
        worker_provider="openai",
        planner_provider="openai",
        run_mode=AgentRunMode.LIVE,
        api_key=SecretStr("test-placeholder-never-read"),
        retry_count=0,
    )
    service.reviewed_source_adapters = ReviewedSourceAdapterRegistry(
        ReviewedSourceAdapter(definition, object(), object(), object())  # type: ignore[arg-type]
        for definition in REVIEWED_ADAPTER_DEFINITIONS
    )
    build = service.create_build(
        EndpointBuildCreate(
            endpoint_name="X receptor antagonist",
            endpoint_slug="reviewed-authorization",
            biological_goal=(
                "Construct a compound-level public training dataset with transcriptomic responses."
            ),
            created_by="test-admin",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            benchmark_mode="blind_training_dataset_discovery",
            idempotency_key="reviewed-source-authorization",
        )
    )
    waiting = service.start_build(
        build.id,
        expected_version=0,
        actor="test-admin",
        idempotency_key="reviewed-source-authorization:start",
    )
    approval = service.list_approvals(build.id, pending_only=True)[0]
    approved = service.decide_approval(
        approval["id"],
        ApprovalDecision(
            decision=ApprovalDecisionValue.APPROVE,
            reviewer_id="test-admin",
            expected_version=waiting.version,
            idempotency_key="reviewed-source-authorization:approve",
            artifact_hashes=approval["request"]["artifact_hashes"],
            dataset_specification_policy=approved_specification_policy(),
        ),
    )
    service.derive_training_dataset_requirements(
        build.id,
        actor="deterministic-orchestrator",
        idempotency_key="reviewed-source-authorization:requirements",
    )
    readiness = service.source_discovery_readiness(build.id)
    assert readiness["ready"] is True
    assert readiness["provider_retries"] == 0
    assert readiness["reviewed_adapters"]["ready"] is True
    assert readiness["reviewed_adapters"]["epa_authenticated_api"]["status"] == (
        "authenticated_api_unavailable"
    )
    assert readiness["reviewed_adapters"]["epa_public_data_releases"]["status"] == "ready"
    assert readiness["epa_authenticated_api"]["status"] == "authenticated_api_unavailable"
    assert readiness["epa_public_data_releases"]["status"] == "ready"
    assert readiness["lincs_public_releases"]["status"] == "ready"
    activity_agent = next(
        item
        for item in service.training_dataset_workflow(build.id)["planned_discovery_agents"]
        if item["agent_name"] == "Activity Evidence Discovery Agent"
    )
    assert "inspect_epa_public_invitrodb_release" in activity_agent["allowed_tools"]
    assert "search_epa_assays" not in activity_agent["allowed_tools"]

    with pytest.raises(StaleWorkflowVersion):
        service.authorize_source_discovery(
            build.id,
            expected_version=approved.version - 1,
            actor="test-admin",
            idempotency_key="reviewed-source-authorization:stale",
            confirmed=True,
        )
    authorized = service.authorize_source_discovery(
        build.id,
        expected_version=approved.version,
        actor="test-admin-utf8-é",
        idempotency_key="reviewed-source-authorization:authorize",
        confirmed=True,
    )
    repeated = service.authorize_source_discovery(
        build.id,
        expected_version=approved.version,
        actor="test-admin-utf8-é",
        idempotency_key="reviewed-source-authorization:authorize",
        confirmed=True,
    )
    with pytest.raises(WorkflowConflict, match="already authorized"):
        service.authorize_source_discovery(
            build.id,
            expected_version=authorized.version,
            actor="test-admin",
            idempotency_key="reviewed-source-authorization:different-click",
            confirmed=True,
        )
    assert authorized.current_stage is WorkflowState.DISCOVERING_ACTIVITY_EVIDENCE
    assert repeated.version == authorized.version
    assert service.agent_runs(build.id) == []


def test_authorized_four_role_orchestration_is_offline_bounded_and_restart_safe(
    workflow_runtime,
) -> None:
    database, store, _providers, _harness, service = workflow_runtime
    calls: list[tuple[str, int]] = []
    source_requests: list[str] = []

    class OfflineRoleProvider:
        name = "openai"

        def run_turn(self, request, history, *, interruption_requested):
            assert interruption_requested is False
            calls.append((request.agent_name, len(history)))
            if request.agent_name == "Activity Evidence Discovery Agent" and len(history) < 3:
                modality = ("binding", "agonism", "antagonism")[len(history)]
                return ProviderTurn(
                    kind="tool",
                    tool_request={
                        "tool_name": "search_activity_sources",
                        "arguments": {
                            "source_system": "pubchem-bioassay",
                            "query": f"example receptor {modality}",
                            "biological_target": "Example receptor",
                            "endpoint_modality": modality,
                            "maximum_results": 2,
                        },
                        "idempotency_key": f"offline-search-activity-{modality}",
                    },
                    usage=UsageReport(
                        input_tokens=10,
                        output_tokens=2,
                        cached_tokens=0,
                        cost_cents=0.001,
                        provider_invocations=1,
                    ),
                )
            if request.agent_name == "Transcriptomic Evidence Discovery Agent" and len(history) < 2:
                operation = (
                    "search_transcriptomic_sources" if not history else "search_lincs_resources"
                )
                return ProviderTurn(
                    kind="tool",
                    tool_request={
                        "tool_name": operation,
                        "arguments": {
                            "query": "example chemical perturbation",
                            "maximum_results": 2,
                        },
                        "idempotency_key": f"offline-{operation}",
                    },
                    usage=UsageReport(
                        input_tokens=10,
                        output_tokens=2,
                        cached_tokens=0,
                        cost_cents=0.001,
                        provider_invocations=1,
                    ),
                )
            if not history:
                operation, arguments = {
                    "Chemical Identity and Structure Source Discovery Agent": (
                        "resolve_compound_identity_sample",
                        {
                            "sampled_identifiers": ["123"],
                            "identifier_type": "cid",
                            "maximum_results": 2,
                        },
                    ),
                    "Supporting Metadata Discovery Agent": (
                        "inspect_supporting_metadata",
                        {"source_identifiers": ["1001"], "maximum_results": 2},
                    ),
                }[request.agent_name]
                return ProviderTurn(
                    kind="tool",
                    tool_request={
                        "tool_name": operation,
                        "arguments": arguments,
                        "idempotency_key": f"offline-{operation}",
                    },
                    usage=UsageReport(
                        input_tokens=10,
                        output_tokens=2,
                        cached_tokens=0,
                        cost_cents=0.001,
                        provider_invocations=1,
                    ),
                )
            return ProviderTurn(
                kind="output",
                output={
                    "schema_version": "1.0.0",
                    "status": (
                        "invalid_model_output"
                        if request.agent_name == "Transcriptomic Evidence Discovery Agent"
                        else "completed"
                    ),
                    "relevance_assessments": [],
                    "ranked_observation_ids": [],
                    "modality_fit_explanations": [],
                    "unresolved_scientific_concerns": [],
                    "recommended_follow_up_inspections": [],
                    "safe_summary": "Offline typed review fixture completed.",
                },
                usage=UsageReport(
                    input_tokens=10,
                    output_tokens=5,
                    cached_tokens=0,
                    cost_cents=0.001,
                    provider_invocations=1,
                ),
            )

    def official_fixture(request: httpx.Request) -> httpx.Response:
        source_requests.append(request.url.host or "")
        if request.url.host == "pubchem.ncbi.nlm.nih.gov":
            if "/rest/pug/assay/aid/" in request.url.path:
                if request.url.path.endswith("/CSV"):
                    return httpx.Response(
                        200,
                        text=(
                            "PUBCHEM_RESULT_TAG,PUBCHEM_SID,PUBCHEM_CID,"
                            "PUBCHEM_ACTIVITY_OUTCOME,AC50 [uM]\n"
                            "1,70001,123,Active,0.5\n"
                        ),
                        headers={"content-type": "text/csv"},
                        request=request,
                    )
                return httpx.Response(
                    200,
                    json={"IdentifierList": {"CID": [123]}},
                    request=request,
                )
            if request.url.path.endswith("/synonyms/JSON"):
                return httpx.Response(
                    200,
                    json={
                        "InformationList": {
                            "Information": [
                                {
                                    "CID": 123,
                                    "Synonym": ["Example compound", "Fixture compound"],
                                }
                            ]
                        }
                    },
                    request=request,
                )
            return httpx.Response(
                200,
                json={
                    "PropertyTable": {
                        "Properties": [
                            {
                                "CID": 123,
                                "Title": "Example compound",
                                "CanonicalSMILES": "CCO",
                                "IsomericSMILES": "CCO",
                                "InChIKey": "AAAAAAAAAAAAAA-BBBBBBBBBB-C",
                            }
                        ]
                    }
                },
                request=request,
            )
        database_name = request.url.params.get("db")
        if request.url.path.endswith("esearch.fcgi"):
            query = str(request.url.params.get("term", "")).casefold()
            if database_name == "pcassay":
                identifier = (
                    "1001"
                    if "binding" in query
                    else "1003"
                    if "antagonism" in query
                    else "1002"
                )
            else:
                identifier = "2001"
            return httpx.Response(
                200,
                json={"esearchresult": {"idlist": [identifier]}},
                request=request,
            )
        if request.url.path.endswith("esummary.fcgi") and database_name == "pcassay":
            identifier = str(request.url.params["id"])
            return httpx.Response(
                200,
                json={
                    "result": {
                        "uids": [identifier],
                        identifier: {
                            "title": "Example receptor assay",
                            "targetname": "Example receptor",
                            "assaytype": "functional assay",
                        },
                    }
                },
                request=request,
            )
        if request.url.path.endswith("esummary.fcgi") and database_name == "gds":
            identifier = str(request.url.params["id"])
            return httpx.Response(
                200,
                json={
                    "result": {
                        "uids": [identifier],
                        identifier: {
                            "accession": f"GSE{identifier}",
                            "title": "Example compound perturbation study",
                            "summary": (
                                "Example compound treatment at 1 uM for 24 hours with "
                                "matched controls."
                            ),
                            "taxon": "Homo sapiens",
                            "gdsType": "HepG2 cells",
                            "suppfile": "processed.tsv.gz",
                            "ftpLink": "https://ftp.ncbi.nlm.nih.gov/geo/example",
                        },
                    }
                },
                request=request,
            )
        return httpx.Response(200, json={"linksets": []}, request=request)

    source_client = ScientificSourceClient(
        transport=httpx.MockTransport(official_fixture),
        maximum_attempts=1,
        sleep=lambda _seconds: None,
    )
    source_cache = SourceResponseCache(database)
    reviewed = production_reviewed_source_registry(
        client=source_client,
        cache=source_cache,
        artifacts=store,
    )
    discovery_tools = DiscoveryToolService(source_cache, store, source_client)
    tools = phase1_tool_registry(service.repo_root, discovery_tools, reviewed)
    providers = ProviderRegistry()
    providers.register("openai", OfflineRoleProvider)
    service.harness = AgentHarness(database, providers, tools)
    service.reviewed_source_adapters = reviewed
    service.agent_configuration = AgentConfiguration(
        provider="openai",
        worker_provider="openai",
        planner_provider="openai",
        run_mode=AgentRunMode.LIVE,
        api_key=SecretStr("test-placeholder-never-read"),
        retry_count=0,
    )

    build = service.create_build(
        EndpointBuildCreate(
            endpoint_name="Receptor X binding, agonism, and antagonism",
            endpoint_slug="offline-four-role-orchestration",
            biological_goal=(
                "Construct a compound-level public training dataset with transcriptomic responses."
            ),
            created_by="test-admin",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            benchmark_mode="blind_training_dataset_discovery",
            idempotency_key="offline-four-role-orchestration",
        )
    )
    waiting = service.start_build(
        build.id,
        expected_version=0,
        actor="test-admin",
        idempotency_key="offline-four-role-orchestration:start",
    )
    approval = service.list_approvals(build.id, pending_only=True)[0]
    approved = service.decide_approval(
        approval["id"],
        ApprovalDecision(
            decision=ApprovalDecisionValue.APPROVE,
            reviewer_id="test-admin",
            expected_version=waiting.version,
            idempotency_key="offline-four-role-orchestration:approve",
            artifact_hashes=approval["request"]["artifact_hashes"],
            dataset_specification_policy=approved_specification_policy(),
        ),
    )
    service.derive_training_dataset_requirements(
        build.id,
        actor="deterministic-orchestrator",
        idempotency_key="offline-four-role-orchestration:requirements",
    )
    authorized = service.authorize_source_discovery(
        build.id,
        expected_version=approved.version,
        actor="test-admin",
        idempotency_key="offline-four-role-orchestration:authorize",
        confirmed=True,
    )
    completed = service.run_authorized_source_discovery(build.id)
    assert completed.current_stage is WorkflowState.AWAITING_SOURCE_INVENTORY_REVIEW
    assert completed.status.value == "waiting"
    assert len(service.agent_runs(build.id)) == 4
    assert len(calls) == 7
    assert {agent for agent, _history in calls} == {
        "Activity Evidence Discovery Agent",
        "Transcriptomic Evidence Discovery Agent",
        "Chemical Identity and Structure Source Discovery Agent",
        "Supporting Metadata Discovery Agent",
    }
    assert all(
        service.agent_run(run["id"])["request"]["budget"]["retry_count"] == 0
        for run in service.agent_runs(build.id)
    )
    workflow = service.training_dataset_workflow(build.id)
    assert len(workflow["source_observations"]) >= 7
    assert len(workflow["source_search_outcomes"]) == 5
    assert len(workflow["source_fragments"]) == 4
    transcript_fragment = next(
        item["fragment"]
        for item in workflow["source_fragments"]
        if item["agent_name"] == "Transcriptomic Evidence Discovery Agent"
    )
    assert transcript_fragment["agent_review_status"] == "unavailable"
    assert transcript_fragment["agent_terminal_outcome"] == "invalid_model_output"
    assert any(
        item["fragment"]["candidate_records"]
        for item in workflow["source_fragments"]
        if item["agent_name"] == "Activity Evidence Discovery Agent"
    )
    assert workflow["verified_source_inventory"] is not None
    assert workflow["capability_matrix"]["field_cells"]
    assert workflow["gap_report"]["maximum_discovery_rounds"] == 0
    assert workflow["assembly_strategies"] is None
    assert set(source_requests) <= {
        "eutils.ncbi.nlm.nih.gov",
        "pubchem.ncbi.nlm.nih.gov",
    }

    before = (len(calls), len(source_requests), len(service.agent_runs(build.id)))
    service._assert_source_discovery_budget(build.id, "Supporting Metadata Discovery Agent")
    with pytest.raises(GuardNotSatisfied, match="four-run"):
        service._assert_source_discovery_budget(build.id, "Unscheduled Fifth Agent")
    repeated = service.run_authorized_source_discovery(build.id)
    assert repeated.version == completed.version
    assert (len(calls), len(source_requests), len(service.agent_runs(build.id))) == before
    assert authorized.current_stage is WorkflowState.DISCOVERING_ACTIVITY_EVIDENCE
    source_client.close()


def test_training_dataset_specification_recovery_requires_explicit_idempotent_continue(
    workflow_runtime,
) -> None:
    _database, store, _providers, _harness, service = workflow_runtime
    build = service.create_build(
        EndpointBuildCreate(
            endpoint_name="X receptor antagonist",
            endpoint_slug="example-functional-endpoint-recovery",
            biological_goal=(
                "Construct a compound-level public training dataset with transcriptomic responses."
            ),
            created_by="test-admin",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            benchmark_mode="blind_training_dataset_discovery",
            idempotency_key="training-spec-recovery",
        )
    )
    artifacts = store.list_artifacts(build.id)
    active = service.transition(
        build.id,
        TransitionRequest(
            target_state=WorkflowState.COMPILING_TARGET_DATASET_SPECIFICATION,
            expected_version=0,
            idempotency_key="training-spec-recovery:specifying",
            initiator=ActorType.HUMAN,
            initiator_id="test-admin",
            reason="Prepared interrupted pre-agent state.",
            artifact_hashes=[item.sha256 for item in artifacts],
        ),
    )
    assert service.agent_runs(build.id) == []

    waiting = service.continue_training_dataset_workflow(
        build.id,
        expected_version=active.version,
        actor="test-admin",
        idempotency_key="training-spec-recovery:continue",
    )
    repeated = service.continue_training_dataset_workflow(
        build.id,
        expected_version=active.version,
        actor="test-admin",
        idempotency_key="training-spec-recovery:continue",
    )

    assert waiting.current_stage is WorkflowState.AWAITING_DATASET_SPECIFICATION_REVIEW
    assert repeated.current_stage is WorkflowState.AWAITING_DATASET_SPECIFICATION_REVIEW
    assert service.agent_runs(build.id) == []


def test_deterministic_compilation_never_invokes_configured_provider(
    workflow_runtime,
) -> None:
    _database, _store, providers, _harness, service = workflow_runtime

    class CountingFailureProvider:
        name = "fake"

        def __init__(self) -> None:
            self.calls = 0

        def run_turn(self, *_args, **_kwargs):
            self.calls += 1
            raise ProviderFailure("Prepared terminal failure.", retryable=False)

    provider = CountingFailureProvider()
    providers._providers["fake"] = lambda: provider
    build = service.create_build(
        EndpointBuildCreate(
            endpoint_name="X receptor antagonist",
            endpoint_slug="example-functional-endpoint-failure",
            biological_goal=(
                "Construct a compound-level source-neutral training plan with transcriptomics."
            ),
            created_by="test-admin",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            benchmark_mode="blind_training_dataset_discovery",
            idempotency_key="training-spec-provider-failure",
        )
    )

    waiting = service.start_build(
        build.id,
        expected_version=0,
        actor="test-admin",
        idempotency_key="training-spec-provider-failure:start",
    )

    assert waiting.current_stage is WorkflowState.AWAITING_DATASET_SPECIFICATION_REVIEW
    assert provider.calls == 0
    assert service.agent_runs(build.id) == []


@pytest.mark.parametrize(
    "invalid_output",
    [
        None,
        {},
        {"schema_version": "1.0.0", "status": "completed"},
        {"schema_version": "1.0.0", "status": "wrong_enum", "extra": True},
    ],
)
def test_invalid_optional_review_preserves_draft_without_discovery_or_retry(
    workflow_runtime,
    invalid_output,
) -> None:
    database, store, providers, _harness, service = workflow_runtime

    class InvalidOutputProvider:
        name = "fake"

        def __init__(self) -> None:
            self.calls = 0

        def run_turn(self, *_args, **_kwargs):
            self.calls += 1
            return ProviderTurn(
                kind="output",
                output=invalid_output,
                usage=UsageReport(
                    usage_status="usage_recorded",
                    input_tokens=100,
                    output_tokens=20,
                    provider_request_ids=["req-offline-fixture"],
                    provider_invocations=1,
                ),
            )

    provider = InvalidOutputProvider()
    providers._providers["fake"] = lambda: provider
    build = service.create_build(
        EndpointBuildCreate(
            endpoint_name="X receptor antagonist",
            endpoint_slug=f"invalid-spec-{len(str(invalid_output))}",
            biological_goal=(
                "Construct a compound-level source-neutral training plan with transcriptomics."
            ),
            created_by="test-admin",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            benchmark_mode="blind_training_dataset_discovery",
            idempotency_key=f"invalid-spec-{len(str(invalid_output))}",
        )
    )
    compiled = service.start_build(
        build.id,
        expected_version=0,
        actor="test-admin",
        idempotency_key="invalid-spec-start",
    )
    waiting = service.run_optional_dataset_specification_review(
        build.id,
        expected_version=compiled.version,
        actor="test-admin",
        idempotency_key="invalid-spec-review",
    )
    assert waiting.current_stage is WorkflowState.AWAITING_DATASET_SPECIFICATION_REVIEW
    assert provider.calls == 1
    assert len(service.agent_runs(build.id)) == 1
    workflow = service.training_dataset_workflow(build.id)
    assert workflow["specification_draft"] is not None
    assert workflow["specification_review"]["status"] == "invalid_output"
    assert workflow["verified_source_inventory"] is None
    assert workflow["assembly_strategies"] is None
    artifact_types = {item.artifact_type for item in store.list_artifacts(build.id)}
    assert "dataset_specification_compilation_outcome" in artifact_types
    assert "specialized_agent_trace" in artifact_types
    assert "verified_source_inventory" not in artifact_types
    database.dispose()
    database.migrate()
    restored = service.get_build(build.id)
    assert restored.current_stage is WorkflowState.AWAITING_DATASET_SPECIFICATION_REVIEW


@pytest.mark.parametrize(
    ("terminal", "expected_status"), [("refusal", "refused"), ("timeout", "unavailable")]
)
def test_optional_reviewer_refusal_or_timeout_preserves_draft(
    workflow_runtime,
    terminal: str,
    expected_status: str,
) -> None:
    _database, _store, providers, _harness, service = workflow_runtime

    class TerminalReviewProvider:
        name = "fake"

        def __init__(self) -> None:
            self.calls = 0

        def run_turn(self, *_args, **_kwargs):
            self.calls += 1
            if terminal == "timeout":
                raise ProviderTimeout("Prepared offline timeout.")
            return ProviderTurn(
                kind="output",
                output={
                    "schema_version": "1.0.0",
                    "status": "model_refused",
                    "review_summary": (
                        "Optional AI review was refused; the deterministic draft is unchanged."
                    ),
                    "blocking_findings": [],
                    "approval_questions_to_add": [],
                    "suggested_field_corrections": [],
                    "scientific_consistency_flags": [],
                    "requires_human_review": True,
                },
                usage=UsageReport(provider_invocations=1),
            )

    provider = TerminalReviewProvider()
    providers._providers["fake"] = lambda: provider
    build = service.create_build(
        EndpointBuildCreate(
            endpoint_name="X receptor antagonist",
            endpoint_slug=f"review-{terminal}",
            biological_goal=(
                "Construct a compound-level source-neutral training plan with transcriptomics."
            ),
            created_by="test-admin",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            benchmark_mode="blind_training_dataset_discovery",
            idempotency_key=f"review-{terminal}",
        )
    )
    compiled = service.start_build(
        build.id,
        expected_version=0,
        actor="test-admin",
        idempotency_key=f"review-{terminal}:compile",
    )
    reviewed = service.run_optional_dataset_specification_review(
        build.id,
        expected_version=compiled.version,
        actor="test-admin",
        idempotency_key=f"review-{terminal}:run",
    )
    workflow = service.training_dataset_workflow(build.id)
    assert reviewed.current_stage is WorkflowState.AWAITING_DATASET_SPECIFICATION_REVIEW
    assert provider.calls == 1
    assert workflow["specification_review"]["status"] == expected_status
    assert workflow["specification_draft"] is not None
    assert workflow["verified_source_inventory"] is None
    assert service.list_approvals(build.id, pending_only=True)


def test_optional_reviewer_blocking_contradiction_preserves_compiled_draft(
    workflow_runtime,
) -> None:
    _database, store, providers, _harness, service = workflow_runtime

    class BlockingReviewProvider:
        name = "fake"

        def __init__(self) -> None:
            self.calls = 0

        def run_turn(self, *_args, **_kwargs):
            self.calls += 1
            return ProviderTurn(
                kind="output",
                output={
                    "schema_version": "1.0.0",
                    "status": "blocking_issue_found",
                    "review_summary": "The requested modality is contradictory.",
                    "blocking_findings": ["The draft contains mutually exclusive modalities."],
                    "approval_questions_to_add": [],
                    "suggested_field_corrections": [],
                    "scientific_consistency_flags": ["contradictory modality"],
                    "requires_human_review": True,
                },
                usage=UsageReport(
                    usage_status="usage_recorded",
                    input_tokens=100,
                    output_tokens=20,
                    provider_request_ids=["req-offline-semantic-fixture"],
                    provider_invocations=1,
                ),
            )

    provider = BlockingReviewProvider()
    providers._providers["fake"] = lambda: provider
    build = service.create_build(
        EndpointBuildCreate(
            endpoint_name="X receptor antagonist",
            endpoint_slug="x-receptor-antagonist-semantic-policy",
            biological_goal=(
                "Construct a compound-level prediction dataset with transcriptomic responses."
            ),
            created_by="test-admin",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            benchmark_mode="blind_training_dataset_discovery",
            idempotency_key="semantic-policy-mismatch",
        )
    )
    compiled = service.start_build(
        build.id,
        expected_version=0,
        actor="test-admin",
        idempotency_key="semantic-policy-mismatch-start",
    )
    waiting = service.run_optional_dataset_specification_review(
        build.id,
        expected_version=compiled.version,
        actor="test-admin",
        idempotency_key="semantic-policy-review",
    )
    assert waiting.current_stage is WorkflowState.AWAITING_DATASET_SPECIFICATION_REVISION
    assert provider.calls == 1
    assert len(service.agent_runs(build.id)) == 1
    workflow = service.training_dataset_workflow(build.id)
    assert workflow["specification_draft"] is not None
    assert workflow["verified_source_inventory"] is None
    assert workflow["assembly_strategies"] is None
    assert workflow["specification_review"]["status"] == "blocking_issue_found"
    artifact_types = {item.artifact_type for item in store.list_artifacts(build.id)}
    assert "dataset_specification_review_outcome" in artifact_types
    assert "training_dataset_specification_draft" in artifact_types
    assert not service.list_approvals(build.id, pending_only=True)


def test_specification_revision_requires_explicit_human_action(workflow_runtime) -> None:
    _database, _store, providers, _harness, service = workflow_runtime

    class InvalidOutputProvider:
        name = "fake"

        def run_turn(self, *_args, **_kwargs):
            return ProviderTurn(kind="output", output={"unexpected": True})

    providers._providers["fake"] = InvalidOutputProvider
    build = service.create_build(
        EndpointBuildCreate(
            endpoint_name="toxicity",
            endpoint_slug="explicit-spec-revision",
            biological_goal="Construct a compound-level transcriptomic training plan.",
            created_by="test-admin",
            workflow_kind=WorkflowKind.TRAINING_DATASET_DISCOVERY,
            benchmark_mode="blind_training_dataset_discovery",
            idempotency_key="explicit-spec-revision",
        )
    )
    waiting = service.start_build(
        build.id,
        expected_version=0,
        actor="test-admin",
        idempotency_key="explicit-spec-revision:start",
    )
    assert waiting.current_stage is WorkflowState.AWAITING_DATASET_SPECIFICATION_REVISION
    assert service.agent_runs(build.id) == []
    draft = service.revise_training_dataset_request(
        build.id,
        expected_version=waiting.version,
        actor="test-admin",
        idempotency_key="explicit-spec-revision:revise",
    )
    assert draft.current_stage is WorkflowState.DRAFT
    assert service.agent_runs(build.id) == []


def test_create_is_idempotent_and_deterministic(workflow_runtime) -> None:
    _database, _store, _providers, _harness, service = workflow_runtime
    first = create_build(service)
    second = create_build(service)
    assert first.id == second.id
    assert first.version == 0
    assert first.current_stage is WorkflowState.DRAFT
    assert len(service.timeline(first.id)) == 4


def test_creation_idempotency_rejects_different_payload(workflow_runtime) -> None:
    _database, _store, _providers, _harness, service = workflow_runtime
    create_build(service)
    with pytest.raises(WorkflowConflict):
        service.create_build(
            EndpointBuildCreate(
                endpoint_name="Different endpoint",
                endpoint_slug="different-endpoint",
                biological_goal="A different biological goal that must not reuse the key.",
                created_by="test-admin",
                idempotency_key="phase0-test-create",
            )
        )


def test_discovery_reaches_persisted_dataset_approval(workflow_runtime) -> None:
    _database, store, _providers, _harness, service = workflow_runtime
    started = start_build(service, create_build(service))
    assert started.current_stage is WorkflowState.AWAITING_DATASET_APPROVAL
    assert started.pending_approval_id
    assert {item.artifact_type for item in store.list_artifacts(started.id)} == {
        "endpoint_definition",
        "dataset_candidates",
        "search_strategy",
        "agent_recommendation",
        "search_trace",
    }
    assert service.agent_runs(started.id)[0]["provider"] == "fake"


def test_invalid_transition_rolls_back_without_event(workflow_runtime) -> None:
    _database, _store, _providers, _harness, service = workflow_runtime
    build = create_build(service)
    count = len(service.timeline(build.id))
    with pytest.raises(InvalidTransition):
        service.resume(
            build.id,
            expected_version=build.version,
            actor="test-admin",
            idempotency_key="invalid-resume-from-draft",
        )
    assert len(service.timeline(build.id)) == count
    assert service.get_build(build.id).version == 0


def test_optimistic_lock_rejects_stale_version(workflow_runtime) -> None:
    _database, _store, _providers, _harness, service = workflow_runtime
    waiting = start_build(service, create_build(service))
    paused = service.pause(
        waiting.id,
        expected_version=waiting.version,
        actor="test-admin",
        idempotency_key="pause-once",
    )
    assert paused.current_stage is WorkflowState.PAUSED
    with pytest.raises(StaleWorkflowVersion):
        service.cancel(
            waiting.id,
            expected_version=waiting.version,
            actor="other-admin",
            idempotency_key="stale-cancel",
        )


def test_concurrent_transition_has_one_winner(workflow_runtime) -> None:
    _database, _store, _providers, _harness, service = workflow_runtime
    waiting = start_build(service, create_build(service))

    def pause(key):
        try:
            return service.pause(
                waiting.id,
                expected_version=waiting.version,
                actor="test-admin",
                idempotency_key=key,
            ).current_stage
        except (StaleWorkflowVersion, InvalidTransition):
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(pause, ["concurrent-a", "concurrent-b"]))
    assert outcomes.count(WorkflowState.PAUSED) == 1
    assert outcomes.count("conflict") == 1


def test_pause_resume_preserves_underlying_stage(workflow_runtime) -> None:
    _database, _store, _providers, _harness, service = workflow_runtime
    waiting = start_build(service, create_build(service))
    paused = service.pause(
        waiting.id,
        expected_version=waiting.version,
        actor="test-admin",
        idempotency_key="pause-stage",
    )
    assert paused.paused_from_state is WorkflowState.AWAITING_DATASET_APPROVAL
    resumed = service.resume(
        waiting.id,
        expected_version=paused.version,
        actor="test-admin",
        idempotency_key="resume-stage",
    )
    assert resumed.current_stage is WorkflowState.AWAITING_DATASET_APPROVAL
    assert resumed.paused_from_state is None


def test_cancel_is_terminal(workflow_runtime) -> None:
    _database, _store, _providers, _harness, service = workflow_runtime
    waiting = start_build(service, create_build(service))
    cancelled = service.cancel(
        waiting.id,
        expected_version=waiting.version,
        actor="test-admin",
        idempotency_key="cancel-terminal",
    )
    assert cancelled.current_stage is WorkflowState.CANCELLED
    with pytest.raises(InvalidTransition):
        service.resume(
            waiting.id,
            expected_version=cancelled.version,
            actor="test-admin",
            idempotency_key="resume-cancelled",
        )


def test_approval_hash_binding_and_immutable_decision(workflow_runtime) -> None:
    _database, _store, _providers, _harness, service = workflow_runtime
    waiting = start_build(service, create_build(service))
    approval = service.get_approval(waiting.pending_approval_id)
    with pytest.raises(GuardNotSatisfied):
        service.decide_approval(
            approval["id"],
            ApprovalDecision(
                decision=ApprovalDecisionValue.APPROVE,
                reviewer_id="scientist",
                expected_version=waiting.version,
                idempotency_key="bad-approval-hash",
                artifact_hashes=["0" * 64],
            ),
        )
    approved = service.decide_approval(
        approval["id"],
        ApprovalDecision(
            decision=ApprovalDecisionValue.APPROVE,
            reviewer_id="scientist",
            expected_version=waiting.version,
            idempotency_key="good-approval-hash",
            artifact_hashes=approval["request"]["artifact_hashes"],
        ),
    )
    assert approved.current_stage is WorkflowState.CURATING_DATA
    with pytest.raises(WorkflowConflict):
        service.decide_approval(
            approval["id"],
            ApprovalDecision(
                decision=ApprovalDecisionValue.REJECT,
                reviewer_id="scientist",
                reviewer_comment="Cannot rewrite the prior decision.",
                expected_version=approved.version,
                idempotency_key="rewrite-approval",
                artifact_hashes=approval["request"]["artifact_hashes"],
            ),
        )


def test_revision_creates_new_proposal_and_replays_tools(workflow_runtime) -> None:
    _database, _store, _providers, _harness, service = workflow_runtime
    waiting = start_build(service, create_build(service))
    approval = service.get_approval(waiting.pending_approval_id)
    revised = service.decide_approval(
        approval["id"],
        ApprovalDecision(
            decision=ApprovalDecisionValue.REQUEST_REVISION,
            reviewer_id="scientist",
            reviewer_comment="Create a new prepared comparison revision.",
            expected_version=waiting.version,
            idempotency_key="revision-decision",
            artifact_hashes=approval["request"]["artifact_hashes"],
        ),
    )
    rerun = service.run_discovery(
        revised.id,
        expected_version=revised.version,
        actor="scientist",
        idempotency_key="revision-discovery",
    )
    assert rerun.pending_approval_id != approval["id"]
    assert len(service.agent_runs(rerun.id)) == 2


def test_immutable_event_trigger_rejects_update(workflow_runtime) -> None:
    database, _store, _providers, _harness, service = workflow_runtime
    build = create_build(service)
    with pytest.raises(DatabaseError):
        with database.session() as session:
            event_id = session.scalar(
                select(WorkflowEventRow.id).where(WorkflowEventRow.workflow_id == build.id)
            )
            session.execute(
                update(WorkflowEventRow)
                .where(WorkflowEventRow.id == event_id)
                .values(event_type="tampered")
            )


def test_transaction_rollback_removes_database_mutation(workflow_runtime) -> None:
    database, _store, _providers, _harness, service = workflow_runtime
    build = create_build(service)
    with pytest.raises(RuntimeError):
        with database.session() as session:
            row = session.get(EndpointBuildRow, build.id)
            row.endpoint_name = "Should roll back"
            raise RuntimeError("rollback")
    assert service.get_build(build.id).endpoint_name == "Oxidative stress"


def test_artifact_integrity_and_size_limit(workflow_runtime) -> None:
    _database, store, _providers, _harness, service = workflow_runtime
    build = create_build(service)
    artifact = store.put_bytes(
        workflow_id=build.id,
        content=b"bounded",
        mime_type="text/plain",
        artifact_type="test",
        logical_name="bounded.txt",
        producer="test",
        idempotency_key="bounded-artifact",
    )
    assert store.verify(artifact.id)
    store._path(artifact.sha256).write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        store.get(artifact.id)
    with pytest.raises(ArtifactTooLarge):
        store.put_bytes(
            workflow_id=build.id,
            content=b"x" * (1024 * 1024 + 1),
            mime_type="application/octet-stream",
            artifact_type="test",
            logical_name="too-large.bin",
            producer="test",
            idempotency_key="oversized-artifact",
        )


def test_artifact_ids_do_not_accept_path_traversal(workflow_runtime) -> None:
    _database, store, _providers, _harness, _service = workflow_runtime
    with pytest.raises(WorkflowNotFound):
        store.get("../../registry/models/endpoints.json")


def test_restart_recovery_marks_running_step_retryable(workflow_runtime) -> None:
    _database, _store, _providers, _harness, service = workflow_runtime
    waiting = start_build(service, create_build(service))
    approval = service.get_approval(waiting.pending_approval_id)
    curating = service.decide_approval(
        approval["id"],
        ApprovalDecision(
            decision=ApprovalDecisionValue.APPROVE,
            reviewer_id="scientist",
            expected_version=waiting.version,
            idempotency_key="recovery-approval",
            artifact_hashes=approval["request"]["artifact_hashes"],
        ),
    )
    service.create_step(
        curating.id,
        WorkflowState.CURATING_DATA,
        idempotency_key="interrupted-curation",
        input_payload={"prepared": True},
    )
    assert service.recover_interrupted() == 1
    recovered = service.get_build(curating.id)
    assert recovered.current_stage is WorkflowState.FAILED
    assert recovered.failed_from_state is WorkflowState.CURATING_DATA
    assert service.steps(curating.id)[-1]["status"] == "interrupted"
    assert service.timeline(curating.id)[-2]["event_type"] == "workflow.step.interrupted"
    assert service.list_approvals(curating.id)[-1]["status"] == "approved"


def test_controlled_failure_retry_does_not_duplicate_discovery(workflow_runtime) -> None:
    _database, _store, _providers, _harness, service = workflow_runtime
    waiting = start_build(service, create_build(service))
    run_count = len(service.agent_runs(waiting.id))
    failed = service.simulate_failure(
        waiting.id,
        expected_version=waiting.version,
        actor="test-admin",
        idempotency_key="controlled-failure",
    )
    retried = service.retry_failed(
        waiting.id,
        expected_version=failed.version,
        actor="test-admin",
        idempotency_key="controlled-retry",
    )
    assert retried.current_stage is WorkflowState.AWAITING_DATASET_APPROVAL
    assert len(service.agent_runs(waiting.id)) == run_count


def test_new_step_attempt_explicitly_supersedes_only_active_attempt(workflow_runtime) -> None:
    _database, _store, _providers, _harness, service = workflow_runtime
    waiting = start_build(service, create_build(service))
    approval = service.get_approval(waiting.pending_approval_id)
    curating = service.decide_approval(
        approval["id"],
        ApprovalDecision(
            decision=ApprovalDecisionValue.APPROVE,
            reviewer_id="scientist",
            expected_version=waiting.version,
            idempotency_key="supersede-approval",
            artifact_hashes=approval["request"]["artifact_hashes"],
        ),
    )
    first = service.create_step(
        curating.id,
        WorkflowState.CURATING_DATA,
        idempotency_key="curation-attempt-one",
        input_payload={"attempt": 1},
    )
    second = service.create_step(
        curating.id,
        WorkflowState.CURATING_DATA,
        idempotency_key="curation-attempt-two",
        input_payload={"attempt": 2},
    )
    steps = service.steps(curating.id)
    assert first.id != second.id
    assert [step["status"] for step in steps[-2:]] == ["interrupted", "running"]
    assert sum(step["status"] == "running" for step in steps) == 1
    assert any(
        event["event_type"] == "workflow.step.superseded"
        and event["payload"]["step_id"] == first.id
        for event in service.timeline(curating.id)
    )


def test_restart_repairs_stale_attempt_on_already_failed_workflow(workflow_runtime) -> None:
    database, _store, _providers, _harness, service = workflow_runtime
    waiting = start_build(service, create_build(service))
    failed = service.simulate_failure(
        waiting.id,
        expected_version=waiting.version,
        actor="test-admin",
        idempotency_key="persisted-failure",
    )
    stale = service.create_step(
        waiting.id,
        WorkflowState.DISCOVERING_DATA,
        idempotency_key="persisted-stale-attempt",
        input_payload={"persisted": True},
    )
    with database.session() as session:
        error = session.scalar(
            select(WorkflowErrorRow)
            .where(WorkflowErrorRow.workflow_id == waiting.id)
            .order_by(WorkflowErrorRow.created_at.desc(), WorkflowErrorRow.id.desc())
            .limit(1)
        )
        error.retryable = 0
    assert service.recover_interrupted() == 1
    repaired = service.get_build(waiting.id)
    assert repaired.current_stage is WorkflowState.FAILED
    assert repaired.version == failed.version
    assert (
        next(step for step in service.steps(waiting.id) if step["id"] == stale.id)["status"]
        == "interrupted"
    )
    assert service.errors(waiting.id)[-1]["retryable"] is False
    assert any(
        event["event_type"] == "workflow.step.interrupted"
        and event["payload"]["repair"] == "backend_restart"
        for event in service.timeline(waiting.id)
    )


def test_schema_version_is_validated_defensively() -> None:
    assert load_versioned_json(json.dumps({"schema_version": "1.0.0", "ok": True}))["ok"]
    with pytest.raises(WorkflowConflict):
        load_versioned_json(json.dumps({"schema_version": "99.0.0"}))


def test_foreign_key_enforcement_rejects_orphan(tmp_path) -> None:
    database = WorkflowDatabase(tmp_path / "fk.db")
    database.migrate()
    with pytest.raises(DatabaseError):
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO workflow_steps "
                    "(id,workflow_id,stage,attempt,status,idempotency_key,input_json) "
                    "VALUES ('step-orphan','missing','DRAFT',1,'running','key',"
                    '\'{"schema_version":"1.0.0"}\')'
                )
            )
    database.dispose()
