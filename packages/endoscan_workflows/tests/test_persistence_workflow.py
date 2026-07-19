from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import inspect, select, text, update
from sqlalchemy.exc import DatabaseError

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
from endoscan_workflows.errors import (
    ArtifactIntegrityError,
    ArtifactTooLarge,
    GuardNotSatisfied,
    InvalidTransition,
    StaleWorkflowVersion,
    WorkflowConflict,
    WorkflowNotFound,
)
from endoscan_workflows.models import EndpointBuildRow, WorkflowErrorRow, WorkflowEventRow
from endoscan_workflows.providers import ProviderFailure, ProviderTimeout
from endoscan_workflows.repository import load_versioned_json
from endoscan_workflows.training_dataset import TrainingDatasetSpecification


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
        "specification_draft_json",
        "specification_outcome_json",
        "specification_semantic_validation_json",
        "specification_compilation_outcome_json",
        "specification_review_record_json",
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
        ),
    )
    assert approved.current_stage is WorkflowState.DERIVING_COMPONENT_REQUIREMENTS
    discovering = service.derive_training_dataset_requirements(
        build.id,
        actor="deterministic-orchestrator",
        idempotency_key="derive-approved-requirements",
    )
    assert discovering.current_stage is WorkflowState.DISCOVERING_ACTIVITY_EVIDENCE
    assert service.training_dataset_workflow(build.id)["assembly_strategies"] is None


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
    database, store, providers, _harness, service = workflow_runtißž½¶‰žËkºwµçe¹Ðˆ°(€€€€€€€€€€€€€€€‰¥½±½¥…±}½…°ô‰‘¥™™•É•¹Ð‰¥½±½¥…°½…°Ñ¡…ÐµÕÍÐ¹½ÐÉ•ÕÍ”Ñ¡”­•ä¸ˆ°(€€€€€€€€€€€€€€€É•…Ñ•‘}‰äô‰Ñ•ÍÐµ…‘µ¥¸ˆ°(€€€€€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰Á¡…Í”ÀµÑ•ÍÐµÉ•…Ñ”ˆ°(€€€€€€€€€€€€¤(€€€€€€€€¤(()‘•˜Ñ•ÍÑ}‘¥Í½Ù•Éå}É•…¡•Í}Á•ÉÍ¥ÍÑ•‘}‘…Ñ…Í•Ñ}…ÁÁÉ½Ù…°¡Ý½É­™±½Ý}ÉÕ¹Ñ¥µ”¤€´ø9½¹”è(€€€}‘…Ñ…‰…Í”°ÍÑ½É”°}ÁÉ½Ù¥‘•ÉÌ°}¡…É¹•ÍÌ°Í•ÉÙ¥”€ôÝ½É­™±½Ý}ÉÕ¹Ñ¥µ”(€€€ÍÑ…ÉÑ•€ôÍÑ…ÉÑ}‰Õ¥±¡Í•ÉÙ¥”°É•…Ñ•}‰Õ¥±¡Í•ÉÙ¥”¤¤(€€€…ÍÍ•ÉÐÍÑ…ÉÑ•¹ÕÉÉ•¹Ñ}ÍÑ…”¥Ì]½É­™±½ÝMÑ…Ñ”¹]%Q%9}QMQ}AAI=Y0(€€€…ÍÍ•ÉÐÍÑ…ÉÑ•¹Á•¹‘¥¹}…ÁÁÉ½Ù…±}¥(€€€…ÍÍ•ÉÐí¥Ñ•´¹…ÉÑ¥™…Ñ}ÑåÁ”™½È¥Ñ•´¥¸ÍÑ½É”¹±¥ÍÑ}…ÉÑ¥™…ÑÌ¡ÍÑ…ÉÑ•¹¥¥ô€ôôì(€€€€€€€€‰•¹‘Á½¥¹Ñ}‘•™¥¹¥Ñ¥½¸ˆ°(€€€€€€€€‰‘…Ñ…Í•Ñ}…¹‘¥‘…Ñ•Ìˆ°(€€€€€€€€‰Í•…É¡}ÍÑÉ…Ñ•äˆ°(€€€€€€€€‰…•¹Ñ}É•½µµ•¹‘…Ñ¥½¸ˆ°(€€€€€€€€‰Í•…É¡}ÑÉ…”ˆ°(€€€ô(€€€…ÍÍ•ÉÐÍ•ÉÙ¥”¹…•¹Ñ}ÉÕ¹Ì¡ÍÑ…ÉÑ•¹¥¥lÁul‰ÁÉ½Ù¥‘•È‰t€ôô€‰™…­”ˆ(()‘•˜Ñ•ÍÑ}¥¹Ù…±¥‘}ÑÉ…¹Í¥Ñ¥½¹}É½±±Í}‰…­}Ý¥Ñ¡½ÕÑ}•Ù•¹Ð¡Ý½É­™±½Ý}ÉÕ¹Ñ¥µ”¤€´ø9½¹”è(€€€}‘…Ñ…‰…Í”°}ÍÑ½É”°}ÁÉ½Ù¥‘•ÉÌ°}¡…É¹•ÍÌ°Í•ÉÙ¥”€ôÝ½É­™±½Ý}ÉÕ¹Ñ¥µ”(€€€‰Õ¥±€ôÉ•…Ñ•}‰Õ¥±¡Í•ÉÙ¥”¤(€€€½Õ¹Ð€ô±•¸¡Í•ÉÙ¥”¹Ñ¥µ•±¥¹”¡‰Õ¥±¹¥¤¤(€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡%¹Ù…±¥‘QÉ…¹Í¥Ñ¥½¸¤è(€€€€€€€Í•ÉÙ¥”¹É•ÍÕµ” (€€€€€€€€€€€‰Õ¥±¹¥°(€€€€€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õ‰Õ¥±¹Ù•ÉÍ¥½¸°(€€€€€€€€€€€…Ñ½Èô‰Ñ•ÍÐµ…‘µ¥¸ˆ°(€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰¥¹Ù…±¥µÉ•ÍÕµ”µ™É½´µ‘É…™Ðˆ°(€€€€€€€€¤(€€€…ÍÍ•ÉÐ±•¸¡Í•ÉÙ¥”¹Ñ¥µ•±¥¹”¡‰Õ¥±¹¥¤¤€ôô½Õ¹Ð(€€€…ÍÍ•ÉÐÍ•ÉÙ¥”¹•Ñ}‰Õ¥±¡‰Õ¥±¹¥¤¹Ù•ÉÍ¥½¸€ôô€À(()‘•˜Ñ•ÍÑ}½ÁÑ¥µ¥ÍÑ¥}±½­}É•©•ÑÍ}ÍÑ…±•}Ù•ÉÍ¥½¸¡Ý½É­™±½Ý}ÉÕ¹Ñ¥µ”¤€´ø9½¹”è(€€€}‘…Ñ…‰…Í”°}ÍÑ½É”°}ÁÉ½Ù¥‘•ÉÌ°}¡…É¹•ÍÌ°Í•ÉÙ¥”€ôÝ½É­™±½Ý}ÉÕ¹Ñ¥µ”(€€€Ý…¥Ñ¥¹œ€ôÍÑ…ÉÑ}‰Õ¥±¡Í•ÉÙ¥”°É•…Ñ•}‰Õ¥±¡Í•ÉÙ¥”¤¤(€€€Á…ÕÍ•€ôÍ•ÉÙ¥”¹Á…ÕÍ” (€€€€€€€Ý…¥Ñ¥¹œ¹¥°(€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õÝ…¥Ñ¥¹œ¹Ù•ÉÍ¥½¸°(€€€€€€€…Ñ½Èô‰Ñ•ÍÐµ…‘µ¥¸ˆ°(€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰Á…ÕÍ”µ½¹”ˆ°(€€€€¤(€€€…ÍÍ•ÉÐÁ…ÕÍ•¹ÕÉÉ•¹Ñ}ÍÑ…”¥Ì]½É­™±½ÝMÑ…Ñ”¹AUM(€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡MÑ…±•]½É­™±½ÝY•ÉÍ¥½¸¤è(€€€€€€€Í•ÉÙ¥”¹…¹•° (€€€€€€€€€€€Ý…¥Ñ¥¹œ¹¥°(€€€€€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õÝ…¥Ñ¥¹œ¹Ù•ÉÍ¥½¸°(€€€€€€€€€€€…Ñ½Èô‰½Ñ¡•Èµ…‘µ¥¸ˆ°(€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰ÍÑ…±”µ…¹•°ˆ°(€€€€€€€€¤(()‘•˜Ñ•ÍÑ}½¹ÕÉÉ•¹Ñ}ÑÉ…¹Í¥Ñ¥½¹}¡…Í}½¹•}Ý¥¹¹•È¡Ý½É­™±½Ý}ÉÕ¹Ñ¥µ”¤€´ø9½¹”è(€€€}‘…Ñ…‰…Í”°}ÍÑ½É”°}ÁÉ½Ù¥‘•ÉÌ°}¡…É¹•ÍÌ°Í•ÉÙ¥”€ôÝ½É­™±½Ý}ÉÕ¹Ñ¥µ”(€€€Ý…¥Ñ¥¹œ€ôÍÑ…ÉÑ}‰Õ¥±¡Í•ÉÙ¥”°É•…Ñ•}‰Õ¥±¡Í•ÉÙ¥”¤¤((€€€‘•˜Á…ÕÍ”¡­•ä¤è(€€€€€€€ÑÉäè(€€€€€€€€€€€É•ÑÕÉ¸Í•ÉÙ¥”¹Á…ÕÍ” (€€€€€€€€€€€€€€€Ý…¥Ñ¥¹œ¹¥°(€€€€€€€€€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õÝ…¥Ñ¥¹œ¹Ù•ÉÍ¥½¸°(€€€€€€€€€€€€€€€…Ñ½Èô‰Ñ•ÍÐµ…‘µ¥¸ˆ°(€€€€€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äõ­•ä°(€€€€€€€€€€€€¤¹ÕÉÉ•¹Ñ}ÍÑ…”(€€€€€€€•á•ÁÐ€¡MÑ…±•]½É­™±½ÝY•ÉÍ¥½¸°%¹Ù…±¥‘QÉ…¹Í¥Ñ¥½¸¤è(€€€€€€€€€€€É•ÑÕÉ¸€‰½¹™±¥Ðˆ((€€€Ý¥Ñ Q¡É•…‘A½½±á•ÕÑ½È¡µ…á}Ý½É­•ÉÌôÈ¤…Ì•á•ÕÑ½Èè(€€€€€€€½ÕÑ½µ•Ì€ô±¥ÍÐ¡•á•ÕÑ½È¹µ…À¡Á…ÕÍ”°l‰½¹ÕÉÉ•¹Ðµ„ˆ°€‰½¹ÕÉÉ•¹Ðµˆ‰t¤¤(€€€…ÍÍ•ÉÐ½ÕÑ½µ•Ì¹½Õ¹Ð¡]½É­™±½ÝMÑ…Ñ”¹AUM¤€ôô€Ä(€€€…ÍÍ•ÉÐ½ÕÑ½µ•Ì¹½Õ¹Ð ‰½¹™±¥Ðˆ¤€ôô€Ä(()‘•˜Ñ•ÍÑ}Á…ÕÍ•}É•ÍÕµ•}ÁÉ•Í•ÉÙ•Í}Õ¹‘•É±å¥¹}ÍÑ…”¡Ý½É­™±½Ý}ÉÕ¹Ñ¥µ”¤€´ø9½¹”è(€€€}‘…Ñ…‰…Í”°}ÍÑ½É”°}ÁÉ½Ù¥‘•ÉÌ°}¡…É¹•ÍÌ°Í•ÉÙ¥”€ôÝ½É­™±½Ý}ÉÕ¹Ñ¥µ”(€€€Ý…¥Ñ¥¹œ€ôÍÑ…ÉÑ}‰Õ¥±¡Í•ÉÙ¥”°É•…Ñ•}‰Õ¥±¡Í•ÉÙ¥”¤¤(€€€Á…ÕÍ•€ôÍ•ÉÙ¥”¹Á…ÕÍ” (€€€€€€€Ý…¥Ñ¥¹œ¹¥°(€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õÝ…¥Ñ¥¹œ¹Ù•ÉÍ¥½¸°(€€€€€€€…Ñ½Èô‰Ñ•ÍÐµ…‘µ¥¸ˆ°(€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰Á…ÕÍ”µÍÑ…”ˆ°(€€€€¤(€€€…ÍÍ•ÉÐÁ…ÕÍ•¹Á…ÕÍ•‘}™É½µ}ÍÑ…Ñ”¥Ì]½É­™±½ÝMÑ…Ñ”¹]%Q%9}QMQ}AAI=Y0(€€€É•ÍÕµ•€ôÍ•ÉÙ¥”¹É•ÍÕµ” (€€€€€€€Ý…¥Ñ¥¹œ¹¥°(€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õÁ…ÕÍ•¹Ù•ÉÍ¥½¸°(€€€€€€€…Ñ½Èô‰Ñ•ÍÐµ…‘µ¥¸ˆ°(€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰É•ÍÕµ”µÍÑ…”ˆ°(€€€€¤(€€€…ÍÍ•ÉÐÉ•ÍÕµ•¹ÕÉÉ•¹Ñ}ÍÑ…”¥Ì]½É­™±½ÝMÑ…Ñ”¹]%Q%9}QMQ}AAI=Y0(€€€…ÍÍ•ÉÐÉ•ÍÕµ•¹Á…ÕÍ•‘}™É½µ}ÍÑ…Ñ”¥Ì9½¹”(()‘•˜Ñ•ÍÑ}…¹•±}¥Í}Ñ•Éµ¥¹…°¡Ý½É­™±½Ý}ÉÕ¹Ñ¥µ”¤€´ø9½¹”è(€€€}‘…Ñ…‰…Í”°}ÍÑ½É”°}ÁÉ½Ù¥‘•ÉÌ°}¡…É¹•ÍÌ°Í•ÉÙ¥”€ôÝ½É­™±½Ý}ÉÕ¹Ñ¥µ”(€€€Ý…¥Ñ¥¹œ€ôÍÑ…ÉÑ}‰Õ¥±¡Í•ÉÙ¥”°É•…Ñ•}‰Õ¥±¡Í•ÉÙ¥”¤¤(€€€…¹•±±•€ôÍ•ÉÙ¥”¹…¹•° (€€€€€€€Ý…¥Ñ¥¹œ¹¥°(€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õÝ…¥Ñ¥¹œ¹Ù•ÉÍ¥½¸°(€€€€€€€…Ñ½Èô‰Ñ•ÍÐµ…‘µ¥¸ˆ°(€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰…¹•°µÑ•Éµ¥¹…°ˆ°(€€€€¤(€€€…ÍÍ•ÉÐ…¹•±±•¹ÕÉÉ•¹Ñ}ÍÑ…”¥Ì]½É­™±½ÝMÑ…Ñ”¹911(€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡%¹Ù…±¥‘QÉ…¹Í¥Ñ¥½¸¤è(€€€€€€€Í•ÉÙ¥”¹É•ÍÕµ” (€€€€€€€€€€€Ý…¥Ñ¥¹œ¹¥°(€€€€€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õ…¹•±±•¹Ù•ÉÍ¥½¸°(€€€€€€€€€€€…Ñ½Èô‰Ñ•ÍÐµ…‘µ¥¸ˆ°(€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰É•ÍÕµ”µ…¹•±±•ˆ°(€€€€€€€€¤(()‘•˜Ñ•ÍÑ}…ÁÁÉ½Ù…±}¡…Í¡}‰¥¹‘¥¹}…¹‘}¥µµÕÑ…‰±•}‘•¥Í¥½¸¡Ý½É­™±½Ý}ÉÕ¹Ñ¥µ”¤€´ø9½¹”è(€€€}‘…Ñ…‰…Í”°}ÍÑ½É”°}ÁÉ½Ù¥‘•ÉÌ°}¡…É¹•ÍÌ°Í•ÉÙ¥”€ôÝ½É­™±½Ý}ÉÕ¹Ñ¥µ”(€€€Ý…¥Ñ¥¹œ€ôÍÑ…ÉÑ}‰Õ¥±¡Í•ÉÙ¥”°É•…Ñ•}‰Õ¥±¡Í•ÉÙ¥”¤¤(€€€…ÁÁÉ½Ù…°€ôÍ•ÉÙ¥”¹•Ñ}…ÁÁÉ½Ù…°¡Ý…¥Ñ¥¹œ¹Á•¹‘¥¹}…ÁÁÉ½Ù…±}¥¤(€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡Õ…É‘9½ÑM…Ñ¥Í™¥•¤è(€€€€€€€Í•ÉÙ¥”¹‘•¥‘•}…ÁÁÉ½Ù…° (€€€€€€€€€€€…ÁÁÉ½Ù…±l‰¥‰t°(€€€€€€€€€€€ÁÁÉ½Ù…±•¥Í¥½¸ (€€€€€€€€€€€€€€€‘•¥Í¥½¸õÁÁÉ½Ù…±•¥Í¥½¹Y…±Õ”¹AAI=Y°(€€€€€€€€€€€€€€€É•Ù¥•Ý•É}¥ô‰Í¥•¹Ñ¥ÍÐˆ°(€€€€€€€€€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õÝ…¥Ñ¥¹œ¹Ù•ÉÍ¥½¸°(€€€€€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰‰…µ…ÁÁÉ½Ù…°µ¡…Í ˆ°(€€€€€€€€€€€€€€€…ÉÑ¥™…Ñ}¡…Í¡•ÌõlˆÀˆ€¨€ØÑt°(€€€€€€€€€€€€¤°(€€€€€€€€¤(€€€…ÁÁÉ½Ù•€ôÍ•ÉÙ¥”¹‘•¥‘•}…ÁÁÉ½Ù…° (€€€€€€€…ÁÁÉ½Ù…±l‰¥‰t°(€€€€€€€ÁÁÉ½Ù…±•¥Í¥½¸ (€€€€€€€€€€€‘•¥Í¥½¸õÁÁÉ½Ù…±•¥Í¥½¹Y…±Õ”¹AAI=Y°(€€€€€€€€€€€É•Ù¥•Ý•É}¥ô‰Í¥•¹Ñ¥ÍÐˆ°(€€€€€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õÝ…¥Ñ¥¹œ¹Ù•ÉÍ¥½¸°(€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰½½µ…ÁÁÉ½Ù…°µ¡…Í ˆ°(€€€€€€€€€€€…ÉÑ¥™…Ñ}¡…Í¡•Ìõ…ÁÁÉ½Ù…±l‰É•ÅÕ•ÍÐ‰ul‰…ÉÑ¥™…Ñ}¡…Í¡•Ì‰t°(€€€€€€€€¤°(€€€€¤(€€€…ÍÍ•ÉÐ…ÁÁÉ½Ù•¹ÕÉÉ•¹Ñ}ÍÑ…”¥Ì]½É­™±½ÝMÑ…Ñ”¹UIQ%9}Q(€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡]½É­™±½Ý½¹™±¥Ð¤è(€€€€€€€Í•ÉÙ¥”¹‘•¥‘•}…ÁÁÉ½Ù…° (€€€€€€€€€€€…ÁÁÉ½Ù…±l‰¥‰t°(€€€€€€€€€€€ÁÁÉ½Ù…±•¥Í¥½¸ (€€€€€€€€€€€€€€€‘•¥Í¥½¸õÁÁÉ½Ù…±•¥Í¥½¹Y…±Õ”¹I)P°(€€€€€€€€€€€€€€€É•Ù¥•Ý•É}¥ô‰Í¥•¹Ñ¥ÍÐˆ°(€€€€€€€€€€€€€€€É•Ù¥•Ý•É}½µµ•¹Ðô‰…¹¹½ÐÉ•ÝÉ¥Ñ”Ñ¡”ÁÉ¥½È‘•¥Í¥½¸¸ˆ°(€€€€€€€€€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õ…ÁÁÉ½Ù•¹Ù•ÉÍ¥½¸°(€€€€€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰É•ÝÉ¥Ñ”µ…ÁÁÉ½Ù…°ˆ°(€€€€€€€€€€€€€€€…ÉÑ¥™…Ñ}¡…Í¡•Ìõ…ÁÁÉ½Ù…±l‰É•ÅÕ•ÍÐ‰ul‰…ÉÑ¥™…Ñ}¡…Í¡•Ì‰t°(€€€€€€€€€€€€¤°(€€€€€€€€¤(()‘•˜Ñ•ÍÑ}É•Ù¥Í¥½¹}É•…Ñ•Í}¹•Ý}ÁÉ½Á½Í…±}…¹‘}É•Á±…åÍ}Ñ½½±Ì¡Ý½É­™±½Ý}ÉÕ¹Ñ¥µ”¤€´ø9½¹”è(€€€}‘…Ñ…‰…Í”°}ÍÑ½É”°}ÁÉ½Ù¥‘•ÉÌ°}¡…É¹•ÍÌ°Í•ÉÙ¥”€ôÝ½É­™±½Ý}ÉÕ¹Ñ¥µ”(€€€Ý…¥Ñ¥¹œ€ôÍÑ…ÉÑ}‰Õ¥±¡Í•ÉÙ¥”°É•…Ñ•}‰Õ¥±¡Í•ÉÙ¥”¤¤(€€€…ÁÁÉ½Ù…°€ôÍ•ÉÙ¥”¹•Ñ}…ÁÁÉ½Ù…°¡Ý…¥Ñ¥¹œ¹Á•¹‘¥¹}…ÁÁÉ½Ù…±}¥¤(€€€É•Ù¥Í•€ôÍ•ÉÙ¥”¹‘•¥‘•}…ÁÁÉ½Ù…° (€€€€€€€…ÁÁÉ½Ù…±l‰¥‰t°(€€€€€€€ÁÁÉ½Ù…±•¥Í¥½¸ (€€€€€€€€€€€‘•¥Í¥½¸õÁÁÉ½Ù…±•¥Í¥½¹Y…±Õ”¹IEUMQ}IY%M%=8°(€€€€€€€€€€€É•Ù¥•Ý•É}¥ô‰Í¥•¹Ñ¥ÍÐˆ°(€€€€€€€€€€€É•Ù¥•Ý•É}½µµ•¹Ðô‰É•…Ñ”„¹•ÜÁÉ•Á…É•½µÁ…É¥Í½¸É•Ù¥Í¥½¸¸ˆ°(€€€€€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õÝ…¥Ñ¥¹œ¹Ù•ÉÍ¥½¸°(€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰É•Ù¥Í¥½¸µ‘•¥Í¥½¸ˆ°(€€€€€€€€€€€…ÉÑ¥™…Ñ}¡…Í¡•Ìõ…ÁÁÉ½Ù…±l‰É•ÅÕ•ÍÐ‰ul‰…ÉÑ¥™…Ñ}¡…Í¡•Ì‰t°(€€€€€€€€¤°(€€€€¤(€€€É•ÉÕ¸€ôÍ•ÉÙ¥”¹ÉÕ¹}‘¥Í½Ù•Éä (€€€€€€€É•Ù¥Í•¹¥°(€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õÉ•Ù¥Í•¹Ù•ÉÍ¥½¸°(€€€€€€€…Ñ½Èô‰Í¥•¹Ñ¥ÍÐˆ°(€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰É•Ù¥Í¥½¸µ‘¥Í½Ù•Éäˆ°(€€€€¤(€€€…ÍÍ•ÉÐÉ•ÉÕ¸¹Á•¹‘¥¹}…ÁÁÉ½Ù…±}¥€„ô…ÁÁÉ½Ù…±l‰¥‰t(€€€…ÍÍ•ÉÐ±•¸¡Í•ÉÙ¥”¹…•¹Ñ}ÉÕ¹Ì¡É•ÉÕ¸¹¥¤¤€ôô€È(()‘•˜Ñ•ÍÑ}¥µµÕÑ…‰±•}•Ù•¹Ñ}ÑÉ¥•É}É•©•ÑÍ}ÕÁ‘…Ñ”¡Ý½É­™±½Ý}ÉÕ¹Ñ¥µ”¤€´ø9½¹”è(€€€‘…Ñ…‰…Í”°}ÍÑ½É”°}ÁÉ½Ù¥‘•ÉÌ°}¡…É¹•ÍÌ°Í•ÉÙ¥”€ôÝ½É­™±½Ý}ÉÕ¹Ñ¥µ”(€€€‰Õ¥±€ôÉ•…Ñ•}‰Õ¥±¡Í•ÉÙ¥”¤(€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡…Ñ…‰…Í•ÉÉ½È¤è(€€€€€€€Ý¥Ñ ‘…Ñ…‰…Í”¹Í•ÍÍ¥½¸ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€•Ù•¹Ñ}¥€ôÍ•ÍÍ¥½¸¹Í…±…È (€€€€€€€€€€€€€€€Í•±•Ð¡]½É­™±½ÝÙ•¹ÑI½Ü¹¥¤¹Ý¡•É”¡]½É­™±½ÝÙ•¹ÑI½Ü¹Ý½É­™±½Ý}¥€ôô‰Õ¥±¹¥¤(€€€€€€€€€€€€¤(€€€€€€€€€€€Í•ÍÍ¥½¸¹•á•ÕÑ” (€€€€€€€€€€€€€€€ÕÁ‘…Ñ”¡]½É­™±½ÝÙ•¹ÑI½Ü¤(€€€€€€€€€€€€€€€€¹Ý¡•É”¡]½É­™±½ÝÙ•¹ÑI½Ü¹¥€ôô•Ù•¹Ñ}¥¤(€€€€€€€€€€€€€€€€¹Ù…±Õ•Ì¡•Ù•¹Ñ}ÑåÁ”ô‰Ñ…µÁ•É•ˆ¤(€€€€€€€€€€€€¤(()‘•˜Ñ•ÍÑ}ÑÉ…¹Í…Ñ¥½¹}É½±±‰…­}É•µ½Ù•Í}‘…Ñ…‰…Í•}µÕÑ…Ñ¥½¸¡Ý½É­™±½Ý}ÉÕ¹Ñ¥µ”¤€´ø9½¹”è(€€€‘…Ñ…‰…Í”°}ÍÑ½É”°}ÁÉ½Ù¥‘•ÉÌ°}¡…É¹•ÍÌ°Í•ÉÙ¥”€ôÝ½É­™±½Ý}ÉÕ¹Ñ¥µ”(€€€‰Õ¥±€ôÉ•…Ñ•}‰Õ¥±¡Í•ÉÙ¥”¤(€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡IÕ¹Ñ¥µ•ÉÉ½È¤è(€€€€€€€Ý¥Ñ ‘…Ñ…‰…Í”¹Í•ÍÍ¥½¸ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€€€€€É½Ü€ôÍ•ÍÍ¥½¸¹•Ð¡¹‘Á½¥¹Ñ	Õ¥±‘I½Ü°‰Õ¥±¹¥¤(€€€€€€€€€€€É½Ü¹•¹‘Á½¥¹Ñ}¹…µ”€ô€‰M¡½Õ±É½±°‰…¬ˆ(€€€€€€€€€€€É…¥Í”IÕ¹Ñ¥µ•ÉÉ½È ‰É½±±‰…¬ˆ¤(€€€…ÍÍ•ÉÐÍ•ÉÙ¥”¹•Ñ}‰Õ¥±¡‰Õ¥±¹¥¤¹•¹‘Á½¥¹Ñ}¹…µ”€ôô€‰=á¥‘…Ñ¥Ù”ÍÑÉ•ÍÌˆ(()‘•˜Ñ•ÍÑ}…ÉÑ¥™…Ñ}¥¹Ñ•É¥Ñå}…¹‘}Í¥é•}±¥µ¥Ð¡Ý½É­™±½Ý}ÉÕ¹Ñ¥µ”¤€´ø9½¹”è(€€€}‘…Ñ…‰…Í”°ÍÑ½É”°}ÁÉ½Ù¥‘•ÉÌ°}¡…É¹•ÍÌ°Í•ÉÙ¥”€ôÝ½É­™±½Ý}ÉÕ¹Ñ¥µ”(€€€‰Õ¥±€ôÉ•…Ñ•}‰Õ¥±¡Í•ÉÙ¥”¤(€€€…ÉÑ¥™…Ð€ôÍÑ½É”¹ÁÕÑ}‰åÑ•Ì (€€€€€€€Ý½É­™±½Ý}¥õ‰Õ¥±¹¥°(€€€€€€€½¹Ñ•¹Ðõˆ‰‰½Õ¹‘•ˆ°(€€€€€€€µ¥µ•}ÑåÁ”ô‰Ñ•áÐ½Á±…¥¸ˆ°(€€€€€€€…ÉÑ¥™…Ñ}ÑåÁ”ô‰Ñ•ÍÐˆ°(€€€€€€€±½¥…±}¹…µ”ô‰‰½Õ¹‘•¹ÑáÐˆ°(€€€€€€€ÁÉ½‘Õ•Èô‰Ñ•ÍÐˆ°(€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰‰½Õ¹‘•µ…ÉÑ¥™…Ðˆ°(€€€€¤(€€€…ÍÍ•ÉÐÍÑ½É”¹Ù•É¥™ä¡…ÉÑ¥™…Ð¹¥¤(€€€ÍÑ½É”¹}Á…Ñ ¡…ÉÑ¥™…Ð¹Í¡„ÈÔØ¤¹ÝÉ¥Ñ•}‰åÑ•Ì¡ˆ‰Ñ…µÁ•É•ˆ¤(€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡ÉÑ¥™…Ñ%¹Ñ•É¥ÑåÉÉ½È¤è(€€€€€€€ÍÑ½É”¹•Ð¡…ÉÑ¥™…Ð¹¥¤(€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡ÉÑ¥™…ÑQ½½1…É”¤è(€€€€€€€ÍÑ½É”¹ÁÕÑ}‰åÑ•Ì (€€€€€€€€€€€Ý½É­™±½Ý}¥õ‰Õ¥±¹¥°(€€€€€€€€€€€½¹Ñ•¹Ðõˆ‰àˆ€¨€ ÄÀÈÐ€¨€ÄÀÈÐ€¬€Ä¤°(€€€€€€€€€€€µ¥µ•}ÑåÁ”ô‰…ÁÁ±¥…Ñ¥½¸½½Ñ•ÐµÍÑÉ•…´ˆ°(€€€€€€€€€€€…ÉÑ¥™…Ñ}ÑåÁ”ô‰Ñ•ÍÐˆ°(€€€€€€€€€€€±½¥…±}¹…µ”ô‰Ñ½¼µ±…É”¹‰¥¸ˆ°(€€€€€€€€€€€ÁÉ½‘Õ•Èô‰Ñ•ÍÐˆ°(€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰½Ù•ÉÍ¥é•µ…ÉÑ¥™…Ðˆ°(€€€€€€€€¤(()‘•˜Ñ•ÍÑ}…ÉÑ¥™…Ñ}¥‘Í}‘½}¹½Ñ}…•ÁÑ}Á…Ñ¡}ÑÉ…Ù•ÉÍ…°¡Ý½É­™±½Ý}ÉÕ¹Ñ¥µ”¤€´ø9½¹”è(€€€}‘…Ñ…‰…Í”°ÍÑ½É”°}ÁÉ½Ù¥‘•ÉÌ°}¡…É¹•ÍÌ°}Í•ÉÙ¥”€ôÝ½É­™±½Ý}ÉÕ¹Ñ¥µ”(€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡]½É­™±½Ý9½Ñ½Õ¹¤è(€€€€€€€ÍÑ½É”¹•Ð ˆ¸¸¼¸¸½É•¥ÍÑÉä½µ½‘•±Ì½•¹‘Á½¥¹ÑÌ¹©Í½¸ˆ¤(()‘•˜Ñ•ÍÑ}É•ÍÑ…ÉÑ}É•½Ù•Éå}µ…É­Í}ÉÕ¹¹¥¹}ÍÑ•Á}É•ÑÉå…‰±”¡Ý½É­™±½Ý}ÉÕ¹Ñ¥µ”¤€´ø9½¹”è(€€€}‘…Ñ…‰…Í”°}ÍÑ½É”°}ÁÉ½Ù¥‘•ÉÌ°}¡…É¹•ÍÌ°Í•ÉÙ¥”€ôÝ½É­™±½Ý}ÉÕ¹Ñ¥µ”(€€€Ý…¥Ñ¥¹œ€ôÍÑ…ÉÑ}‰Õ¥±¡Í•ÉÙ¥”°É•…Ñ•}‰Õ¥±¡Í•ÉÙ¥”¤¤(€€€…ÁÁÉ½Ù…°€ôÍ•ÉÙ¥”¹•Ñ}…ÁÁÉ½Ù…°¡Ý…¥Ñ¥¹œ¹Á•¹‘¥¹}…ÁÁÉ½Ù…±}¥¤(€€€ÕÉ…Ñ¥¹œ€ôÍ•ÉÙ¥”¹‘•¥‘•}…ÁÁÉ½Ù…° (€€€€€€€…ÁÁÉ½Ù…±l‰¥‰t°(€€€€€€€ÁÁÉ½Ù…±•¥Í¥½¸ (€€€€€€€€€€€‘•¥Í¥½¸õÁÁÉ½Ù…±•¥Í¥½¹Y…±Õ”¹AAI=Y°(€€€€€€€€€€€É•Ù¥•Ý•É}¥ô‰Í¥•¹Ñ¥ÍÐˆ°(€€€€€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õÝ…¥Ñ¥¹œ¹Ù•ÉÍ¥½¸°(€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰É•½Ù•Éäµ…ÁÁÉ½Ù…°ˆ°(€€€€€€€€€€€…ÉÑ¥™…Ñ}¡…Í¡•Ìõ…ÁÁÉ½Ù…±l‰É•ÅÕ•ÍÐ‰ul‰…ÉÑ¥™…Ñ}¡…Í¡•Ì‰t°(€€€€€€€€¤°(€€€€¤(€€€Í•ÉÙ¥”¹É•…Ñ•}ÍÑ•À (€€€€€€€ÕÉ…Ñ¥¹œ¹¥°(€€€€€€€]½É­™±½ÝMÑ…Ñ”¹UIQ%9}Q°(€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰¥¹Ñ•ÉÉÕÁÑ•µÕÉ…Ñ¥½¸ˆ°(€€€€€€€¥¹ÁÕÑ}Á…å±½…õì‰ÁÉ•Á…É•ˆèQÉÕ•ô°(€€€€¤(€€€…ÍÍ•ÉÐÍ•ÉÙ¥”¹É•½Ù•É}¥¹Ñ•ÉÉÕÁÑ• ¤€ôô€Ä(€€€É•½Ù•É•€ôÍ•ÉÙ¥”¹•Ñ}‰Õ¥±¡ÕÉ…Ñ¥¹œ¹¥¤(€€€…ÍÍ•ÉÐÉ•½Ù•É•¹ÕÉÉ•¹Ñ}ÍÑ…”¥Ì]½É­™±½ÝMÑ…Ñ”¹%1(€€€…ÍÍ•ÉÐÉ•½Ù•É•¹™…¥±•‘}™É½µ}ÍÑ…Ñ”¥Ì]½É­™±½ÝMÑ…Ñ”¹UIQ%9}Q(€€€…ÍÍ•ÉÐÍ•ÉÙ¥”¹ÍÑ•ÁÌ¡ÕÉ…Ñ¥¹œ¹¥¥l´Åul‰ÍÑ…ÑÕÌ‰t€ôô€‰¥¹Ñ•ÉÉÕÁÑ•ˆ(€€€…ÍÍ•ÉÐÍ•ÉÙ¥”¹Ñ¥µ•±¥¹”¡ÕÉ…Ñ¥¹œ¹¥¥l´Éul‰•Ù•¹Ñ}ÑåÁ”‰t€ôô€‰Ý½É­™±½Ü¹ÍÑ•À¹¥¹Ñ•ÉÉÕÁÑ•ˆ(€€€…ÍÍ•ÉÐÍ•ÉÙ¥”¹±¥ÍÑ}…ÁÁÉ½Ù…±Ì¡ÕÉ…Ñ¥¹œ¹¥¥l´Åul‰ÍÑ…ÑÕÌ‰t€ôô€‰…ÁÁÉ½Ù•ˆ(()‘•˜Ñ•ÍÑ}½¹ÑÉ½±±•‘}™…¥±ÕÉ•}É•ÑÉå}‘½•Í}¹½Ñ}‘ÕÁ±¥…Ñ•}‘¥Í½Ù•Éä¡Ý½É­™±½Ý}ÉÕ¹Ñ¥µ”¤€´ø9½¹”è(€€€}‘…Ñ…‰…Í”°}ÍÑ½É”°}ÁÉ½Ù¥‘•ÉÌ°}¡…É¹•ÍÌ°Í•ÉÙ¥”€ôÝ½É­™±½Ý}ÉÕ¹Ñ¥µ”(€€€Ý…¥Ñ¥¹œ€ôÍÑ…ÉÑ}‰Õ¥±¡Í•ÉÙ¥”°É•…Ñ•}‰Õ¥±¡Í•ÉÙ¥”¤¤(€€€ÉÕ¹}½Õ¹Ð€ô±•¸¡Í•ÉÙ¥”¹…•¹Ñ}ÉÕ¹Ì¡Ý…¥Ñ¥¹œ¹¥¤¤(€€€™…¥±•€ôÍ•ÉÙ¥”¹Í¥µÕ±…Ñ•}™…¥±ÕÉ” (€€€€€€€Ý…¥Ñ¥¹œ¹¥°(€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õÝ…¥Ñ¥¹œ¹Ù•ÉÍ¥½¸°(€€€€€€€…Ñ½Èô‰Ñ•ÍÐµ…‘µ¥¸ˆ°(€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰½¹ÑÉ½±±•µ™…¥±ÕÉ”ˆ°(€€€€¤(€€€É•ÑÉ¥•€ôÍ•ÉÙ¥”¹É•ÑÉå}™…¥±• (€€€€€€€Ý…¥Ñ¥¹œ¹¥°(€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õ™…¥±•¹Ù•ÉÍ¥½¸°(€€€€€€€…Ñ½Èô‰Ñ•ÍÐµ…‘µ¥¸ˆ°(€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰½¹ÑÉ½±±•µÉ•ÑÉäˆ°(€€€€¤(€€€…ÍÍ•ÉÐÉ•ÑÉ¥•¹ÕÉÉ•¹Ñ}ÍÑ…”¥Ì]½É­™±½ÝMÑ…Ñ”¹]%Q%9}QMQ}AAI=Y0(€€€…ÍÍ•ÉÐ±•¸¡Í•ÉÙ¥”¹…•¹Ñ}ÉÕ¹Ì¡Ý…¥Ñ¥¹œ¹¥¤¤€ôôÉÕ¹}½Õ¹Ð(()‘•˜Ñ•ÍÑ}¹•Ý}ÍÑ•Á}…ÑÑ•µÁÑ}•áÁ±¥¥Ñ±å}ÍÕÁ•ÉÍ•‘•Í}½¹±å}…Ñ¥Ù•}…ÑÑ•µÁÐ¡Ý½É­™±½Ý}ÉÕ¹Ñ¥µ”¤€´ø9½¹”è(€€€}‘…Ñ…‰…Í”°}ÍÑ½É”°}ÁÉ½Ù¥‘•ÉÌ°}¡…É¹•ÍÌ°Í•ÉÙ¥”€ôÝ½É­™±½Ý}ÉÕ¹Ñ¥µ”(€€€Ý…¥Ñ¥¹œ€ôÍÑ…ÉÑ}‰Õ¥±¡Í•ÉÙ¥”°É•…Ñ•}‰Õ¥±¡Í•ÉÙ¥”¤¤(€€€…ÁÁÉ½Ù…°€ôÍ•ÉÙ¥”¹•Ñ}…ÁÁÉ½Ù…°¡Ý…¥Ñ¥¹œ¹Á•¹‘¥¹}…ÁÁÉ½Ù…±}¥¤(€€€ÕÉ…Ñ¥¹œ€ôÍ•ÉÙ¥”¹‘•¥‘•}…ÁÁÉ½Ù…° (€€€€€€€…ÁÁÉ½Ù…±l‰¥‰t°(€€€€€€€ÁÁÉ½Ù…±•¥Í¥½¸ (€€€€€€€€€€€‘•¥Í¥½¸õÁÁÉ½Ù…±•¥Í¥½¹Y…±Õ”¹AAI=Y°(€€€€€€€€€€€É•Ù¥•Ý•É}¥ô‰Í¥•¹Ñ¥ÍÐˆ°(€€€€€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õÝ…¥Ñ¥¹œ¹Ù•ÉÍ¥½¸°(€€€€€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰ÍÕÁ•ÉÍ•‘”µ…ÁÁÉ½Ù…°ˆ°(€€€€€€€€€€€…ÉÑ¥™…Ñ}¡…Í¡•Ìõ…ÁÁÉ½Ù…±l‰É•ÅÕ•ÍÐ‰ul‰…ÉÑ¥™…Ñ}¡…Í¡•Ì‰t°(€€€€€€€€¤°(€€€€¤(€€€™¥ÉÍÐ€ôÍ•ÉÙ¥”¹É•…Ñ•}ÍÑ•À (€€€€€€€ÕÉ…Ñ¥¹œ¹¥°(€€€€€€€]½É­™±½ÝMÑ…Ñ”¹UIQ%9}Q°(€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰ÕÉ…Ñ¥½¸µ…ÑÑ•µÁÐµ½¹”ˆ°(€€€€€€€¥¹ÁÕÑ}Á…å±½…õì‰…ÑÑ•µÁÐˆè€Åô°(€€€€¤(€€€Í•½¹€ôÍ•ÉÙ¥”¹É•…Ñ•}ÍÑ•À (€€€€€€€ÕÉ…Ñ¥¹œ¹¥°(€€€€€€€]½É­™±½ÝMÑ…Ñ”¹UIQ%9}Q°(€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰ÕÉ…Ñ¥½¸µ…ÑÑ•µÁÐµÑÝ¼ˆ°(€€€€€€€¥¹ÁÕÑ}Á…å±½…õì‰…ÑÑ•µÁÐˆè€Éô°(€€€€¤(€€€ÍÑ•ÁÌ€ôÍ•ÉÙ¥”¹ÍÑ•ÁÌ¡ÕÉ…Ñ¥¹œ¹¥¤(€€€…ÍÍ•ÉÐ™¥ÉÍÐ¹¥€„ôÍ•½¹¹¥(€€€…ÍÍ•ÉÐmÍÑ•Ál‰ÍÑ…ÑÕÌ‰t™½ÈÍÑ•À¥¸ÍÑ•ÁÍl´Èéut€ôôl‰¥¹Ñ•ÉÉÕÁÑ•ˆ°€‰ÉÕ¹¹¥¹œ‰t(€€€…ÍÍ•ÉÐÍÕ´¡ÍÑ•Ál‰ÍÑ…ÑÕÌ‰t€ôô€‰ÉÕ¹¹¥¹œˆ™½ÈÍÑ•À¥¸ÍÑ•ÁÌ¤€ôô€Ä(€€€…ÍÍ•ÉÐ…¹ä (€€€€€€€•Ù•¹Ñl‰•Ù•¹Ñ}ÑåÁ”‰t€ôô€‰Ý½É­™±½Ü¹ÍÑ•À¹ÍÕÁ•ÉÍ•‘•ˆ(€€€€€€€…¹•Ù•¹Ñl‰Á…å±½…‰ul‰ÍÑ•Á}¥‰t€ôô™¥ÉÍÐ¹¥(€€€€€€€™½È•Ù•¹Ð¥¸Í•ÉÙ¥”¹Ñ¥µ•±¥¹”¡ÕÉ…Ñ¥¹œ¹¥¤(€€€€¤(()‘•˜Ñ•ÍÑ}É•ÍÑ…ÉÑ}É•Á…¥ÉÍ}ÍÑ…±•}…ÑÑ•µÁÑ}½¹}…±É•…‘å}™…¥±•‘}Ý½É­™±½Ü¡Ý½É­™±½Ý}ÉÕ¹Ñ¥µ”¤€´ø9½¹”è(€€€‘…Ñ…‰…Í”°}ÍÑ½É”°}ÁÉ½Ù¥‘•ÉÌ°}¡…É¹•ÍÌ°Í•ÉÙ¥”€ôÝ½É­™±½Ý}ÉÕ¹Ñ¥µ”(€€€Ý…¥Ñ¥¹œ€ôÍÑ…ÉÑ}‰Õ¥±¡Í•ÉÙ¥”°É•…Ñ•}‰Õ¥±¡Í•ÉÙ¥”¤¤(€€€™…¥±•€ôÍ•ÉÙ¥”¹Í¥µÕ±…Ñ•}™…¥±ÕÉ” (€€€€€€€Ý…¥Ñ¥¹œ¹¥°(€€€€€€€•áÁ•Ñ•‘}Ù•ÉÍ¥½¸õÝ…¥Ñ¥¹œ¹Ù•ÉÍ¥½¸°(€€€€€€€…Ñ½Èô‰Ñ•ÍÐµ…‘µ¥¸ˆ°(€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰Á•ÉÍ¥ÍÑ•µ™…¥±ÕÉ”ˆ°(€€€€¤(€€€ÍÑ…±”€ôÍ•ÉÙ¥”¹É•…Ñ•}ÍÑ•À (€€€€€€€Ý…¥Ñ¥¹œ¹¥°(€€€€€€€]½É­™±½ÝMÑ…Ñ”¹%M=YI%9}Q°(€€€€€€€¥‘•µÁ½Ñ•¹å}­•äô‰Á•ÉÍ¥ÍÑ•µÍÑ…±”µ…ÑÑ•µÁÐˆ°(€€€€€€€¥¹ÁÕÑ}Á…å±½…õì‰Á•ÉÍ¥ÍÑ•ˆèQÉÕ•ô°(€€€€¤(€€€Ý¥Ñ ‘…Ñ…‰…Í”¹Í•ÍÍ¥½¸ ¤…ÌÍ•ÍÍ¥½¸è(€€€€€€€•ÉÉ½È€ôÍ•ÍÍ¥½¸¹Í…±…È (€€€€€€€€€€€Í•±•Ð¡]½É­™±½ÝÉÉ½ÉI½Ü¤(€€€€€€€€€€€€¹Ý¡•É”¡]½É­™±½ÝÉÉ½ÉI½Ü¹Ý½É­™±½Ý}¥€ôôÝ…¥Ñ¥¹œ¹¥¤(€€€€€€€€€€€€¹½É‘•É}‰ä¡]½É­™±½ÝÉÉ½ÉI½Ü¹É•…Ñ•‘}…Ð¹‘•ÍŒ ¤°]½É­™±½ÝÉÉ½ÉI½Ü¹¥¹‘•ÍŒ ¤¤(€€€€€€€€€€€€¹±¥µ¥Ð Ä¤(€€€€€€€€¤(€€€€€€€•ÉÉ½È¹É•ÑÉå…‰±”€ô€À(€€€…ÍÍ•ÉÐÍ•ÉÙ¥”¹É•½Ù•É}¥¹Ñ•ÉÉÕÁÑ• ¤€ôô€Ä(€€€É•Á…¥É•€ôÍ•ÉÙ¥”¹•Ñ}‰Õ¥±¡Ý…¥Ñ¥¹œ¹¥¤(€€€…ÍÍ•ÉÐÉ•Á…¥É•¹ÕÉÉ•¹Ñ}ÍÑ…”¥Ì]½É­™±½ÝMÑ…Ñ”¹%1(€€€…ÍÍ•ÉÐÉ•Á…¥É•¹Ù•ÉÍ¥½¸€ôô™…¥±•¹Ù•ÉÍ¥½¸(€€€…ÍÍ•ÉÐ€ (€€€€€€€¹•áÐ¡ÍÑ•À™½ÈÍÑ•À¥¸Í•ÉÙ¥”¹ÍÑ•ÁÌ¡Ý…¥Ñ¥¹œ¹¥¤¥˜ÍÑ•Ál‰¥‰t€ôôÍÑ…±”¹¥¥l‰ÍÑ…ÑÕÌ‰t(€€€€€€€€ôô€‰¥¹Ñ•ÉÉÕÁÑ•ˆ(€€€€¤(€€€…ÍÍ•ÉÐÍ•ÉÙ¥”¹•ÉÉ½ÉÌ¡Ý…¥Ñ¥¹œ¹¥¥l´Åul‰É•ÑÉå…‰±”‰t¥Ì…±Í”(€€€…ÍÍ•ÉÐ…¹ä (€€€€€€€•Ù•¹Ñl‰•Ù•¹Ñ}ÑåÁ”‰t€ôô€‰Ý½É­™±½Ü¹ÍÑ•À¹¥¹Ñ•ÉÉÕÁÑ•ˆ(€€€€€€€…¹•Ù•¹Ñl‰Á…å±½…‰ul‰É•Á…¥È‰t€ôô€‰‰…­•¹‘}É•ÍÑ…ÉÐˆ(€€€€€€€™½È•Ù•¹Ð¥¸Í•ÉÙ¥”¹Ñ¥µ•±¥¹”¡Ý…¥Ñ¥¹œ¹¥¤(€€€€¤(()‘•˜Ñ•ÍÑ}Í¡•µ…}Ù•ÉÍ¥½¹}¥Í}Ù…±¥‘…Ñ•‘}‘•™•¹Í¥Ù•±ä ¤€´ø9½¹”è(€€€…ÍÍ•ÉÐ±½…‘}Ù•ÉÍ¥½¹•‘}©Í½¸¡©Í½¸¹‘ÕµÁÌ¡ì‰Í¡•µ…}Ù•ÉÍ¥½¸ˆè€ˆÄ¸À¸Àˆ°€‰½¬ˆèQÉÕ•ô¤¥l‰½¬‰t(€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡]½É­™±½Ý½¹™±¥Ð¤è(€€€€€€€±½…‘}Ù•ÉÍ¥½¹•‘}©Í½¸¡©Í½¸¹‘ÕµÁÌ¡ì‰Í¡•µ…}Ù•ÉÍ¥½¸ˆè€ˆää¸À¸À‰ô¤¤(()‘•˜Ñ•ÍÑ}™½É•¥¹}­•å}•¹™½É•µ•¹Ñ}É•©•ÑÍ}½ÉÁ¡…¸¡ÑµÁ}Á…Ñ ¤€´ø9½¹”è(€€€‘…Ñ…‰…Í”€ô]½É­™±½Ý…Ñ…‰…Í”¡ÑµÁ}Á…Ñ €¼€‰™¬¹‘ˆˆ¤(€€€‘…Ñ…‰…Í”¹µ¥É…Ñ” ¤(€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡…Ñ…‰…Í•ÉÉ½È¤è(€€€€€€€Ý¥Ñ ‘…Ñ…‰…Í”¹•¹¥¹”¹‰•¥¸ ¤…Ì½¹¹•Ñ¥½¸è(€€€€€€€€€€€½¹¹•Ñ¥½¸¹•á•ÕÑ” (€€€€€€€€€€€€€€€Ñ•áÐ (€€€€€€€€€€€€€€€€€€€€‰%9MIP%9Q<Ý½É­™±½Ý}ÍÑ•ÁÌ€ˆ(€€€€€€€€€€€€€€€€€€€€ˆ¡¥±Ý½É­™±½Ý}¥±ÍÑ…”±…ÑÑ•µÁÐ±ÍÑ…ÑÕÌ±¥‘•µÁ½Ñ•¹å}­•ä±¥¹ÁÕÑ}©Í½¸¤€ˆ(€€€€€€€€€€€€€€€€€€€€‰Y1UL€ ÍÑ•Àµ½ÉÁ¡…¸œ°µ¥ÍÍ¥¹œœ°IPœ°Ä°ÉÕ¹¹¥¹œœ°­•äœ°ˆ(€€€€€€€€€€€€€€€€€€€€pì‰Í¡•µ…}Ù•ÉÍ¥½¸ˆèˆÄ¸À¸À‰õpœ¤œ(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€¤(€€€‘…Ñ…‰…Í”¹‘¥ÍÁ½Í” ¤(