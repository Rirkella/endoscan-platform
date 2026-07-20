"""Transactional workflow service. It owns workflow truth but no scientific logic."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .artifacts import LocalArtifactStore
from .config import AgentConfiguration
from .contracts import (
    SCHEMA_VERSION,
    ActorType,
    AgentRunStatus,
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalType,
    EndpointBuildCreate,
    StepStatus,
    TransitionRequest,
    WorkflowKind,
    WorkflowSnapshot,
    WorkflowState,
    WorkflowStatus,
)
from .database import WorkflowDatabase
from .discovery import DiscoveryOutput, discovery_request, validate_evidence_references
from .errors import (
    GuardNotSatisfied,
    InvalidTransition,
    SourceDiscoveryNotReady,
    StaleWorkflowVersion,
    WorkflowConflict,
    WorkflowNotFound,
)
from .harness import AgentHarness
from .models import (
    AgentRunRow,
    ApprovalRow,
    ArtifactRow,
    EndpointBuildRow,
    HumanDecisionRow,
    ToolCallRow,
    TrainingDatasetWorkflowRow,
    WorkflowErrorRow,
    WorkflowEventRow,
    WorkflowStepRow,
)
from .repository import (
    append_event,
    canonical_json,
    deterministic_id,
    load_versioned_json,
    parse_utc,
    require_build,
    utc_text,
    versioned_payload,
)
from .reviewed_source_adapters import ReviewedSourceAdapterRegistry
from .state_machine import TransitionSpec, WorkflowGraph
from .training_dataset import (
    BLIND_TRAINING_DATASET_DISCOVERY,
    SPECIALIZED_AGENT_SEQUENCE,
    TRAINING_DATASET_CONTRACT_VERSION,
    AssemblyGapReport,
    BlindBenchmarkInitialContext,
    DatasetSpecificationAgentOutcome,
    DatasetSpecificationCompiler,
    DatasetSpecificationReviewOutcome,
    DatasetSpecificationReviewRecord,
    DatasetSpecificationSemanticValidation,
    DiscoveryAgentReviewOutcome,
    DiscoveryBeforeStrategyGuard,
    SourceCapabilityMatrix,
    SpecializedAgentDefinition,
    TrainingDatasetAssemblyReview,
    TrainingDatasetComponentRequirements,
    TrainingDatasetPreparationPlan,
    TrainingDatasetSpecification,
    TrainingDatasetSpecificationApprovalPolicy,
    TrainingDatasetSpecificationDraft,
    VerifiedSourceInventory,
    VerifiedSourceInventoryFragment,
    VerifiedSourceObservation,
    VerifiedSourceObservationBatch,
    build_capability_matrix,
    build_source_assembly_gap_report,
    compile_verified_source_fragment,
    compile_verified_source_inventory,
    derive_component_requirements,
    derive_endpoint_request_semantic_hints,
    materialize_training_dataset_specification,
    specialized_agent_request,
    validate_dataset_specification_semantics,
    validate_strategy_sources,
)

STATE_PROGRESS = {
    WorkflowState.DRAFT: 0,
    WorkflowState.COMPILING_TARGET_DATASET_SPECIFICATION: 2,
    WorkflowState.AWAITING_DATASET_SPECIFICATION_REVIEW: 6,
    WorkflowState.REVIEWING_DATASET_SPECIFICATION: 6,
    WorkflowState.SPECIFYING_TARGET_DATASET: 3,
    WorkflowState.AWAITING_DATASET_SPECIFICATION_REVISION: 5,
    WorkflowState.AWAITING_DATASET_SPECIFICATION_APPROVAL: 8,
    WorkflowState.DERIVING_COMPONENT_REQUIREMENTS: 11,
    WorkflowState.DISCOVERING_ACTIVITY_EVIDENCE: 16,
    WorkflowState.DISCOVERING_TRANSCRIPTOMIC_EVIDENCE: 23,
    WorkflowState.DISCOVERING_IDENTITY_AND_STRUCTURE_SOURCES: 30,
    WorkflowState.DISCOVERING_SUPPORTING_METADATA: 36,
    WorkflowState.VALIDATING_DISCOVERED_SOURCES: 42,
    WorkflowState.BUILDING_SOURCE_INVENTORY: 49,
    WorkflowState.AWAITING_SOURCE_INVENTORY_REVIEW: 55,
    WorkflowState.AWAITING_SOURCE_DISCOVERY_REVISION: 55,
    WorkflowState.SOURCE_ACCESS_BLOCKED: 42,
    WorkflowState.PLANNING_ASSEMBLY_STRATEGIES: 58,
    WorkflowState.EVALUATING_JOINABILITY: 66,
    WorkflowState.IDENTIFYING_ASSEMBLY_GAPS: 73,
    WorkflowState.GAP_DIRECTED_DISCOVERY: 77,
    WorkflowState.COMPARING_ASSEMBLY_STRATEGIES: 85,
    WorkflowState.AWAITING_ASSEMBLY_STRATEGY_APPROVAL: 94,
    WorkflowState.DISCOVERING_DATA: 8,
    WorkflowState.AWAITING_DATASET_APPROVAL: 15,
    WorkflowState.AWAITING_SEARCH_REVIEW: 15,
    WorkflowState.CURATING_DATA: 22,
    WorkflowState.AWAITING_LABEL_APPROVAL: 30,
    WorkflowState.RESOLVING_IDENTITIES: 38,
    WorkflowState.AUDITING_DATASET: 46,
    WorkflowState.AUDITING_LEAKAGE: 54,
    WorkflowState.AWAITING_TRAINING_APPROVAL: 62,
    WorkflowState.TRAINING: 70,
    WorkflowState.EVALUATING: 78,
    WorkflowState.AWAITING_SCIENTIFIC_APPROVAL: 86,
    WorkflowState.REGISTERING: 94,
    WorkflowState.COMPLETED: 100,
    WorkflowState.FAILED: 0,
    WorkflowState.PAUSED: 0,
    WorkflowState.CANCELLED: 0,
}

APPROVAL_ARTIFACT_TYPES = {
    "dataset_specification_approval": ApprovalType.DATASET_SPECIFICATION,
    "dataset_selection_approval": ApprovalType.DATASET_SELECTION,
    "label_rules_approval": ApprovalType.LABEL_RULES,
    "training_approval": ApprovalType.TRAINING_AUTHORIZATION,
    "model_acceptance_approval": ApprovalType.MODEL_ACCEPTANCE,
    "registration_approval": ApprovalType.REGISTRY_PUBLICATION,
    "identity_conflict_decisions": ApprovalType.IDENTITY_CONFLICT,
    "training_dataset_assembly_strategy_approval": (
        ApprovalType.TRAINING_DATASET_ASSEMBLY_STRATEGY
    ),
}


def _load_training_document(raw: str) -> dict:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise WorkflowConflict("Persisted training-dataset document is not an object.")
    if value.get("schema_version") == SCHEMA_VERSION and isinstance(value.get("document"), dict):
        return value["document"]
    return value


class WorkflowService:
    def __init__(
        self,
        database: WorkflowDatabase,
        artifact_store: LocalArtifactStore,
        graph: WorkflowGraph,
        *,
        repo_root: Path,
        harness: AgentHarness | None = None,
        agent_configuration: AgentConfiguration | None = None,
        reviewed_source_adapters: ReviewedSourceAdapterRegistry | None = None,
        maximum_workflows: int = 100,
    ):
        self.database = database
        self.artifact_store = artifact_store
        self.graph = graph
        self.repo_root = Path(repo_root).resolve()
        self.harness = harness
        self.agent_configuration = agent_configuration or AgentConfiguration()
        self.reviewed_source_adapters = reviewed_source_adapters or ReviewedSourceAdapterRegistry()
        self.maximum_workflows = maximum_workflows

    def create_build(self, request: EndpointBuildCreate) -> WorkflowSnapshot:
        build_id = deterministic_id("build", request.idempotency_key)
        payload_json = request.model_dump_json()
        with self.database.session() as session:
            existing = session.get(EndpointBuildRow, build_id)
            if existing is not None:
                if existing.payload_json != payload_json:
                    raise WorkflowConflict(
                        "Creation idempotency key was already used with different input."
                    )
                return self._snapshot(session, existing)
            count = int(session.scalar(select(func.count(EndpointBuildRow.id))) or 0)
            if count >= self.maximum_workflows:
                raise WorkflowConflict("The configured Phase-0 workflow limit has been reached.")
            now = utc_text()
            build = EndpointBuildRow(
                id=build_id,
                endpoint_name=request.endpoint_name,
                endpoint_slug=request.endpoint_slug,
                biological_goal=request.biological_goal,
                workflow_kind=request.workflow_kind.value,
                benchmark_mode=request.benchmark_mode,
                status=WorkflowStatus.DRAFT.value,
                current_stage=WorkflowState.DRAFT.value,
                paused_from_state=None,
                failed_from_state=None,
                created_by=request.created_by,
                version=0,
                created_at=now,
                updated_at=now,
                paused_at=None,
                cancelled_at=None,
                completed_at=None,
                payload_json=payload_json,
            )
            session.add(build)
            session.flush()
            append_event(
                session,
                build,
                event_type="workflow.created",
                actor_type=ActorType.HUMAN.value,
                actor_id=request.created_by,
                idempotency_key=f"{request.idempotency_key}:created",
                payload={
                    "endpoint_name": request.endpoint_name,
                    "endpoint_slug": request.endpoint_slug,
                    "biological_goal": request.biological_goal,
                    "workflow_kind": request.workflow_kind.value,
                    "benchmark_mode": request.benchmark_mode,
                },
                to_state=WorkflowState.DRAFT.value,
            )
            definition = self.artifact_store._put_bytes(
                session,
                workflow_id=build.id,
                content=canonical_json(
                    versioned_payload(
                        endpoint_name=request.endpoint_name,
                        endpoint_slug=request.endpoint_slug,
                        biological_goal=request.biological_goal,
                        workflow_kind=request.workflow_kind.value,
                        benchmark_mode=request.benchmark_mode,
                        limitations=[
                            "Phase 0 defines workflow scope only; it does not create a "
                            "scientific model.",
                            "The prepared discovery simulation is not live dataset discovery.",
                        ],
                    )
                ).encode(),
                mime_type="application/json",
                artifact_type="endpoint_definition",
                logical_name="endpoint-definition-v1.json",
                producer="admin",
                idempotency_key=f"{request.idempotency_key}:definition",
            )
            if request.workflow_kind is WorkflowKind.TRAINING_DATASET_DISCOVERY:
                initial_context = self._training_dataset_initial_context(request)
                semantic_hints = initial_context.endpoint_request_semantic_hints
                if semantic_hints is None:  # defensive; new contexts always contain hints
                    semantic_hints = derive_endpoint_request_semantic_hints(
                        request.endpoint_name,
                        request.biological_goal,
                    )
                self.artifact_store._put_bytes(
                    session,
                    workflow_id=build.id,
                    content=json.dumps(
                        semantic_hints.model_dump(mode="json"),
                        indent=2,
                        sort_keys=True,
                        ensure_ascii=False,
                    ).encode(),
                    mime_type="application/json",
                    artifact_type="endpoint_request_semantic_hints",
                    logical_name="endpoint-request-semantic-hints-v1.json",
                    producer="deterministic-orchestrator",
                    idempotency_key=f"{request.idempotency_key}:semantic-hints",
                )
                self.artifact_store._put_bytes(
                    session,
                    workflow_id=build.id,
                    content=canonical_json(initial_context.model_dump(mode="json")).encode(),
                    mime_type="application/json",
                    artifact_type="blind_context_audit",
                    logical_name="blind-training-dataset-context-v1.json",
                    producer="deterministic-orchestrator",
                    idempotency_key=f"{request.idempotency_key}:blind-context",
                )
                now = utc_text()
                session.add(
                    TrainingDatasetWorkflowRow(
                        workflow_id=build.id,
                        contract_version=TRAINING_DATASET_CONTRACT_VERSION,
                        benchmark_mode=request.benchmark_mode,
                        initial_context_json=canonical_json(
                            versioned_payload(document=initial_context.model_dump(mode="json"))
                        ),
                        specification_draft_json=None,
                        specification_outcome_json=None,
                        specification_semantic_validation_json=None,
                        specification_compilation_outcome_json=None,
                        specification_review_record_json=None,
                        specification_json=None,
                        component_requirements_json=None,
                        source_inventory_json=None,
                        capability_matrix_json=None,
                        assembly_strategies_json=None,
                        joinability_diagnostics_json=None,
                        gap_report_json=None,
                        preparation_plan_json=None,
                        assembly_review_json=None,
                        discovery_round=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
            approval_request = ApprovalRequest(
                workflow_id=build.id,
                stage=WorkflowState.DRAFT,
                approval_type=ApprovalType.ENDPOINT_DEFINITION,
                proposed_decision=f"Freeze endpoint definition for {request.endpoint_name}.",
                evidence_summary=request.biological_goal,
                limitations=[
                    "Creation is the explicit Phase-0 local-admin endpoint-definition decision."
                ],
                artifact_hashes=[definition.sha256],
                agent_recommendation="No agent recommendation; administrator-authored definition.",
                requested_action=(
                    "Start target training-dataset specification."
                    if request.workflow_kind is WorkflowKind.TRAINING_DATASET_DISCOVERY
                    else "Start bounded prepared discovery simulation."
                ),
            )
            approval = self._create_approval(
                session,
                build,
                approval_request,
                idempotency_key=f"{request.idempotency_key}:definition-approval",
            )
            decision = ApprovalDecision(
                decision=ApprovalDecisionValue.APPROVE,
                reviewer_id=request.created_by,
                reviewer_comment="Approved by explicit endpoint creation action.",
                expected_version=0,
                idempotency_key=f"{request.idempotency_key}:definition-decision",
                artifact_hashes=[definition.sha256],
            )
            self._record_decision(session, build, approval, decision, advance=False)
            return self._snapshot(session, build)

    def _training_dataset_initial_context(
        self, request: EndpointBuildCreate
    ) -> BlindBenchmarkInitialContext:
        semantic_hints = derive_endpoint_request_semantic_hints(
            request.endpoint_name,
            request.biological_goal,
        )
        return BlindBenchmarkInitialContext(
            benchmark_mode=(
                BLIND_TRAINING_DATASET_DISCOVERY
                if request.benchmark_mode == BLIND_TRAINING_DATASET_DISCOVERY
                else request.benchmark_mode
            ),
            endpoint_name=request.endpoint_name,
            biological_goal=request.biological_goal,
            target_training_dataset_contract={
                "required_relationship": [
                    "canonical chemical compound",
                    "chemical structure and identifiers",
                    "compound-induced transcriptomic response",
                    "transcriptomic experimental context",
                    "endpoint activity measurement or label",
                    "activity assay context",
                    "provenance and quality flags",
                ],
                "strategy_generation_allowed": False,
                "strategy_unlock_artifacts": [
                    "verified_source_inventory",
                    "source_capability_matrix",
                ],
            },
            source_adapter_capabilities=[
                "official activity-source metadata adapters",
                "official perturbational-transcriptomic metadata adapters",
                "official chemical identity and structure metadata adapters",
            ],
            approved_scientific_policies=[
                "discovery before strategy",
                "primary public records over literature summaries",
                "exact values only from deterministic computation",
                "human approval before deterministic construction",
            ],
            allowed_tools=sorted(
                {tool for agent in SPECIALIZED_AGENT_SEQUENCE for tool in agent.allowed_tools}
            ),
            planner_provider=self.agent_configuration.planner_provider,
            planner_model=self.agent_configuration.planner_model,
            worker_provider=self.agent_configuration.worker_provider,
            worker_model=self.agent_configuration.worker_model,
            endpoint_request_semantic_hints=semantic_hints,
            budgets={
                "per_agent": {
                    "maximum_turns": self.agent_configuration.maximum_turns,
                    "maximum_tool_calls": self.agent_configuration.maximum_tool_calls,
                    "maximum_input_tokens": self.agent_configuration.maximum_input_tokens,
                    "maximum_output_tokens": self.agent_configuration.maximum_output_tokens,
                    "maximum_cost_usd": self.agent_configuration.maximum_cost_usd,
                    "timeout_seconds": self.agent_configuration.timeout_seconds,
                    "provider_retries": self.agent_configuration.retry_count,
                },
                "global": {
                    "maximum_input_tokens": (self.agent_configuration.global_maximum_input_tokens),
                    "maximum_output_tokens": (
                        self.agent_configuration.global_maximum_output_tokens
                    ),
                    "maximum_tool_calls": self.agent_configuration.global_maximum_tool_calls,
                    "maximum_cost_usd": self.agent_configuration.global_maximum_cost_usd,
                    "maximum_gap_discovery_rounds": (
                        self.agent_configuration.maximum_gap_discovery_rounds
                    ),
                    "timeout_seconds": self.agent_configuration.global_timeout_seconds,
                },
            },
        )

    def training_dataset_workflow(self, workflow_id: str) -> dict:
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None:
                return {
                    "schema_version": SCHEMA_VERSION,
                    "workflow_id": workflow_id,
                    "workflow_kind": build.workflow_kind,
                    "legacy": True,
                    "label": "Legacy single-source discovery",
                }
            return {
                "schema_version": SCHEMA_VERSION,
                "contract_version": row.contract_version,
                "workflow_id": workflow_id,
                "workflow_kind": build.workflow_kind,
                "benchmark_mode": row.benchmark_mode,
                "legacy": False,
                "initial_context": _load_training_document(row.initial_context_json),
                "target_specification": (
                    _load_training_document(row.specification_json)
                    if row.specification_json
                    else None
                ),
                "specification_draft": (
                    _load_training_document(row.specification_draft_json)
                    if row.specification_draft_json
                    else None
                ),
                "specification_agent_outcome": (
                    _load_training_document(row.specification_outcome_json)
                    if row.specification_outcome_json
                    else None
                ),
                "specification_semantic_validation": (
                    _load_training_document(row.specification_semantic_validation_json)
                    if row.specification_semantic_validation_json
                    else None
                ),
                "specification_compilation_outcome": (
                    _load_training_document(row.specification_compilation_outcome_json)
                    if row.specification_compilation_outcome_json
                    else None
                ),
                "specification_review": (
                    _load_training_document(row.specification_review_record_json)
                    if row.specification_review_record_json
                    else None
                ),
                "component_requirements": (
                    _load_training_document(row.component_requirements_json)
                    if row.component_requirements_json
                    else None
                ),
                "source_discovery_authorization": (
                    _load_training_document(row.source_discovery_authorization_json)
                    if row.source_discovery_authorization_json
                    else None
                ),
                "source_discovery_budget": (
                    _load_training_document(row.source_discovery_budget_json)
                    if row.source_discovery_budget_json
                    else None
                ),
                "source_observations": (
                    _load_training_document(row.source_observations_json).get("items", [])
                    if row.source_observations_json
                    else []
                ),
                "source_fragments": (
                    _load_training_document(row.source_fragments_json).get("items", [])
                    if row.source_fragments_json
                    else []
                ),
                "verified_source_inventory": (
                    _load_training_document(row.source_inventory_json)
                    if row.source_inventory_json
                    else None
                ),
                "capability_matrix": (
                    _load_training_document(row.capability_matrix_json)
                    if row.capability_matrix_json
                    else None
                ),
                "assembly_strategies": (
                    _load_training_document(row.assembly_strategies_json)
                    if row.assembly_strategies_json
                    else None
                ),
                "joinability_diagnostics": (
                    _load_training_document(row.joinability_diagnostics_json)
                    if row.joinability_diagnostics_json
                    else None
                ),
                "gap_report": (
                    _load_training_document(row.gap_report_json) if row.gap_report_json else None
                ),
                "preparation_plan": (
                    _load_training_document(row.preparation_plan_json)
                    if row.preparation_plan_json
                    else None
                ),
                "assembly_review": (
                    _load_training_document(row.assembly_review_json)
                    if row.assembly_review_json
                    else None
                ),
                "planned_discovery_agents": self._planned_discovery_agents(),
                "source_discovery_readiness": self.source_discovery_readiness(workflow_id),
                "discovery_round": row.discovery_round,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }

    def _planned_discovery_agents(self) -> list[dict]:
        configuration = self.agent_configuration.controlled_source_discovery()
        adapter_labels = {
            "Activity Evidence Discovery Agent": [
                "official activity-source metadata adapters"
            ],
            "Transcriptomic Evidence Discovery Agent": [
                "official perturbational-transcriptomic metadata adapters"
            ],
            "Chemical Identity and Structure Source Discovery Agent": [
                "official chemical identity and structure metadata adapters"
            ],
            "Supporting Metadata Discovery Agent": [
                "official activity-source metadata adapters",
                "official perturbational-transcriptomic metadata adapters",
                "official chemical identity and structure metadata adapters",
            ],
        }
        names = set(adapter_labels)
        return [
            {
                "agent_name": item.agent_name,
                "provider": configuration.worker_provider,
                "model": configuration.worker_model,
                "allowed_tools": self.reviewed_source_adapters.filter_available_operations(
                    item.allowed_tools
                ),
                "allowed_official_source_adapters": adapter_labels[item.agent_name],
                "maximum_turns": configuration.maximum_turns,
                "maximum_tool_calls": configuration.maximum_tool_calls,
                "maximum_input_tokens": configuration.maximum_input_tokens,
                "maximum_output_tokens": configuration.maximum_output_tokens,
                "maximum_cost_usd": configuration.maximum_cost_usd,
                "timeout_seconds": configuration.timeout_seconds,
                "provider_retries": configuration.retry_count,
                "output_schema_name": item.output_schema_name,
            }
            for item in SPECIALIZED_AGENT_SEQUENCE
            if item.agent_name in names
        ]

    def persist_training_dataset_document(
        self,
        workflow_id: str,
        *,
        document_name: str,
        value: dict,
        actor: str,
        idempotency_key: str,
    ) -> dict:
        """Persist one validated checkpoint without changing workflow state."""

        document_types = {
            "specification": (
                TrainingDatasetSpecification,
                "specification_json",
                "training_dataset_specification",
            ),
            "component_requirements": (
                TrainingDatasetComponentRequirements,
                "component_requirements_json",
                "component_requirements",
            ),
            "source_inventory": (
                VerifiedSourceInventory,
                "source_inventory_json",
                "verified_source_inventory",
            ),
            "capability_matrix": (
                SourceCapabilityMatrix,
                "capability_matrix_json",
                "source_capability_matrix",
            ),
            "gap_report": (
                AssemblyGapReport,
                "gap_report_json",
                "assembly_gap_report",
            ),
            "preparation_plan": (
                TrainingDatasetPreparationPlan,
                "preparation_plan_json",
                "training_dataset_preparation_plan",
            ),
            "assembly_review": (
                TrainingDatasetAssemblyReview,
                "assembly_review_json",
                "assembly_strategy_comparison",
            ),
        }
        if document_name not in document_types:
            raise WorkflowConflict("Unsupported training-dataset document type.")
        model, column, artifact_type = document_types[document_name]
        document = model.model_validate(value)
        payload = document.model_dump(mode="json")
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            if build.workflow_kind != WorkflowKind.TRAINING_DATASET_DISCOVERY.value:
                raise WorkflowConflict("Legacy workflows cannot store training-dataset documents.")
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None:
                raise WorkflowNotFound("Training-dataset workflow state was not found.")
            artifact = self.artifact_store._put_bytes(
                session,
                workflow_id=workflow_id,
                content=canonical_json(payload).encode(),
                mime_type="application/json",
                artifact_type=artifact_type,
                logical_name=f"{artifact_type}-{document_name}-v1.json",
                producer=actor,
                idempotency_key=idempotency_key,
            )
            setattr(row, column, canonical_json(versioned_payload(document=payload)))
            row.updated_at = utc_text()
            append_event(
                session,
                build,
                event_type="training_dataset.document.persisted",
                actor_type=ActorType.ORCHESTRATOR.value,
                actor_id=actor,
                idempotency_key=f"{idempotency_key}:event",
                payload={
                    "document_name": document_name,
                    "artifact_type": artifact_type,
                    "artifact_id": artifact.id,
                    "sha256": artifact.sha256,
                },
                from_state=build.current_stage,
                to_state=build.current_stage,
            )
            return {
                "document_name": document_name,
                "artifact_id": artifact.id,
                "sha256": artifact.sha256,
            }

    def finalize_training_dataset_specification(
        self,
        workflow_id: str,
        *,
        specification: TrainingDatasetSpecification,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Checkpoint agent output and pause before any source discovery."""

        payload = specification.model_dump(mode="json")
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            if build.current_stage != WorkflowState.SPECIFYING_TARGET_DATASET.value:
                raise InvalidTransition("Specification can be finalized only in its active stage.")
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None:
                raise WorkflowNotFound("Training-dataset workflow state was not found.")
            artifact = self.artifact_store._put_bytes(
                session,
                workflow_id=workflow_id,
                content=canonical_json(payload).encode(),
                mime_type="application/json",
                artifact_type="training_dataset_specification",
                logical_name=f"training-dataset-specification-{specification.specification_id}.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:artifact",
            )
            row.specification_json = canonical_json(versioned_payload(document=payload))
            row.updated_at = utc_text()
            snapshot = self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.AWAITING_DATASET_SPECIFICATION_APPROVAL,
                    expected_version=expected_version,
                    idempotency_key=f"{idempotency_key}:transition",
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id=actor,
                    reason="Strict target training-dataset specification is ready for review.",
                    artifact_hashes=[artifact.sha256],
                ),
            )
            self._create_approval(
                session,
                build,
                ApprovalRequest(
                    workflow_id=workflow_id,
                    stage=WorkflowState.AWAITING_DATASET_SPECIFICATION_APPROVAL,
                    approval_type=ApprovalType.DATASET_SPECIFICATION,
                    proposed_decision="Approve the target training-dataset contract.",
                    evidence_summary=specification.endpoint_definition,
                    limitations=specification.unresolved_questions,
                    artifact_hashes=[artifact.sha256],
                    agent_recommendation=(
                        "Review prediction grain, evidence representations, missingness, and "
                        "assumptions before source discovery."
                    ),
                    requested_action=(
                        "Approve, reject, or request a revised target specification."
                    ),
                ),
                idempotency_key=f"{idempotency_key}:approval",
            )
            return snapshot

    def derive_training_dataset_requirements(
        self, workflow_id: str, *, actor: str, idempotency_key: str
    ) -> dict:
        with self.database.session() as session:
            require_build(session, workflow_id)
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None or not row.specification_json:
                raise GuardNotSatisfied("Approved training-dataset specification is required.")
            specification = TrainingDatasetSpecification.model_validate(
                _load_training_document(row.specification_json)
            )
        requirements = derive_component_requirements(specification)
        stored = self.persist_training_dataset_document(
            workflow_id,
            document_name="component_requirements",
            value=requirements.model_dump(mode="json"),
            actor=actor,
            idempotency_key=idempotency_key,
        )
        return stored

    def validate_training_dataset_strategy_checkpoint(self, workflow_id: str) -> None:
        """Enforce discovery-before-strategy against the durable current inventory."""

        with self.database.session() as session:
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None:
                raise WorkflowNotFound("Training-dataset workflow state was not found.")
            specification = (
                TrainingDatasetSpecification.model_validate(
                    _load_training_document(row.specification_json)
                )
                if row.specification_json
                else None
            )
            requirements = (
                TrainingDatasetComponentRequirements.model_validate(
                    _load_training_document(row.component_requirements_json)
                )
                if row.component_requirements_json
                else None
            )
            inventory = (
                VerifiedSourceInventory.model_validate(
                    _load_training_document(row.source_inventory_json)
                )
                if row.source_inventory_json
                else None
            )
            matrix = (
                SourceCapabilityMatrix.model_validate(
                    _load_training_document(row.capability_matrix_json)
                )
                if row.capability_matrix_json
                else None
            )
            DiscoveryBeforeStrategyGuard.validate(specification, requirements, inventory, matrix)
            if row.assembly_review_json:
                review = TrainingDatasetAssemblyReview.model_validate(
                    _load_training_document(row.assembly_review_json)
                )
                validate_strategy_sources(review.strategies, inventory)

    def finalize_training_dataset_assembly_review(
        self,
        workflow_id: str,
        *,
        review: TrainingDatasetAssemblyReview,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Persist a fully bound review and create the narrow assembly approval gate."""

        self.validate_training_dataset_strategy_checkpoint(workflow_id)
        selected = next(
            (
                item
                for item in review.strategies
                if item.strategy_id == review.recommended_strategy_id
            ),
            None,
        )
        if selected is None:
            raise GuardNotSatisfied(
                "A recommended verified strategy is required for assembly approval."
            )
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            if build.current_stage != WorkflowState.COMPARING_ASSEMBLY_STRATEGIES.value:
                raise InvalidTransition("Assembly review is not the active workflow stage.")
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None:
                raise WorkflowNotFound("Training-dataset workflow state was not found.")
            documents = {
                "assembly_strategy_comparison": review.model_dump(mode="json"),
                "selected_source_graph": selected.source_graph.model_dump(mode="json"),
                "identity_policy": {
                    "strategy_id": selected.strategy_id,
                    "policy": selected.identity_policy,
                    "chemical_standardization_policy": (selected.chemical_standardization_policy),
                },
                "label_policy": {
                    "strategy_id": selected.strategy_id,
                    "policy": selected.label_policy,
                },
                "transcriptomic_condition_policy": {
                    "strategy_id": selected.strategy_id,
                    "policy": selected.transcriptomic_condition_policy,
                    "repeated_signature_policy": selected.repeated_signature_policy,
                },
                "joinability_diagnostic": selected.overlap_diagnostic.model_dump(mode="json"),
                "training_dataset_preparation_plan": (
                    selected.preparation_plan.model_dump(mode="json")
                ),
            }
            artifacts = []
            for name, value in documents.items():
                artifacts.append(
                    self.artifact_store._put_bytes(
                        session,
                        workflow_id=workflow_id,
                        content=canonical_json(value).encode(),
                        mime_type="application/json",
                        artifact_type=name,
                        logical_name=f"{name}-{selected.strategy_id}.json",
                        producer=actor,
                        idempotency_key=f"{idempotency_key}:{name}",
                    )
                )
            row.assembly_strategies_json = canonical_json(
                versioned_payload(
                    document={
                        "strategies": [item.model_dump(mode="json") for item in review.strategies]
                    }
                )
            )
            row.joinability_diagnostics_json = canonical_json(
                versioned_payload(
                    document={
                        "diagnostics": [
                            item.overlap_diagnostic.model_dump(mode="json")
                            for item in review.strategies
                        ]
                    }
                )
            )
            row.preparation_plan_json = canonical_json(
                versioned_payload(document=selected.preparation_plan.model_dump(mode="json"))
            )
            row.assembly_review_json = canonical_json(
                versioned_payload(document=review.model_dump(mode="json"))
            )
            row.updated_at = utc_text()
            foundational_types = {
                "training_dataset_specification",
                "component_requirements",
                "verified_source_inventory",
                "source_capability_matrix",
            }
            foundational = session.scalars(
                select(ArtifactRow).where(
                    ArtifactRow.workflow_id == workflow_id,
                    ArtifactRow.artifact_type.in_(foundational_types),
                )
            ).all()
            latest_by_type = {item.artifact_type: item for item in foundational}
            missing = sorted(foundational_types - set(latest_by_type))
            if missing:
                raise GuardNotSatisfied(
                    "Assembly approval prerequisites are missing.", detail={"missing": missing}
                )
            approval_hashes = sorted(
                {item.sha256 for item in [*latest_by_type.values(), *artifacts]}
            )
            snapshot = self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.AWAITING_ASSEMBLY_STRATEGY_APPROVAL,
                    expected_version=expected_version,
                    idempotency_key=f"{idempotency_key}:transition",
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id=actor,
                    reason="Verified assembly graph and preparation plan are ready for review.",
                    artifact_hashes=approval_hashes,
                ),
            )
            self._create_approval(
                session,
                build,
                ApprovalRequest(
                    workflow_id=workflow_id,
                    stage=WorkflowState.AWAITING_ASSEMBLY_STRATEGY_APPROVAL,
                    approval_type=ApprovalType.TRAINING_DATASET_ASSEMBLY_STRATEGY,
                    proposed_decision=f"Approve verified strategy {selected.strategy_id}.",
                    evidence_summary=review.decision_summary,
                    source_references=review.evidence_used,
                    limitations=[
                        *selected.scientific_risks,
                        *selected.technical_risks,
                        *review.unresolved_questions,
                    ],
                    artifact_hashes=approval_hashes,
                    agent_recommendation=review.decision_summary,
                    requested_action=(
                        "Approve this verified source graph and preparation strategy for "
                        "deterministic construction of a candidate training dataset."
                    ),
                ),
                idempotency_key=f"{idempotency_key}:approval",
            )
            return snapshot

    def list_builds(self) -> list[WorkflowSnapshot]:
        with self.database.session() as session:
            rows = session.scalars(
                select(EndpointBuildRow).order_by(
                    EndpointBuildRow.updated_at.desc(), EndpointBuildRow.id
                )
            ).all()
            return [self._snapshot(session, row) for row in rows]

    def get_build(self, workflow_id: str) -> WorkflowSnapshot:
        with self.database.session() as session:
            return self._snapshot(session, require_build(session, workflow_id))

    def source_discovery_readiness(self, workflow_id: str) -> dict:
        configuration = self.agent_configuration.controlled_source_discovery()
        adapter_readiness = self.reviewed_source_adapters.readiness()
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            approval = session.scalar(
                select(ApprovalRow).where(
                    ApprovalRow.workflow_id == workflow_id,
                    ApprovalRow.approval_type == ApprovalType.DATASET_SPECIFICATION.value,
                    ApprovalRow.status.in_(
                        [
                            ApprovalStatus.APPROVED.value,
                            ApprovalStatus.ALTERNATIVE_SELECTED.value,
                        ]
                    ),
                )
            )
            specification_ready = bool(row and row.specification_json and approval)
            requirements_ready = bool(row and row.component_requirements_json)
            stage_ready = build.current_stage == WorkflowState.DERIVING_COMPONENT_REQUIREMENTS.value
        provider_ready = (
            configuration.run_mode.value == "live"
            and configuration.worker_provider == "openai"
            and configuration.api_key_present
            and configuration.retry_count == 0
        )
        budget = configuration.public_status()["controlled_source_discovery_budget"]
        return {
            "schema_version": SCHEMA_VERSION,
            "ready": bool(
                provider_ready
                and adapter_readiness["ready"]
                and specification_ready
                and requirements_ready
                and stage_ready
            ),
            "provider_ready": provider_ready,
            "provider": configuration.worker_provider,
            "mode": configuration.run_mode.value,
            "api_key_present": configuration.api_key_present,
            "provider_retries": configuration.retry_count,
            "reviewed_adapters": adapter_readiness,
            "reviewed_adapter_inventory": self.reviewed_source_adapters.public_inventory(),
            "epa_authenticated_api": adapter_readiness["epa_authenticated_api"],
            "epa_public_data_releases": adapter_readiness["epa_public_data_releases"],
            "lincs_public_releases": adapter_readiness["lincs_public_releases"],
            "specification_approved": specification_ready,
            "component_requirements_present": requirements_ready,
            "stage_ready": stage_ready,
            "budget": budget,
        }

    def authorize_source_discovery(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        actor: str,
        idempotency_key: str,
        confirmed: bool,
    ) -> WorkflowSnapshot:
        """Record an explicit, fail-closed source-discovery authorization."""

        if not confirmed:
            raise SourceDiscoveryNotReady(
                "Explicit source-discovery confirmation is required."
            )
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            existing = session.scalar(
                select(WorkflowEventRow).where(
                    WorkflowEventRow.workflow_id == workflow_id,
                    WorkflowEventRow.idempotency_key == f"{idempotency_key}:authorized",
                )
            )
            if existing is not None:
                return self._snapshot(session, build)
            prior_authorization = session.scalar(
                select(WorkflowEventRow).where(
                    WorkflowEventRow.workflow_id == workflow_id,
                    WorkflowEventRow.event_type == "source_discovery.authorized",
                )
            )
            if prior_authorization is not None:
                raise WorkflowConflict(
                    "Source discovery was already authorized; reuse the original idempotency key."
                )
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
        readiness = self.source_discovery_readiness(workflow_id)
        if not readiness["ready"]:
            raise SourceDiscoveryNotReady(
                "Source discovery is not ready.",
                detail={
                    key: value
                    for key, value in readiness.items()
                    if key not in {"schema_version", "budget"}
                },
            )
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            existing = session.scalar(
                select(WorkflowEventRow).where(
                    WorkflowEventRow.workflow_id == workflow_id,
                    WorkflowEventRow.idempotency_key == f"{idempotency_key}:authorized",
                )
            )
            if existing is not None:
                return self._snapshot(session, build)
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            if build.current_stage != WorkflowState.DERIVING_COMPONENT_REQUIREMENTS.value:
                raise InvalidTransition(
                    "Source discovery can be authorized only after component requirements."
                )
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None or not row.component_requirements_json or not row.specification_json:
                raise GuardNotSatisfied("Approved specification and requirements are required.")
            requirement_artifact = session.scalar(
                select(ArtifactRow)
                .where(
                    ArtifactRow.workflow_id == workflow_id,
                    ArtifactRow.artifact_type == "component_requirements",
                )
                .order_by(ArtifactRow.created_at.desc())
            )
            if requirement_artifact is None:
                raise GuardNotSatisfied("Persisted component requirements artifact is missing.")
            now = utc_text()
            authorization = {
                "authorized": True,
                "authorized_by": actor,
                "authorized_at": now,
                "idempotency_key": idempotency_key,
                "adapter_registry": readiness["reviewed_adapters"],
            }
            row.source_discovery_authorization_json = canonical_json(
                versioned_payload(document=authorization)
            )
            row.source_discovery_budget_json = canonical_json(
                versioned_payload(document=readiness["budget"])
            )
            row.updated_at = now
            append_event(
                session,
                build,
                event_type="source_discovery.authorized",
                actor_type=ActorType.HUMAN.value,
                actor_id=actor,
                idempotency_key=f"{idempotency_key}:authorized",
                payload={
                    "adapter_registry_fingerprint": hashlib.sha256(
                        canonical_json(readiness["reviewed_adapters"]).encode()
                    ).hexdigest(),
                    "budget": readiness["budget"],
                },
                from_state=build.current_stage,
                to_state=build.current_stage,
            )
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.DISCOVERING_ACTIVITY_EVIDENCE,
                    expected_version=expected_version,
                    idempotency_key=f"{idempotency_key}:transition",
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id=actor,
                    reason="Human explicitly authorized reviewed source discovery.",
                    artifact_hashes=[requirement_artifact.sha256],
                ),
            )

    def _persist_tool_observations(
        self,
        workflow_id: str,
        *,
        step_id: str,
        agent_name: str,
        tool_call_id: str,
        tool_name: str,
        batch: VerifiedSourceObservationBatch,
    ) -> None:
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None:
                raise WorkflowNotFound("Training-dataset workflow state was not found.")
            current = (
                _load_training_document(row.source_observations_json).get("items", [])
                if row.source_observations_json
                else []
            )
            known = {
                item["observation"]["observation_id"]
                for item in current
                if isinstance(item, dict) and isinstance(item.get("observation"), dict)
            }
            for observation in batch.observations:
                if observation.observation_id in known:
                    continue
                payload = observation.model_dump(mode="json")
                artifact = self.artifact_store._put_bytes(
                    session,
                    workflow_id=workflow_id,
                    step_id=step_id,
                    content=canonical_json(payload).encode(),
                    mime_type="application/json",
                    artifact_type="verified_source_observation",
                    logical_name=f"verified-source-observation-{observation.observation_id}.json",
                    producer=f"{batch.adapter_id}@{batch.adapter_version}",
                    idempotency_key=f"observation:{observation.observation_id}",
                )
                current.append(
                    {
                        "agent_name": agent_name,
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "observation_artifact_id": artifact.id,
                        "observation_artifact_hash": artifact.sha256,
                        "observation": payload,
                    }
                )
                known.add(observation.observation_id)
            row.source_observations_json = canonical_json(
                versioned_payload(document={"items": current})
            )
            row.updated_at = utc_text()
            append_event(
                session,
                build,
                event_type="source_observations.persisted",
                actor_type=ActorType.TOOL.value,
                actor_id=tool_name,
                idempotency_key=f"observations:{tool_call_id}",
                payload={
                    "agent_name": agent_name,
                    "tool_call_id": tool_call_id,
                    "observation_ids": [item.observation_id for item in batch.observations],
                    "source_request_artifact_ids": batch.source_request_artifact_ids,
                },
                from_state=build.current_stage,
                to_state=build.current_stage,
            )

    def _agent_observations(
        self, workflow_id: str, agent_name: str
    ) -> list[VerifiedSourceObservation]:
        with self.database.session() as session:
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None or not row.source_observations_json:
                return []
            items = _load_training_document(row.source_observations_json).get("items", [])
        return [
            VerifiedSourceObservation.model_validate(item["observation"])
            for item in items
            if item.get("agent_name") == agent_name and isinstance(item.get("observation"), dict)
        ]

    def _persist_source_fragment(
        self,
        workflow_id: str,
        *,
        definition: SpecializedAgentDefinition,
        step_id: str,
        fragment: VerifiedSourceInventoryFragment,
    ) -> str:
        payload = fragment.model_dump(mode="json")
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None:
                raise WorkflowNotFound("Training-dataset workflow state was not found.")
            fragments = (
                _load_training_document(row.source_fragments_json).get("items", [])
                if row.source_fragments_json
                else []
            )
            existing = next(
                (item for item in fragments if item.get("agent_name") == definition.agent_name),
                None,
            )
            if existing is not None:
                return str(existing["artifact_hash"])
            artifact = self.artifact_store._put_bytes(
                session,
                workflow_id=workflow_id,
                step_id=step_id,
                content=canonical_json(payload).encode(),
                mime_type="application/json",
                artifact_type=definition.produces_artifact,
                logical_name=f"{definition.produces_artifact}-v1.json",
                producer="deterministic-source-fragment-compiler",
                idempotency_key=f"fragment:{fragment.fragment_id}",
            )
            fragments.append(
                {
                    "agent_name": definition.agent_name,
                    "artifact_id": artifact.id,
                    "artifact_hash": artifact.sha256,
                    "fragment": payload,
                }
            )
            row.source_fragments_json = canonical_json(
                versioned_payload(document={"items": fragments})
            )
            row.updated_at = utc_text()
            append_event(
                session,
                build,
                event_type="source_fragment.compiled",
                actor_type=ActorType.ORCHESTRATOR.value,
                actor_id="deterministic-source-fragment-compiler",
                idempotency_key=f"fragment:{fragment.fragment_id}:event",
                payload={
                    "agent_name": definition.agent_name,
                    "fragment_id": fragment.fragment_id,
                    "artifact_id": artifact.id,
                    "artifact_hash": artifact.sha256,
                    "agent_review_status": fragment.agent_review_status.value,
                    "agent_terminal_outcome": fragment.agent_terminal_outcome,
                },
                from_state=build.current_stage,
                to_state=build.current_stage,
            )
            return artifact.sha256

    def _assert_source_discovery_budget(self, workflow_id: str, agent_name: str) -> None:
        configuration = self.agent_configuration.controlled_source_discovery()
        names = {
            item.agent_name
            for item in SPECIALIZED_AGENT_SEQUENCE
            if item.agent_name
            in {
                "Activity Evidence Discovery Agent",
                "Transcriptomic Evidence Discovery Agent",
                "Chemical Identity and Structure Source Discovery Agent",
                "Supporting Metadata Discovery Agent",
            }
        }
        with self.database.session() as session:
            runs = session.scalars(
                select(AgentRunRow).where(
                    AgentRunRow.workflow_id == workflow_id,
                    AgentRunRow.agent_name.in_(names),
                )
            ).all()
            duplicate_names = [
                name for name in names if sum(item.agent_name == name for item in runs) > 1
            ]
            if duplicate_names:
                raise WorkflowConflict("A discovery role has more than one persisted agent run.")
            totals = {"input": 0, "output": 0, "cost_cents": 0.0, "tools": 0}
            for run in runs:
                usage = load_versioned_json(run.usage_json)
                totals["input"] += int(usage.get("input_tokens", 0) or 0)
                totals["output"] += int(usage.get("output_tokens", 0) or 0)
                totals["cost_cents"] += float(usage.get("cost_cents", 0.0) or 0.0)
            totals["tools"] = int(
                session.scalar(
                    select(func.count(ToolCallRow.id)).where(
                        ToolCallRow.workflow_id == workflow_id,
                        ToolCallRow.agent_run_id.in_([item.id for item in runs] or ["none"]),
                    )
                )
                or 0
            )
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            authorization = (
                _load_training_document(row.source_discovery_authorization_json)
                if row and row.source_discovery_authorization_json
                else None
            )
        if authorization is None:
            raise GuardNotSatisfied("Source discovery has not been explicitly authorized.")
        if any(run.agent_name == agent_name for run in runs):
            # The harness rehydrates the deterministic persisted terminal result. This path
            # consumes no provider, source-request, token, tool-call, or cost budget.
            return
        if len(runs) >= 4:
            raise GuardNotSatisfied("The four-run source-discovery budget is exhausted.")
        if time.time() - parse_utc(authorization["authorized_at"]).timestamp() >= (
            configuration.global_timeout_seconds
        ):
            raise GuardNotSatisfied("The global source-discovery timeout is exhausted.")
        if totals["input"] + configuration.maximum_input_tokens > (
            configuration.global_maximum_input_tokens
        ):
            raise GuardNotSatisfied("Remaining global input-token budget is insufficient.")
        if totals["output"] + configuration.maximum_output_tokens > (
            configuration.global_maximum_output_tokens
        ):
            raise GuardNotSatisfied("Remaining global output-token budget is insufficient.")
        if totals["tools"] + configuration.maximum_tool_calls > (
            configuration.global_maximum_tool_calls
        ):
            raise GuardNotSatisfied("Remaining global tool-call budget is insufficient.")
        if totals["cost_cents"] + configuration.maximum_cost_usd * 100 > (
            configuration.global_maximum_cost_usd * 100 + 1e-9
        ):
            raise GuardNotSatisfied("Remaining global cost budget is insufficient.")

    def _run_source_discovery_agent(
        self,
        workflow_id: str,
        *,
        definition: SpecializedAgentDefinition,
        stage: WorkflowState,
        next_stage: WorkflowState,
    ) -> WorkflowSnapshot:
        if self.harness is None:
            raise WorkflowConflict("No agent harness is configured for source discovery.")
        self._assert_source_discovery_budget(workflow_id, definition.agent_name)
        snapshot = self.get_build(workflow_id)
        if snapshot.current_stage is not stage:
            raise InvalidTransition(f"{definition.agent_name} is not the active discovery stage.")
        with self.database.session() as session:
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None or not row.specification_json or not row.component_requirements_json:
                raise GuardNotSatisfied("Approved specification and requirements are required.")
            initial_context = BlindBenchmarkInitialContext.model_validate(
                _load_training_document(row.initial_context_json)
            )
            specification = TrainingDatasetSpecification.model_validate(
                _load_training_document(row.specification_json)
            )
            requirements = TrainingDatasetComponentRequirements.model_validate(
                _load_training_document(row.component_requirements_json)
            )
            prior_fragments = (
                _load_training_document(row.source_fragments_json).get("items", [])
                if row.source_fragments_json
                else []
            )
        validated_artifacts: dict[str, object] = {
            "training_dataset_specification": specification.model_dump(mode="json"),
            "component_requirements": requirements.model_dump(mode="json"),
            "approved_scientific_policies": initial_context.approved_scientific_policies,
            "reviewed_adapter_capabilities": self.reviewed_source_adapters.public_inventory(),
        }
        if definition.agent_name == "Chemical Identity and Structure Source Discovery Agent":
            validated_artifacts = {
                "component_requirements": requirements.model_dump(mode="json"),
                "verified_identifier_field_summaries": [
                    {
                        "source_id": record.get("source_id"),
                        "identifier_fields": record.get("identifier_fields", []),
                    }
                    for item in prior_fragments
                    for record in item.get("fragment", {}).get("candidate_records", [])
                ],
                "reviewed_adapter_capabilities": self.reviewed_source_adapters.public_inventory(),
            }
        elif definition.agent_name == "Supporting Metadata Discovery Agent":
            validated_artifacts = {
                "component_requirements": requirements.model_dump(mode="json"),
                "verified_source_identifiers": sorted(
                    {
                        str(record.get("stable_accession"))
                        for item in prior_fragments
                        for record in item.get("fragment", {}).get("candidate_records", [])
                        if record.get("stable_accession")
                    }
                ),
                "reviewed_adapter_capabilities": self.reviewed_source_adapters.public_inventory(),
            }
        step_key = f"source-discovery:{stage.value}:step"
        available_definition = definition.model_copy(
            update={
                "allowed_tools": self.reviewed_source_adapters.filter_available_operations(
                    definition.allowed_tools
                )
            }
        )
        step = self.create_step(
            workflow_id,
            stage,
            idempotency_key=step_key,
            input_payload={
                "agent_name": definition.agent_name,
                "provider": self.agent_configuration.worker_provider,
                "model": self.agent_configuration.worker_model,
                "reviewed_adapter_policy": self.reviewed_source_adapters.readiness(),
            },
        )
        configuration = self.agent_configuration.controlled_source_discovery()
        request = specialized_agent_request(
            definition=available_definition,
            workflow_id=workflow_id,
            step_id=step.id,
            workflow_stage=stage,
            initial_context=initial_context,
            validated_artifacts=validated_artifacts,
            configuration=configuration,
        )

        def persist(call_id: str, tool_name: str, tool_result) -> None:
            if not isinstance(tool_result.output, dict):
                return
            batch = VerifiedSourceObservationBatch.model_validate(tool_result.output)
            self._persist_tool_observations(
                workflow_id,
                step_id=step.id,
                agent_name=definition.agent_name,
                tool_call_id=call_id,
                tool_name=tool_name,
                batch=batch,
            )

        run_id, result = self.harness.run(
            request,
            DiscoveryAgentReviewOutcome,
            tool_result_callback=persist,
        )
        trace_artifact = self._persist_specialized_agent_trace(
            workflow_id=workflow_id,
            step=step,
            run_id=run_id,
            request=request,
            result=result,
            idempotency_key=f"source-discovery:{stage.value}:trace",
        )
        review = (
            DiscoveryAgentReviewOutcome.model_validate(result.output)
            if result.status is AgentRunStatus.COMPLETED and result.output is not None
            else None
        )
        observations = self._agent_observations(workflow_id, definition.agent_name)
        roles = list(
            dict.fromkeys(
                role
                for observation in observations
                for role in observation.source_roles
            )
        ) or [
            role
            for role in self.reviewed_source_adapters.MANDATORY_ROLES
            if role
            in {
                requirement.role
                for requirement in requirements.requirements
            }
        ]
        fragment = compile_verified_source_fragment(
            fragment_id=deterministic_id("fragment", workflow_id, definition.agent_name),
            component_roles=roles or [requirements.requirements[0].role],
            observations=observations,
            review=review,
        )
        fragment_hash = self._persist_source_fragment(
            workflow_id,
            definition=definition,
            step_id=step.id,
            fragment=fragment,
        )
        self.complete_step(
            step.id,
            output_payload={
                "agent_run_id": run_id,
                "agent_run_status": result.status.value,
                "fragment_id": fragment.fragment_id,
                "fragment_hash": fragment_hash,
                "trace_artifact_hash": trace_artifact.sha256,
                "observation_count": len(observations),
                "agent_review_status": fragment.agent_review_status.value,
                "agent_terminal_outcome": fragment.agent_terminal_outcome,
            },
            idempotency_key=f"source-discovery:{stage.value}:step-complete",
        )
        return self.transition(
            workflow_id,
            TransitionRequest(
                target_state=next_stage,
                expected_version=snapshot.version,
                idempotency_key=f"source-discovery:{stage.value}:transition",
                initiator=ActorType.ORCHESTRATOR,
                initiator_id="source-discovery-orchestrator",
                reason=f"{definition.agent_name} completed or terminated without retry.",
                artifact_hashes=[fragment_hash],
            ),
        )

    def _finalize_source_inventory(self, workflow_id: str) -> WorkflowSnapshot:
        snapshot = self.get_build(workflow_id)
        if snapshot.current_stage is not WorkflowState.VALIDATING_DISCOVERED_SOURCES:
            raise InvalidTransition("Source validation is not the active stage.")
        with self.database.session() as session:
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None or not row.source_fragments_json:
                raise GuardNotSatisfied("No source fragments are available for validation.")
            specification = TrainingDatasetSpecification.model_validate(
                _load_training_document(row.specification_json)
            )
            requirements = TrainingDatasetComponentRequirements.model_validate(
                _load_training_document(row.component_requirements_json)
            )
            fragment_items = _load_training_document(row.source_fragments_json).get("items", [])
            fragments = [
                VerifiedSourceInventoryFragment.model_validate(item["fragment"])
                for item in fragment_items
            ]
            observation_ids = sorted(
                {
                    observation_id
                    for fragment in fragments
                    for observation_id in fragment.observation_ids
                }
            )
        validated = self.artifact_store.put_json(
            workflow_id=workflow_id,
            value={"observation_ids": observation_ids, "validation": "typed_adapter_contracts"},
            artifact_type="validated_source_records",
            logical_name="validated-source-records-v1.json",
            producer="deterministic-source-validator",
            idempotency_key="source-discovery:validated-records",
        )
        snapshot = self.transition(
            workflow_id,
            TransitionRequest(
                target_state=WorkflowState.BUILDING_SOURCE_INVENTORY,
                expected_version=snapshot.version,
                idempotency_key="source-discovery:validation-transition",
                initiator=ActorType.ORCHESTRATOR,
                initiator_id="deterministic-source-validator",
                reason="Typed adapter observations were validated deterministically.",
                artifact_hashes=[validated.sha256],
            ),
        )
        inventory = compile_verified_source_inventory(
            inventory_id=deterministic_id("inventory", workflow_id, specification.specification_id),
            specification_id=specification.specification_id,
            requirements=requirements,
            fragments=fragments,
        )
        matrix = build_capability_matrix(inventory, requirements)
        gap_report = build_source_assembly_gap_report(
            report_id=deterministic_id("gap", workflow_id, inventory.inventory_id),
            inventory=inventory,
            matrix=matrix,
        )
        documents = [
            ("source_inventory", inventory.model_dump(mode="json")),
            ("capability_matrix", matrix.model_dump(mode="json")),
            ("gap_report", gap_report.model_dump(mode="json")),
        ]
        hashes = []
        for name, value in documents:
            stored = self.persist_training_dataset_document(
                workflow_id,
                document_name=name,
                value=value,
                actor="deterministic-source-inventory-compiler",
                idempotency_key=f"source-discovery:{name}",
            )
            hashes.append(stored["sha256"])
        return self.transition(
            workflow_id,
            TransitionRequest(
                target_state=WorkflowState.AWAITING_SOURCE_INVENTORY_REVIEW,
                expected_version=snapshot.version,
                idempotency_key="source-discovery:review-transition",
                initiator=ActorType.ORCHESTRATOR,
                initiator_id="deterministic-source-inventory-compiler",
                reason="Verified source inventory is ready for human review.",
                artifact_hashes=hashes,
            ),
        )

    def run_authorized_source_discovery(self, workflow_id: str) -> WorkflowSnapshot:
        """Resume the four-role workflow without repeating completed provider or source calls."""

        definitions = {
            item.agent_name: item
            for item in SPECIALIZED_AGENT_SEQUENCE
        }
        stages = {
            WorkflowState.DISCOVERING_ACTIVITY_EVIDENCE: (
                definitions["Activity Evidence Discovery Agent"],
                WorkflowState.DISCOVERING_TRANSCRIPTOMIC_EVIDENCE,
            ),
            WorkflowState.DISCOVERING_TRANSCRIPTOMIC_EVIDENCE: (
                definitions["Transcriptomic Evidence Discovery Agent"],
                WorkflowState.DISCOVERING_IDENTITY_AND_STRUCTURE_SOURCES,
            ),
            WorkflowState.DISCOVERING_IDENTITY_AND_STRUCTURE_SOURCES: (
                definitions["Chemical Identity and Structure Source Discovery Agent"],
                WorkflowState.DISCOVERING_SUPPORTING_METADATA,
            ),
            WorkflowState.DISCOVERING_SUPPORTING_METADATA: (
                definitions["Supporting Metadata Discovery Agent"],
                WorkflowState.VALIDATING_DISCOVERED_SOURCES,
            ),
        }
        while True:
            snapshot = self.get_build(workflow_id)
            if snapshot.current_stage in stages:
                definition, next_stage = stages[snapshot.current_stage]
                self._run_source_discovery_agent(
                    workflow_id,
                    definition=definition,
                    stage=snapshot.current_stage,
                    next_stage=next_stage,
                )
                continue
            if snapshot.current_stage is WorkflowState.VALIDATING_DISCOVERED_SOURCES:
                return self._finalize_source_inventory(workflow_id)
            if snapshot.current_stage is WorkflowState.AWAITING_SOURCE_INVENTORY_REVIEW:
                return snapshot
            raise InvalidTransition("The workflow is not in an authorized source-discovery stage.")

    def continue_training_dataset_workflow(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Explicitly resume one durable training-dataset activity after recovery.

        Restart reconciliation never invokes a paid provider. This command is the
        authorization boundary for a build that was already transitioned into an
        active stage before its bounded agent step was created.
        """

        snapshot = self.get_build(workflow_id)
        if snapshot.workflow_kind is not WorkflowKind.TRAINING_DATASET_DISCOVERY:
            raise WorkflowConflict("Only training-dataset workflows use this continuation.")
        with self.database.session() as session:
            prior = session.scalar(
                select(WorkflowEventRow).where(
                    WorkflowEventRow.workflow_id == workflow_id,
                    WorkflowEventRow.idempotency_key.in_(
                        {
                            f"{idempotency_key}:specification:finalize:transition",
                            f"{idempotency_key}:compile:transition",
                        }
                    ),
                )
            )
            if prior is not None:
                return self._snapshot(session, require_build(session, workflow_id))
        if snapshot.version != expected_version:
            raise StaleWorkflowVersion(
                "Workflow version is stale.", detail={"current_version": snapshot.version}
            )
        if snapshot.current_stage is WorkflowState.COMPILING_TARGET_DATASET_SPECIFICATION:
            return self.compile_training_dataset_specification(
                workflow_id,
                expected_version=expected_version,
                actor=actor,
                idempotency_key=f"{idempotency_key}:compile",
            )
        if snapshot.current_stage is WorkflowState.DERIVING_COMPONENT_REQUIREMENTS:
            with self.database.session() as session:
                workflow = session.get(TrainingDatasetWorkflowRow, workflow_id)
                requirements_ready = bool(
                    workflow is not None and workflow.component_requirements_json
                )
            if requirements_ready:
                raise GuardNotSatisfied(
                    "Source discovery requires the dedicated reviewed-adapter authorization "
                    "action. Continuing the workflow cannot create authorization implicitly."
                )
            self.derive_training_dataset_requirements(
                workflow_id,
                actor="deterministic-orchestrator",
                idempotency_key=f"{idempotency_key}:requirements",
            )
            return self.get_build(workflow_id)
        if snapshot.current_stage in {
            WorkflowState.DISCOVERING_ACTIVITY_EVIDENCE,
            WorkflowState.DISCOVERING_TRANSCRIPTOMIC_EVIDENCE,
            WorkflowState.DISCOVERING_IDENTITY_AND_STRUCTURE_SOURCES,
            WorkflowState.DISCOVERING_SUPPORTING_METADATA,
            WorkflowState.VALIDATING_DISCOVERED_SOURCES,
        }:
            return self.run_authorized_source_discovery(workflow_id)
        raise InvalidTransition(
            "The current training-dataset stage has no explicit continuation activity."
        )

    def retry_training_dataset_specification(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Human-authorized deterministic recompilation; never a provider retry."""

        snapshot = self.get_build(workflow_id)
        if snapshot.current_stage is not WorkflowState.AWAITING_DATASET_SPECIFICATION_REVISION:
            raise InvalidTransition("Only a specification revision gate may be retried.")
        outcome = next(
            (
                item
                for item in reversed(self.artifact_store.list_artifacts(workflow_id))
                if item.artifact_type
                in {
                    "dataset_specification_agent_outcome",
                    "dataset_specification_compilation_outcome",
                }
            ),
            None,
        )
        if outcome is None:
            raise GuardNotSatisfied("The terminal specification outcome is missing.")
        snapshot = self.transition(
            workflow_id,
            TransitionRequest(
                target_state=WorkflowState.COMPILING_TARGET_DATASET_SPECIFICATION,
                expected_version=expected_version,
                idempotency_key=f"{idempotency_key}:authorize",
                initiator=ActorType.HUMAN,
                initiator_id=actor,
                reason=(
                    "Administrator explicitly authorized deterministic specification compilation."
                ),
                artifact_hashes=[outcome.sha256],
            ),
        )
        return self.compile_training_dataset_specification(
            workflow_id,
            expected_version=snapshot.version,
            actor=actor,
            idempotency_key=f"{idempotency_key}:compile",
        )

    def revise_training_dataset_request(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Return to draft without calling a provider; endpoint edits remain human-authored."""

        outcome = next(
            (
                item
                for item in reversed(self.artifact_store.list_artifacts(workflow_id))
                if item.artifact_type
                in {
                    "dataset_specification_agent_outcome",
                    "dataset_specification_compilation_outcome",
                    "dataset_specification_review_record",
                }
            ),
            None,
        )
        if outcome is None:
            raise GuardNotSatisfied("The specification revision artifact is missing.")
        return self.transition(
            workflow_id,
            TransitionRequest(
                target_state=WorkflowState.DRAFT,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                initiator=ActorType.HUMAN,
                initiator_id=actor,
                reason="Administrator selected endpoint-request revision.",
                artifact_hashes=[outcome.sha256],
            ),
        )

    def compile_training_dataset_specification(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Compile a source-neutral review draft without invoking any provider."""

        snapshot = self.get_build(workflow_id)
        if snapshot.current_stage is not WorkflowState.COMPILING_TARGET_DATASET_SPECIFICATION:
            raise InvalidTransition("Specification compilation is not the active stage.")
        if snapshot.version != expected_version:
            raise StaleWorkflowVersion(
                "Workflow version is stale.", detail={"current_version": snapshot.version}
            )
        with self.database.session() as session:
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None:
                raise WorkflowNotFound("Training-dataset workflow state was not found.")
            initial_context = BlindBenchmarkInitialContext.model_validate(
                _load_training_document(row.initial_context_json)
            )
        step = self.create_step(
            workflow_id,
            WorkflowState.COMPILING_TARGET_DATASET_SPECIFICATION,
            idempotency_key=f"{idempotency_key}:step",
            input_payload={
                "compiler": "DatasetSpecificationCompiler",
                "compiler_version": DatasetSpecificationCompiler.version,
                "provider_invocations": 0,
            },
        )
        hints = initial_context.endpoint_request_semantic_hints or (
            derive_endpoint_request_semantic_hints(
                initial_context.endpoint_name,
                initial_context.biological_goal,
            )
        )
        outcome = DatasetSpecificationCompiler().compile(
            endpoint_name=initial_context.endpoint_name,
            biological_goal=initial_context.biological_goal,
            semantic_hints=hints,
            target_training_dataset_contract=initial_context.target_training_dataset_contract,
            approved_platform_policies=initial_context.approved_scientific_policies,
            schema_version=TRAINING_DATASET_CONTRACT_VERSION,
        )
        hints_artifact = self.artifact_store.put_json(
            workflow_id=workflow_id,
            step_id=step.id,
            value=hints.model_dump(mode="json"),
            artifact_type="endpoint_request_semantic_hints",
            logical_name="endpoint-request-semantic-hints-v1.json",
            producer="deterministic-orchestrator",
            original_source=None,
            idempotency_key=f"{idempotency_key}:semantic-hints",
        )
        outcome_artifact = self.artifact_store.put_json(
            workflow_id=workflow_id,
            step_id=step.id,
            value=outcome.model_dump(mode="json"),
            artifact_type="dataset_specification_compilation_outcome",
            logical_name="dataset-specification-compilation-outcome-v1.json",
            producer="DatasetSpecificationCompiler",
            original_source=None,
            idempotency_key=f"{idempotency_key}:outcome",
        )
        review_record = DatasetSpecificationReviewRecord(
            status="not_run",
            reviewer_outcome=None,
            safe_summary="Optional AI review has not been requested.",
            deterministic_draft_preserved=True,
            requires_explicit_human_action=True,
        )
        review_record_artifact = self.artifact_store.put_json(
            workflow_id=workflow_id,
            step_id=step.id,
            value=review_record.model_dump(mode="json"),
            artifact_type="dataset_specification_review_record",
            logical_name="dataset-specification-review-record-v1.json",
            producer="deterministic-orchestrator",
            original_source=None,
            idempotency_key=f"{idempotency_key}:review-status",
        )
        artifact_hashes = [
            hints_artifact.sha256,
            outcome_artifact.sha256,
            review_record_artifact.sha256,
        ]
        draft_artifact = None
        if outcome.specification is not None:
            draft_artifact = self.artifact_store.put_json(
                workflow_id=workflow_id,
                step_id=step.id,
                value=outcome.specification.model_dump(mode="json"),
                artifact_type="training_dataset_specification_draft",
                logical_name="training-dataset-specification-draft-v1.json",
                producer="DatasetSpecificationCompiler",
                original_source=None,
                idempotency_key=f"{idempotency_key}:draft",
            )
            artifact_hashes.append(draft_artifact.sha256)
        with self.database.session() as session:
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None:
                raise WorkflowNotFound("Training-dataset workflow state was not found.")
            row.specification_draft_json = (
                canonical_json(
                    versioned_payload(document=outcome.specification.model_dump(mode="json"))
                )
                if outcome.specification is not None
                else None
            )
            row.specification_compilation_outcome_json = canonical_json(
                versioned_payload(document=outcome.model_dump(mode="json"))
            )
            row.specification_review_record_json = canonical_json(
                versioned_payload(document=review_record.model_dump(mode="json"))
            )
            row.updated_at = utc_text()
        self.complete_step(
            step.id,
            output_payload={
                "compiler_version": outcome.compiler_version,
                "deterministic_hash": outcome.deterministic_hash,
                "status": outcome.status,
                "provider_invocations": 0,
                "draft_artifact_id": draft_artifact.id if draft_artifact else None,
            },
            idempotency_key=f"{idempotency_key}:step-complete",
        )
        target_state = (
            WorkflowState.AWAITING_DATASET_SPECIFICATION_REVIEW
            if outcome.status == "compiled"
            else WorkflowState.AWAITING_DATASET_SPECIFICATION_REVISION
        )
        snapshot = self.transition(
            workflow_id,
            TransitionRequest(
                target_state=target_state,
                expected_version=expected_version,
                idempotency_key=f"{idempotency_key}:transition",
                initiator=ActorType.ORCHESTRATOR,
                initiator_id="DatasetSpecificationCompiler",
                reason=(
                    "Deterministic target-dataset draft is ready for optional AI and human review."
                    if outcome.status == "compiled"
                    else "Core endpoint semantics require human clarification before compilation."
                ),
                artifact_hashes=artifact_hashes,
            ),
        )
        if outcome.status != "compiled" or outcome.specification is None:
            return snapshot
        endpoint_definition = next(
            item
            for item in self.artifact_store.list_artifacts(workflow_id)
            if item.artifact_type == "endpoint_definition"
        )
        approval_hashes = sorted({endpoint_definition.sha256, *artifact_hashes})
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            self._create_approval(
                session,
                build,
                ApprovalRequest(
                    workflow_id=workflow_id,
                    stage=WorkflowState.AWAITING_DATASET_SPECIFICATION_REVIEW,
                    approval_type=ApprovalType.DATASET_SPECIFICATION,
                    proposed_decision=(
                        "Approve this target training-dataset specification before public-source "
                        "discovery begins."
                    ),
                    evidence_summary=(
                        "Draft produced deterministically from the request and approved platform "
                        "contract."
                    ),
                    limitations=[*outcome.limitations, *outcome.approval_questions],
                    artifact_hashes=approval_hashes,
                    agent_recommendation=(
                        "Optional AI review has not run. Review compiler provenance and all "
                        "human-decision questions before approval."
                    ),
                    requested_action=(
                        "Approve as drafted, edit allowed policy choices, request optional AI "
                        "review, or request revision."
                    ),
                ),
                idempotency_key=f"{idempotency_key}:approval",
            )
            return self._snapshot(session, build)

    def run_optional_dataset_specification_review(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Run one explicitly authorized reviewer without making the draft authoritative."""

        if self.harness is None:
            raise WorkflowConflict("No agent harness is configured for optional review.")
        snapshot = self.get_build(workflow_id)
        if snapshot.current_stage is not WorkflowState.AWAITING_DATASET_SPECIFICATION_REVIEW:
            raise InvalidTransition("Optional review can start only from the review gate.")
        snapshot = self.transition(
            workflow_id,
            TransitionRequest(
                target_state=WorkflowState.REVIEWING_DATASET_SPECIFICATION,
                expected_version=expected_version,
                idempotency_key=f"{idempotency_key}:authorize",
                initiator=ActorType.HUMAN,
                initiator_id=actor,
                reason="Administrator explicitly requested one optional specification review.",
                artifact_hashes=[
                    item.sha256
                    for item in self.artifact_store.list_artifacts(workflow_id)
                    if item.artifact_type
                    in {
                        "training_dataset_specification_draft",
                        "dataset_specification_compilation_outcome",
                    }
                ],
            ),
        )
        with self.database.session() as session:
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None or not row.specification_draft_json:
                raise GuardNotSatisfied("Deterministic specification draft is missing.")
            initial_context = BlindBenchmarkInitialContext.model_validate(
                _load_training_document(row.initial_context_json)
            )
            draft = TrainingDatasetSpecificationDraft.model_validate(
                _load_training_document(row.specification_draft_json)
            )
        definition = next(
            item
            for item in SPECIALIZED_AGENT_SEQUENCE
            if item.agent_name == "Dataset Specification Review Agent"
        )
        step = self.create_step(
            workflow_id,
            WorkflowState.REVIEWING_DATASET_SPECIFICATION,
            idempotency_key=f"{idempotency_key}:step",
            input_payload={
                "agent_name": definition.agent_name,
                "provider": self.agent_configuration.planner_provider,
                "model": self.agent_configuration.planner_model,
                "optional": True,
            },
        )
        request = specialized_agent_request(
            definition=definition,
            workflow_id=workflow_id,
            step_id=step.id,
            workflow_stage=WorkflowState.REVIEWING_DATASET_SPECIFICATION,
            initial_context=initial_context,
            validated_artifacts={
                "training_dataset_specification_draft": draft.model_dump(mode="json"),
                "approved_scientific_policies": initial_context.approved_scientific_policies,
            },
            configuration=self.agent_configuration,
        )
        run_id, result = self.harness.run(request, DatasetSpecificationReviewOutcome)
        trace_artifact = self._persist_specialized_agent_trace(
            workflow_id=workflow_id,
            step=step,
            run_id=run_id,
            request=request,
            result=result,
            idempotency_key=f"{idempotency_key}:trace",
        )
        reviewer_outcome = (
            DatasetSpecificationReviewOutcome.model_validate(result.output)
            if result.status is AgentRunStatus.COMPLETED and result.output is not None
            else None
        )
        if reviewer_outcome is None:
            record = DatasetSpecificationReviewRecord(
                status="unavailable",
                reviewer_outcome=None,
                safe_summary=(
                    result.error.safe_message
                    if result.error
                    else "Optional AI review was unavailable; the compiled draft is unchanged."
                ),
                deterministic_draft_preserved=True,
                requires_explicit_human_action=True,
            )
        else:
            status = {
                "blocking_issue_found": "blocking_issue_found",
                "model_refused": "refused",
                "invalid_model_output": "invalid_output",
            }.get(reviewer_outcome.status, "completed")
            record = DatasetSpecificationReviewRecord(
                status=status,
                reviewer_outcome=reviewer_outcome,
                safe_summary=reviewer_outcome.review_summary,
                deterministic_draft_preserved=True,
                requires_explicit_human_action=True,
            )
        outcome_artifact = None
        if reviewer_outcome is not None:
            outcome_artifact = self.artifact_store.put_json(
                workflow_id=workflow_id,
                step_id=step.id,
                value=reviewer_outcome.model_dump(mode="json"),
                artifact_type="dataset_specification_review_outcome",
                logical_name="dataset-specification-review-outcome-v1.json",
                producer=definition.agent_name,
                original_source=None,
                idempotency_key=f"{idempotency_key}:outcome",
            )
        record_artifact = self.artifact_store.put_json(
            workflow_id=workflow_id,
            step_id=step.id,
            value=record.model_dump(mode="json"),
            artifact_type="dataset_specification_review_record",
            logical_name="dataset-specification-review-record-v2.json",
            producer="deterministic-orchestrator",
            original_source=None,
            idempotency_key=f"{idempotency_key}:record",
        )
        with self.database.session() as session:
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None:
                raise WorkflowNotFound("Training-dataset workflow state was not found.")
            row.specification_review_record_json = canonical_json(
                versioned_payload(document=record.model_dump(mode="json"))
            )
            row.updated_at = utc_text()
        self.complete_step(
            step.id,
            output_payload={
                "agent_run_id": run_id,
                "review_status": record.status,
                "trace_artifact_id": trace_artifact.id,
                "deterministic_draft_preserved": True,
            },
            idempotency_key=f"{idempotency_key}:step-complete",
        )
        blocking = record.status == "blocking_issue_found"
        target = (
            WorkflowState.AWAITING_DATASET_SPECIFICATION_REVISION
            if blocking
            else WorkflowState.AWAITING_DATASET_SPECIFICATION_REVIEW
        )
        hashes = [trace_artifact.sha256, record_artifact.sha256]
        if outcome_artifact:
            hashes.append(outcome_artifact.sha256)
        snapshot = self.transition(
            workflow_id,
            TransitionRequest(
                target_state=target,
                expected_version=snapshot.version,
                idempotency_key=f"{idempotency_key}:transition",
                initiator=ActorType.ORCHESTRATOR,
                initiator_id="workflow-service",
                reason=(
                    "Optional reviewer found a blocking issue; deterministic draft was preserved."
                    if blocking
                    else (
                        "Optional review completed or was unavailable; deterministic draft remains "
                        "reviewable."
                    )
                ),
                artifact_hashes=hashes,
            ),
        )
        if blocking:
            with self.database.session() as session:
                pending = session.scalar(
                    select(ApprovalRow)
                    .where(
                        ApprovalRow.workflow_id == workflow_id,
                        ApprovalRow.approval_type == ApprovalType.DATASET_SPECIFICATION.value,
                        ApprovalRow.status == ApprovalStatus.PENDING.value,
                    )
                    .order_by(ApprovalRow.created_at.desc())
                )
                if pending is not None:
                    pending.status = ApprovalStatus.CANCELLED.value
                    pending.decided_at = utc_text()
            return snapshot
        if outcome_artifact is None:
            return snapshot
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            prior = session.scalar(
                select(ApprovalRow)
                .where(
                    ApprovalRow.workflow_id == workflow_id,
                    ApprovalRow.approval_type == ApprovalType.DATASET_SPECIFICATION.value,
                    ApprovalRow.status == ApprovalStatus.PENDING.value,
                )
                .order_by(ApprovalRow.created_at.desc())
            )
            if prior is None:
                return self._snapshot(session, build)
            prior_request = ApprovalRequest.model_validate_json(prior.request_json)
            prior.status = ApprovalStatus.CANCELLED.value
            prior.decided_at = utc_text()
            self._create_approval(
                session,
                build,
                ApprovalRequest(
                    workflow_id=workflow_id,
                    stage=WorkflowState.AWAITING_DATASET_SPECIFICATION_REVIEW,
                    approval_type=ApprovalType.DATASET_SPECIFICATION,
                    proposed_decision=prior_request.proposed_decision,
                    evidence_summary=prior_request.evidence_summary,
                    limitations=[*prior_request.limitations, record.safe_summary],
                    artifact_hashes=sorted(
                        {
                            *prior_request.artifact_hashes,
                            outcome_artifact.sha256,
                            record_artifact.sha256,
                        }
                    ),
                    agent_recommendation=record.safe_summary,
                    requested_action=prior_request.requested_action,
                ),
                idempotency_key=f"{idempotency_key}:superseding-approval",
                supersedes_id=prior.id,
            )
            return self._snapshot(session, build)

    def run_training_dataset_specification(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Run and checkpoint the bounded Dataset Specification Agent exactly once."""

        if self.harness is None:
            raise WorkflowConflict("No agent harness is configured.")
        snapshot = self.get_build(workflow_id)
        if snapshot.current_stage is not WorkflowState.SPECIFYING_TARGET_DATASET:
            raise InvalidTransition(
                "Dataset specification can run only in SPECIFYING_TARGET_DATASET."
            )
        if snapshot.version != expected_version:
            raise StaleWorkflowVersion(
                "Workflow version is stale.", detail={"current_version": snapshot.version}
            )
        with self.database.session() as session:
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None:
                raise WorkflowNotFound("Training-dataset workflow state was not found.")
            initial_context = BlindBenchmarkInitialContext.model_validate(
                _load_training_document(row.initial_context_json)
            )
        definition = SpecializedAgentDefinition(
            agent_name="Dataset Specification Agent",
            role="planner",
            output_schema_name="DatasetSpecificationAgentOutcome",
            receives_artifacts=["endpoint_definition"],
            produces_artifact="training_dataset_specification",
        )
        step = self.create_step(
            workflow_id,
            WorkflowState.SPECIFYING_TARGET_DATASET,
            idempotency_key=f"{idempotency_key}:step",
            input_payload={
                "agent_name": definition.agent_name,
                "benchmark_mode": initial_context.benchmark_mode,
                "provider": self.agent_configuration.planner_provider,
                "model": self.agent_configuration.planner_model,
            },
        )
        semantic_hints = (
            initial_context.endpoint_request_semantic_hints
            or derive_endpoint_request_semantic_hints(
                initial_context.endpoint_name,
                initial_context.biological_goal,
            )
        )
        semantic_hints_artifact = self.artifact_store.put_json(
            workflow_id=workflow_id,
            step_id=step.id,
            value=semantic_hints.model_dump(mode="json"),
            artifact_type="endpoint_request_semantic_hints",
            logical_name="endpoint-request-semantic-hints-v1.json",
            producer="deterministic-orchestrator",
            original_source=None,
            idempotency_key=f"{idempotency_key}:semantic-hints",
        )
        request = specialized_agent_request(
            definition=definition,
            workflow_id=workflow_id,
            step_id=step.id,
            workflow_stage=WorkflowState.SPECIFYING_TARGET_DATASET,
            initial_context=initial_context,
            validated_artifacts={
                "target_training_dataset_contract": (
                    initial_context.target_training_dataset_contract
                ),
                "approved_scientific_policies": (initial_context.approved_scientific_policies),
            },
            configuration=self.agent_configuration,
        )
        run_id, result = self.harness.run(request, DatasetSpecificationAgentOutcome)
        if result.status is not AgentRunStatus.COMPLETED or result.output is None:
            return self._fail_training_dataset_agent_step(
                workflow_id=workflow_id,
                step=step,
                run_id=run_id,
                request=request,
                result=result,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )
        outcome = DatasetSpecificationAgentOutcome.model_validate(result.output)
        semantic_validation = validate_dataset_specification_semantics(
            semantic_hints,
            outcome,
        )
        trace_artifact = self._persist_specialized_agent_trace(
            workflow_id=workflow_id,
            step=step,
            run_id=run_id,
            request=request,
            result=result,
            idempotency_key=f"{idempotency_key}:trace",
        )
        self.complete_step(
            step.id,
            output_payload={
                "agent_run_id": run_id,
                "trace_artifact_id": trace_artifact.id,
                "output_schema": DatasetSpecificationAgentOutcome.__name__,
                "outcome_status": outcome.status,
                "semantic_validation_status": semantic_validation.status,
                "semantic_violation_code": semantic_validation.violation_code,
            },
            idempotency_key=f"{idempotency_key}:step-complete",
        )
        return self.finalize_training_dataset_specification_outcome(
            workflow_id,
            outcome=outcome,
            semantic_validation=semantic_validation,
            semantic_hints_artifact_hash=semantic_hints_artifact.sha256,
            trace_artifact_hash=trace_artifact.sha256,
            expected_version=expected_version,
            actor=definition.agent_name,
            idempotency_key=f"{idempotency_key}:finalize",
        )

    def finalize_training_dataset_specification_outcome(
        self,
        workflow_id: str,
        *,
        outcome: DatasetSpecificationAgentOutcome,
        semantic_validation: DatasetSpecificationSemanticValidation,
        semantic_hints_artifact_hash: str,
        trace_artifact_hash: str,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Persist the planner outcome and stop at approval or explicit revision."""

        outcome_payload = outcome.model_dump(mode="json")
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            if build.current_stage != WorkflowState.SPECIFYING_TARGET_DATASET.value:
                raise InvalidTransition("Specification outcome is not the active workflow stage.")
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None:
                raise WorkflowNotFound("Training-dataset workflow state was not found.")
            outcome_artifact = self.artifact_store._put_bytes(
                session,
                workflow_id=workflow_id,
                content=canonical_json(outcome_payload).encode(),
                mime_type="application/json",
                artifact_type="dataset_specification_agent_outcome",
                logical_name="dataset-specification-agent-outcome-v1.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:outcome-artifact",
            )
            semantic_validation_payload = semantic_validation.model_dump(mode="json")
            semantic_validation_artifact = self.artifact_store._put_bytes(
                session,
                workflow_id=workflow_id,
                content=canonical_json(semantic_validation_payload).encode(),
                mime_type="application/json",
                artifact_type="dataset_specification_semantic_validation",
                logical_name="dataset-specification-semantic-validation-v1.json",
                producer="deterministic-orchestrator",
                idempotency_key=f"{idempotency_key}:semantic-validation-artifact",
            )
            row.specification_outcome_json = canonical_json(
                versioned_payload(document=outcome_payload)
            )
            row.specification_semantic_validation_json = canonical_json(
                versioned_payload(document=semantic_validation_payload)
            )
            artifact_hashes = [
                semantic_hints_artifact_hash,
                trace_artifact_hash,
                outcome_artifact.sha256,
                semantic_validation_artifact.sha256,
            ]
            if outcome.specification is not None:
                draft_payload = outcome.specification.model_dump(mode="json")
                draft_artifact = self.artifact_store._put_bytes(
                    session,
                    workflow_id=workflow_id,
                    content=canonical_json(draft_payload).encode(),
                    mime_type="application/json",
                    artifact_type="training_dataset_specification_draft",
                    logical_name="training-dataset-specification-draft-v1.json",
                    producer=actor,
                    idempotency_key=f"{idempotency_key}:draft-artifact",
                )
                row.specification_draft_json = canonical_json(
                    versioned_payload(document=draft_payload)
                )
                artifact_hashes.append(draft_artifact.sha256)
            else:
                row.specification_draft_json = None

            if (
                outcome.status == "completed"
                and outcome.specification is not None
                and semantic_validation.status == "valid"
            ):
                row.updated_at = utc_text()
                snapshot = self._apply_transition(
                    session,
                    build,
                    TransitionRequest(
                        target_state=WorkflowState.AWAITING_DATASET_SPECIFICATION_APPROVAL,
                        expected_version=expected_version,
                        idempotency_key=f"{idempotency_key}:transition",
                        initiator=ActorType.ORCHESTRATOR,
                        initiator_id=actor,
                        reason="Strict target training-dataset draft is ready for review.",
                        artifact_hashes=artifact_hashes,
                    ),
                )
                self._create_approval(
                    session,
                    build,
                    ApprovalRequest(
                        workflow_id=workflow_id,
                        stage=WorkflowState.AWAITING_DATASET_SPECIFICATION_APPROVAL,
                        approval_type=ApprovalType.DATASET_SPECIFICATION,
                        proposed_decision="Approve the target training-dataset draft.",
                        evidence_summary=outcome.decision_summary,
                        limitations=[*outcome.limitations, *outcome.unresolved_questions],
                        artifact_hashes=artifact_hashes,
                        agent_recommendation=(
                            "Review the proposed grain, evidence forms, assumptions, and "
                            "unresolved decisions. Policy thresholds are not model-generated."
                        ),
                        requested_action=(
                            "Approve the draft for deterministic full-contract materialization, "
                            "or request revision."
                        ),
                    ),
                    idempotency_key=f"{idempotency_key}:approval",
                )
                return snapshot

            row.updated_at = utc_text()
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.AWAITING_DATASET_SPECIFICATION_REVISION,
                    expected_version=expected_version,
                    idempotency_key=f"{idempotency_key}:transition",
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id=actor,
                    reason=(
                        "Dataset specification policy needs revision; no source discovery was "
                        "started."
                        if semantic_validation.status == "semantic_contract_violation"
                        else "Dataset specification needs revision; no source discovery was "
                        "started."
                    ),
                    artifact_hashes=artifact_hashes,
                ),
            )

    def _persist_specialized_agent_trace(
        self,
        *,
        workflow_id: str,
        step,
        run_id: str,
        request,
        result,
        idempotency_key: str,
    ):
        diagnostics = [
            event.detail.get("structured_output_diagnostic")
            for event in result.trace
            if isinstance(event.detail.get("structured_output_diagnostic"), dict)
        ]
        terminal_diagnostic = diagnostics[-1] if diagnostics else None
        return self.artifact_store.put_json(
            workflow_id=workflow_id,
            step_id=step.id,
            value={
                "agent_name": request.agent_name,
                "provider": request.model.provider,
                "model": request.model.model_identifier,
                "run_mode": request.context.get("run_mode"),
                "benchmark_mode": request.context.get("benchmark_mode"),
                "agent_run_id": run_id,
                "status": result.status.value,
                "turns": result.turns,
                "tool_calls": result.tool_calls,
                "usage": result.usage.model_dump(mode="json"),
                "sdk_version": (
                    terminal_diagnostic.get("sdk_version") if terminal_diagnostic else None
                ),
                "output_schema_name": request.output_schema_name,
                "output_schema_version": (
                    terminal_diagnostic.get("output_schema_version")
                    if terminal_diagnostic
                    else None
                ),
                "output_schema_hash": (
                    terminal_diagnostic.get("output_schema_hash") if terminal_diagnostic else None
                ),
                "structured_output_diagnostic": terminal_diagnostic,
                "termination_reason": (
                    terminal_diagnostic.get("handler_outcome")
                    if terminal_diagnostic
                    else result.status.value
                ),
                "events": [event.model_dump(mode="json") for event in result.trace],
            },
            artifact_type="specialized_agent_trace",
            logical_name=(
                f"{request.agent_name.casefold().replace(' ', '-')}-trace-v{step.attempt}.json"
            ),
            producer="agent-harness",
            original_source="endoscan://immutable-trace",
            idempotency_key=idempotency_key,
        )

    def _fail_training_dataset_agent_step(
        self,
        *,
        workflow_id: str,
        step,
        run_id: str,
        request,
        result,
        expected_version: int,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        trace_artifact = self._persist_specialized_agent_trace(
            workflow_id=workflow_id,
            step=step,
            run_id=run_id,
            request=request,
            result=result,
            idempotency_key=f"{idempotency_key}:terminal-trace",
        )
        with self.database.session() as session:
            stored_step = session.get(WorkflowStepRow, step.id)
            if stored_step and stored_step.status == StepStatus.RUNNING.value:
                retryable = bool(result.error and result.error.retryable)
                stored_step.status = (
                    StepStatus.FAILED_RETRYABLE.value if retryable else StepStatus.FAILED.value
                )
                if result.error:
                    stored_step.error_id = deterministic_id("err", run_id, result.error.code)
                stored_step.output_json = canonical_json(
                    versioned_payload(trace_artifact_id=trace_artifact.id)
                )
                stored_step.completed_at = utc_text()
        return self.transition(
            workflow_id,
            TransitionRequest(
                target_state=WorkflowState.FAILED,
                expected_version=expected_version,
                idempotency_key=f"{idempotency_key}:failed",
                initiator=ActorType.ORCHESTRATOR,
                initiator_id="agent-harness",
                reason=(
                    result.error.safe_message
                    if result.error
                    else "Specialized agent failed safely."
                ),
                artifact_hashes=[trace_artifact.sha256],
            ),
        )

    def start_build(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        artifacts = self.artifact_store.list_artifacts(workflow_id)
        definition = next(
            (item for item in artifacts if item.artifact_type == "endpoint_definition"), None
        )
        if definition is None:
            raise GuardNotSatisfied("Endpoint definition artifact is missing.")
        snapshot = self.get_build(workflow_id)
        if snapshot.workflow_kind is WorkflowKind.TRAINING_DATASET_DISCOVERY:
            context = next(
                (item for item in artifacts if item.artifact_type == "blind_context_audit"),
                None,
            )
            if context is None:
                raise GuardNotSatisfied("Training-dataset initial context is missing.")
            snapshot = self.transition(
                workflow_id,
                TransitionRequest(
                    target_state=WorkflowState.COMPILING_TARGET_DATASET_SPECIFICATION,
                    expected_version=expected_version,
                    idempotency_key=f"{idempotency_key}:compiling",
                    initiator=ActorType.HUMAN,
                    initiator_id=actor,
                    reason="Administrator started deterministic target-dataset compilation.",
                    artifact_hashes=[definition.sha256, context.sha256],
                ),
            )
            if snapshot.current_stage is not WorkflowState.COMPILING_TARGET_DATASET_SPECIFICATION:
                return snapshot
            return self.compile_training_dataset_specification(
                workflow_id,
                expected_version=snapshot.version,
                actor=actor,
                idempotency_key=f"{idempotency_key}:compilation",
            )
        if self.harness is None:
            raise WorkflowConflict("No Phase-0 agent harness is configured.")
        snapshot = self.transition(
            workflow_id,
            TransitionRequest(
                target_state=WorkflowState.DISCOVERING_DATA,
                expected_version=expected_version,
                idempotency_key=f"{idempotency_key}:discovering",
                initiator=ActorType.HUMAN,
                initiator_id=actor,
                reason=(
                    "Administrator started "
                    f"{self.agent_configuration.run_mode.value} dataset discovery."
                ),
                artifact_hashes=[definition.sha256],
            ),
        )
        if snapshot.current_stage is WorkflowState.AWAITING_DATASET_APPROVAL:
            return snapshot
        return self.run_discovery(
            workflow_id,
            expected_version=snapshot.version,
            actor=actor,
            idempotency_key=idempotency_key,
        )

    def run_discovery(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        actor: str,
        idempotency_key: str,
        refresh_source_metadata: bool = False,
    ) -> WorkflowSnapshot:
        if self.harness is None:
            raise WorkflowConflict("No Phase-0 agent harness is configured.")
        snapshot = self.get_build(workflow_id)
        if snapshot.current_stage is not WorkflowState.DISCOVERING_DATA:
            raise InvalidTransition("Discovery can run only in DISCOVERING_DATA.")
        if snapshot.version != expected_version:
            raise StaleWorkflowVersion(
                "Workflow version is stale.", detail={"current_version": snapshot.version}
            )
        step = self.create_step(
            workflow_id,
            WorkflowState.DISCOVERING_DATA,
            idempotency_key=f"{idempotency_key}:step",
            input_payload={
                "run_mode": self.agent_configuration.run_mode.value,
                "provider": self.agent_configuration.provider,
                "model": self.agent_configuration.model,
            },
        )
        request = discovery_request(
            workflow_id=workflow_id,
            step_id=step.id,
            endpoint_name=snapshot.endpoint_name,
            biological_goal=snapshot.biological_goal,
            configuration=self.agent_configuration,
            refresh_source_metadata=refresh_source_metadata,
        )
        run_id, result = self.harness.run(request, DiscoveryOutput)
        if result.status.value != "completed" or result.output is None:
            trace_artifact = self._persist_terminal_trace_artifact(
                workflow_id=workflow_id,
                step=step,
                run_id=run_id,
                request=request,
                result=result,
                idempotency_key=f"{idempotency_key}:trace-artifact",
            )
            with self.database.session() as session:
                stored_step = session.get(WorkflowStepRow, step.id)
                if stored_step and stored_step.status == StepStatus.RUNNING.value:
                    retryable = bool(result.error and result.error.retryable)
                    stored_step.status = (
                        StepStatus.FAILED_RETRYABLE.value if retryable else StepStatus.FAILED.value
                    )
                    if result.error:
                        stored_step.error_id = deterministic_id("err", run_id, result.error.code)
                    stored_step.output_json = canonical_json(
                        versioned_payload(trace_artifact_id=trace_artifact.id)
                    )
                    stored_step.completed_at = utc_text()
            return self.transition(
                workflow_id,
                TransitionRequest(
                    target_state=WorkflowState.FAILED,
                    expected_version=expected_version,
                    idempotency_key=f"{idempotency_key}:failed",
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id="agent-harness",
                    reason=result.error.safe_message
                    if result.error
                    else "Discovery failed safely.",
                    artifact_hashes=[trace_artifact.sha256],
                ),
            )
        revision = step.attempt
        available_artifact_ids = {
            item.id for item in self.artifact_store.list_artifacts(workflow_id)
        }
        output = validate_evidence_references(
            DiscoveryOutput.model_validate(result.output), available_artifact_ids
        )
        output_payload = output.model_dump(mode="json")
        run_mode = output.run_mode
        candidate_artifact = self.artifact_store.put_json(
            workflow_id=workflow_id,
            step_id=step.id,
            value={**output_payload, "proposal_revision": revision, "agent_run_id": run_id},
            artifact_type="dataset_candidates",
            logical_name=f"dataset-candidates-{run_mode}-v{revision}.json",
            producer="Dataset Discovery and Evaluation Agent",
            original_source=(
                "phase1://validated-replay"
                if run_mode == "replay"
                else "https://www.ncbi.nlm.nih.gov/geo/"
            ),
            idempotency_key=f"{idempotency_key}:candidate-artifact",
        )
        strategy_artifact = self.artifact_store.put_json(
            workflow_id=workflow_id,
            step_id=step.id,
            value={
                "run_mode": run_mode,
                "search_strategy": output.search_strategy,
                "queries_executed": output.queries_executed,
                "search_strategy_steps": [
                    item.model_dump(mode="json") for item in output.search_strategy_steps
                ],
                "limitations": output.limitations,
            },
            artifact_type="search_strategy",
            logical_name=f"search-strategy-{run_mode}-v{revision}.json",
            producer="Dataset Discovery and Evaluation Agent",
            original_source="endoscan://agent-output",
            idempotency_key=f"{idempotency_key}:strategy-artifact",
        )
        recommendation_artifact = self.artifact_store.put_json(
            workflow_id=workflow_id,
            step_id=step.id,
            value={
                "run_mode": run_mode,
                "recommended_candidate_id": output.recommended_candidate_id,
                "recommendation": output.recommendation,
                "decision_summary": output.decision_summary,
                "unresolved_questions": output.unresolved_questions,
                "requires_human_review": output.requires_human_review,
                "confidence_category": output.confidence_category,
                "evidence_references": [
                    item.model_dump(mode="json") for item in output.evidence_references
                ],
            },
            artifact_type="agent_recommendation",
            logical_name=f"agent-recommendation-{run_mode}-v{revision}.json",
            producer="Dataset Discovery and Evaluation Agent",
            original_source="endoscan://agent-output",
            idempotency_key=f"{idempotency_key}:recommendation-artifact",
        )
        trace_artifact = self.artifact_store.put_json(
            workflow_id=workflow_id,
            step_id=step.id,
            value={
                "run_mode": run_mode,
                "provider": request.model.provider,
                "model": request.model.model_identifier,
                "agent_run_id": run_id,
                "events": [event.model_dump(mode="json") for event in result.trace],
            },
            artifact_type="search_trace",
            logical_name=f"internal-agent-trace-{run_mode}-v{revision}.json",
            producer="agent-harness",
            original_source="endoscan://immutable-trace",
            idempotency_key=f"{idempotency_key}:trace-artifact",
        )
        self.complete_step(
            step.id,
            output_payload={
                "agent_run_id": run_id,
                "candidate_artifact_id": candidate_artifact.id,
                "strategy_artifact_id": strategy_artifact.id,
                "recommendation_artifact_id": recommendation_artifact.id,
                "trace_artifact_id": trace_artifact.id,
            },
            idempotency_key=f"{idempotency_key}:step-complete",
        )
        artifact_hashes = [
            candidate_artifact.sha256,
            strategy_artifact.sha256,
            recommendation_artifact.sha256,
            trace_artifact.sha256,
        ]
        if output.recommended_candidate_id is None:
            self.transition(
                workflow_id,
                TransitionRequest(
                    target_state=WorkflowState.AWAITING_SEARCH_REVIEW,
                    expected_version=expected_version,
                    idempotency_key=f"{idempotency_key}:no-valid-candidate",
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id="workflow-service",
                    reason=(
                        "Discovery completed without a scientifically suitable dataset "
                        "recommendation and requires human search review."
                    ),
                    artifact_hashes=artifact_hashes,
                ),
            )
            search_review = ApprovalRequest(
                workflow_id=workflow_id,
                stage=WorkflowState.AWAITING_SEARCH_REVIEW,
                approval_type=ApprovalType.SEARCH_REVISION,
                proposed_decision=f"Review bounded {run_mode} discovery revision {revision}.",
                evidence_summary=output.decision_summary,
                source_references=[candidate.source for candidate in output.candidates],
                limitations=output.limitations,
                artifact_hashes=[
                    candidate_artifact.sha256,
                    strategy_artifact.sha256,
                    recommendation_artifact.sha256,
                ],
                agent_recommendation=(
                    output.proposed_next_search_strategy
                    or "Revise one bounded search dimension before another discovery attempt."
                ),
                requested_action="Request a revised search or cancel the workflow.",
            )
            self.create_approval(
                search_review,
                idempotency_key=f"{idempotency_key}:search-review-v{revision}",
            )
            return self.get_build(workflow_id)
        self.transition(
            workflow_id,
            TransitionRequest(
                target_state=WorkflowState.AWAITING_DATASET_APPROVAL,
                expected_version=expected_version,
                idempotency_key=f"{idempotency_key}:awaiting-approval",
                initiator=ActorType.ORCHESTRATOR,
                initiator_id="workflow-service",
                reason=f"{run_mode.title()} candidates are ready for human dataset review.",
                artifact_hashes=artifact_hashes,
            ),
        )
        approval = ApprovalRequest(
            workflow_id=workflow_id,
            stage=WorkflowState.AWAITING_DATASET_APPROVAL,
            approval_type=ApprovalType.DATASET_SELECTION,
            proposed_decision=f"Select one {run_mode} candidate from proposal revision {revision}.",
            evidence_summary=output.decision_summary,
            source_references=[candidate.source for candidate in output.candidates],
            limitations=output.limitations,
            artifact_hashes=[
                candidate_artifact.sha256,
                strategy_artifact.sha256,
                recommendation_artifact.sha256,
            ],
            agent_recommendation=output.recommendation,
            requested_action="Approve, reject, request revision, or choose an alternative.",
        )
        self.create_approval(
            approval, idempotency_key=f"{idempotency_key}:dataset-approval-v{revision}"
        )
        return self.get_build(workflow_id)

    def _persist_terminal_trace_artifact(
        self,
        *,
        workflow_id: str,
        step,
        run_id: str,
        request,
        result,
        idempotency_key: str,
    ):
        stored_run = self.agent_run(run_id)
        tool_summaries = []
        for call in stored_run.get("tool_calls", []):
            result_payload = call.get("result") or {}
            output = result_payload.get("output") or {}
            bounded_output: dict = {}
            if call.get("tool_name") == "search_geo_series":
                bounded_output = {
                    "rendered_query": output.get("rendered_query"),
                    "result_count": output.get("result_count"),
                    "accessions": [
                        item.get("accession")
                        for item in output.get("results", [])
                        if isinstance(item, dict) and item.get("accession")
                    ][:10],
                    "source_artifact_id": output.get("source_artifact_id"),
                    "cache_status": output.get("cache_status"),
                }
            elif call.get("tool_name") in {
                "validate_geo_accession",
                "validate_geo_accessions",
            }:
                validation_results = output.get("results") if isinstance(output, dict) else None
                if not isinstance(validation_results, list):
                    validation_results = [output] if isinstance(output, dict) else []
                bounded_output = {
                    "results": [
                        {
                            key: item.get(key)
                            for key in (
                                "accession",
                                "status",
                                "source_artifact_id",
                                "source_artifact_sha256",
                                "cache_status",
                                "safe_warning_or_error_category",
                                "retryable",
                            )
                        }
                        for item in validation_results[:5]
                        if isinstance(item, dict)
                    ]
                }
            tool_summaries.append(
                {
                    "id": call.get("id"),
                    "tool_name": call.get("tool_name"),
                    "status": call.get("status"),
                    "duration_ms": call.get("duration_ms"),
                    "original_arguments": result_payload.get("original_arguments"),
                    "normalized_arguments": result_payload.get("normalized_arguments"),
                    "normalization_warnings": result_payload.get("normalization_warnings", []),
                    "source_diagnostic": result_payload.get("source_diagnostic"),
                    "error": result_payload.get("error"),
                    "bounded_output": bounded_output,
                }
            )
        artifact_references = [
            {
                "id": item.id,
                "sha256": item.sha256,
                "artifact_type": item.artifact_type,
                "logical_name": item.logical_name,
            }
            for item in self.artifact_store.list_artifacts(workflow_id)
        ]
        return self.artifact_store.put_json(
            workflow_id=workflow_id,
            step_id=step.id,
            value={
                "run_mode": request.context.get("run_mode", "replay"),
                "provider": request.model.provider,
                "model": request.model.model_identifier,
                "agent_run_id": run_id,
                "status": result.status.value,
                "turns": result.turns,
                "usage": result.usage.model_dump(mode="json"),
                "termination_reason": (
                    result.error.model_dump(mode="json") if result.error else None
                ),
                "events": [event.model_dump(mode="json") for event in result.trace],
                "tool_calls": tool_summaries,
                "artifact_references": artifact_references,
            },
            artifact_type="search_trace",
            logical_name=(
                f"internal-agent-trace-{request.context.get('run_mode', 'replay')}-"
                f"v{step.attempt}.json"
            ),
            producer="agent-harness",
            original_source="endoscan://immutable-trace",
            idempotency_key=idempotency_key,
        )

    def transition(self, workflow_id: str, request: TransitionRequest) -> WorkflowSnapshot:
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            return self._apply_transition(session, build, request)

    def pause(
        self, workflow_id: str, *, expected_version: int, actor: str, idempotency_key: str
    ) -> WorkflowSnapshot:
        return self.transition(
            workflow_id,
            TransitionRequest(
                target_state=WorkflowState.PAUSED,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                initiator=ActorType.HUMAN,
                initiator_id=actor,
                reason="Administrator paused workflow.",
            ),
        )

    def resume(
        self, workflow_id: str, *, expected_version: int, actor: str, idempotency_key: str
    ) -> WorkflowSnapshot:
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            if build.current_stage != WorkflowState.PAUSED.value or not build.paused_from_state:
                raise InvalidTransition("Only a paused workflow can be resumed.")
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState(build.paused_from_state),
                    expected_version=expected_version,
                    idempotency_key=idempotency_key,
                    initiator=ActorType.HUMAN,
                    initiator_id=actor,
                    reason="Administrator resumed preserved workflow stage.",
                ),
                resume=True,
            )

    def cancel(
        self, workflow_id: str, *, expected_version: int, actor: str, idempotency_key: str
    ) -> WorkflowSnapshot:
        return self.transition(
            workflow_id,
            TransitionRequest(
                target_state=WorkflowState.CANCELLED,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                initiator=ActorType.HUMAN,
                initiator_id=actor,
                reason="Administrator cancelled workflow; audit history is retained.",
            ),
        )

    def create_step(
        self,
        workflow_id: str,
        stage: WorkflowState,
        *,
        idempotency_key: str,
        input_payload: dict,
    ) -> WorkflowStepRow:
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            existing = session.scalar(
                select(WorkflowStepRow).where(
                    WorkflowStepRow.workflow_id == workflow_id,
                    WorkflowStepRow.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return existing
            return self._create_step_in_session(
                session,
                build,
                stage,
                idempotency_key=idempotency_key,
                input_payload=input_payload,
            )

    def complete_step(self, step_id: str, *, output_payload: dict, idempotency_key: str) -> None:
        with self.database.session() as session:
            step = session.get(WorkflowStepRow, step_id)
            if step is None:
                raise WorkflowNotFound("Workflow step was not found.")
            if step.status == StepStatus.COMPLETED.value:
                return
            if step.status != StepStatus.RUNNING.value:
                raise WorkflowConflict("Only a running step can be completed.")
            step.status = StepStatus.COMPLETED.value
            step.completed_at = utc_text()
            step.output_json = canonical_json(versioned_payload(**output_payload))
            build = require_build(session, step.workflow_id)
            append_event(
                session,
                build,
                event_type="workflow.step.completed",
                actor_type=ActorType.ORCHESTRATOR.value,
                actor_id="workflow-service",
                idempotency_key=idempotency_key,
                payload={"step_id": step.id, "stage": step.stage, "attempt": step.attempt},
                from_state=build.current_stage,
                to_state=build.current_stage,
            )

    def create_approval(self, request: ApprovalRequest, *, idempotency_key: str) -> dict:
        with self.database.session() as session:
            build = require_build(session, request.workflow_id)
            return self._approval_dict(
                self._create_approval(session, build, request, idempotency_key=idempotency_key)
            )

    def list_approvals(self, workflow_id: str, *, pending_only: bool = False) -> list[dict]:
        with self.database.session() as session:
            require_build(session, workflow_id)
            statement = select(ApprovalRow).where(ApprovalRow.workflow_id == workflow_id)
            if pending_only:
                statement = statement.where(ApprovalRow.status == ApprovalStatus.PENDING.value)
            rows = session.scalars(statement.order_by(ApprovalRow.created_at, ApprovalRow.id)).all()
            return [self._approval_dict(row) for row in rows]

    def get_approval(self, approval_id: str) -> dict:
        with self.database.session() as session:
            row = session.get(ApprovalRow, approval_id)
            if row is None:
                raise WorkflowNotFound("Approval was not found.")
            return self._approval_dict(row)

    def decide_approval(self, approval_id: str, decision: ApprovalDecision) -> WorkflowSnapshot:
        with self.database.session() as session:
            approval = session.get(ApprovalRow, approval_id)
            if approval is None:
                raise WorkflowNotFound("Approval was not found.")
            build = require_build(session, approval.workflow_id)
            return self._record_decision(session, build, approval, decision, advance=True)

    def timeline(self, workflow_id: str) -> list[dict]:
        with self.database.session() as session:
            require_build(session, workflow_id)
            rows = session.scalars(
                select(WorkflowEventRow)
                .where(WorkflowEventRow.workflow_id == workflow_id)
                .order_by(WorkflowEventRow.sequence)
            ).all()
            return [
                {
                    "schema_version": SCHEMA_VERSION,
                    "id": row.id,
                    "sequence": row.sequence,
                    "event_type": row.event_type,
                    "actor_type": row.actor_type,
                    "actor_id": row.actor_id,
                    "from_state": row.from_state,
                    "to_state": row.to_state,
                    "payload": load_versioned_json(row.payload_json),
                    "event_hash": row.event_hash,
                    "previous_event_hash": row.previous_event_hash,
                    "created_at": row.created_at,
                }
                for row in rows
            ]

    def steps(self, workflow_id: str) -> list[dict]:
        with self.database.session() as session:
            require_build(session, workflow_id)
            rows = session.scalars(
                select(WorkflowStepRow)
                .where(WorkflowStepRow.workflow_id == workflow_id)
                .order_by(WorkflowStepRow.started_at, WorkflowStepRow.id)
            ).all()
            return [self._step_dict(row) for row in rows]

    def agent_runs(self, workflow_id: str) -> list[dict]:
        with self.database.session() as session:
            require_build(session, workflow_id)
            rows = session.scalars(
                select(AgentRunRow)
                .where(AgentRunRow.workflow_id == workflow_id)
                .order_by(AgentRunRow.created_at, AgentRunRow.id)
            ).all()
            return [self._agent_run_dict(session, row, include_trace=False) for row in rows]

    def agent_run(self, run_id: str) -> dict:
        with self.database.session() as session:
            row = session.get(AgentRunRow, run_id)
            if row is None:
                raise WorkflowNotFound("Agent run was not found.")
            return self._agent_run_dict(session, row, include_trace=True)

    def errors(self, workflow_id: str) -> list[dict]:
        with self.database.session() as session:
            require_build(session, workflow_id)
            rows = session.scalars(
                select(WorkflowErrorRow)
                .where(WorkflowErrorRow.workflow_id == workflow_id)
                .order_by(WorkflowErrorRow.created_at, WorkflowErrorRow.id)
            ).all()
            return [
                {
                    "schema_version": SCHEMA_VERSION,
                    "id": row.id,
                    "step_id": row.step_id,
                    "code": row.code,
                    "category": row.category,
                    "retryable": bool(row.retryable),
                    "safe_message": row.safe_message,
                    "detail": load_versioned_json(row.detail_json),
                    "created_at": row.created_at,
                }
                for row in rows
            ]

    def simulate_failure(
        self, workflow_id: str, *, expected_version: int, actor: str, idempotency_key: str
    ) -> WorkflowSnapshot:
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            if WorkflowState(build.current_stage) in {
                WorkflowState.DRAFT,
                WorkflowState.PAUSED,
                WorkflowState.FAILED,
                WorkflowState.CANCELLED,
                WorkflowState.COMPLETED,
                WorkflowState.REGISTERING,
            }:
                raise InvalidTransition("Controlled failure is unavailable in this stage.")
            step = session.scalar(
                select(WorkflowStepRow)
                .where(WorkflowStepRow.workflow_id == workflow_id)
                .order_by(WorkflowStepRow.started_at.desc())
                .limit(1)
            )
            error_id = deterministic_id("err", workflow_id, idempotency_key)
            if session.get(WorkflowErrorRow, error_id) is None:
                error = WorkflowErrorRow(
                    id=error_id,
                    workflow_id=workflow_id,
                    step_id=step.id if step else None,
                    agent_run_id=None,
                    tool_call_id=None,
                    code="phase0_controlled_failure",
                    category="demonstration",
                    retryable=1,
                    safe_message="Prepared controlled Phase-0 failure; retry is allowed.",
                    detail_json=canonical_json(
                        versioned_payload(reason="Explicit local-admin demonstration action.")
                    ),
                    created_at=utc_text(),
                )
                session.add(error)
                if step and step.status == StepStatus.RUNNING.value:
                    step.status = StepStatus.FAILED_RETRYABLE.value
                    step.error_id = error_id
                    step.completed_at = utc_text()
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.FAILED,
                    expected_version=expected_version,
                    idempotency_key=idempotency_key,
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id=actor,
                    reason="Prepared controlled failure for restart/retry demonstration.",
                ),
            )

    def retry_failed(
        self, workflow_id: str, *, expected_version: int, actor: str, idempotency_key: str
    ) -> WorkflowSnapshot:
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            if build.current_stage != WorkflowState.FAILED.value or not build.failed_from_state:
                raise InvalidTransition("Only a failed retryable workflow can be retried.")
            retryable = session.scalar(
                select(WorkflowErrorRow)
                .where(WorkflowErrorRow.workflow_id == workflow_id)
                .order_by(WorkflowErrorRow.created_at.desc(), WorkflowErrorRow.id.desc())
                .limit(1)
            )
            if retryable is None or not bool(retryable.retryable):
                raise GuardNotSatisfied("The most recent workflow failure is not retryable.")
            snapshot = self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState(build.failed_from_state),
                    expected_version=expected_version,
                    idempotency_key=idempotency_key,
                    initiator=ActorType.HUMAN,
                    initiator_id=actor,
                    reason="Administrator authorized a new idempotent step attempt.",
                ),
                retry=True,
            )
            return snapshot

    def recover_interrupted(self) -> int:
        recovered = 0
        with self.database.session() as session:
            steps = session.scalars(
                select(WorkflowStepRow).where(WorkflowStepRow.status == StepStatus.RUNNING.value)
            ).all()
            workflow_ids = sorted({step.workflow_id for step in steps})
            for workflow_id in workflow_ids:
                build = require_build(session, workflow_id)
                original = build.current_stage
                workflow_steps = [step for step in steps if step.workflow_id == workflow_id]
                latest_error = session.scalar(
                    select(WorkflowErrorRow)
                    .where(WorkflowErrorRow.workflow_id == workflow_id)
                    .order_by(WorkflowErrorRow.created_at.desc(), WorkflowErrorRow.id.desc())
                    .limit(1)
                )
                repair_retryable = (
                    bool(latest_error.retryable)
                    if original == WorkflowState.FAILED.value and latest_error is not None
                    else True
                )
                for step in workflow_steps:
                    error_id = deterministic_id("err", build.id, step.id, "startup-recovery")
                    if session.get(WorkflowErrorRow, error_id) is None:
                        session.add(
                            WorkflowErrorRow(
                                id=error_id,
                                workflow_id=build.id,
                                step_id=step.id,
                                agent_run_id=None,
                                tool_call_id=None,
                                code="interrupted_by_restart",
                                category="recovery",
                                retryable=int(repair_retryable),
                                safe_message=(
                                    "A stale running attempt was interrupted during backend "
                                    "restart recovery."
                                ),
                                detail_json=canonical_json(
                                    versioned_payload(
                                        previous_stage=original,
                                        repaired_status=StepStatus.INTERRUPTED.value,
                                    )
                                ),
                                created_at=utc_text(),
                            )
                        )
                    step.status = StepStatus.INTERRUPTED.value
                    step.error_id = error_id
                    step.completed_at = utc_text()
                    append_event(
                        session,
                        build,
                        event_type="workflow.step.interrupted",
                        actor_type=ActorType.SYSTEM.value,
                        actor_id="startup-recovery",
                        idempotency_key=f"startup-recovery:{step.id}",
                        payload={
                            "step_id": step.id,
                            "stage": step.stage,
                            "attempt": step.attempt,
                            "repair": "backend_restart",
                        },
                        from_state=original,
                        to_state=original,
                    )
                    recovered += 1
                if original not in {
                    WorkflowState.FAILED.value,
                    WorkflowState.CANCELLED.value,
                    WorkflowState.COMPLETED.value,
                }:
                    current_version = build.version
                    result = session.execute(
                        update(EndpointBuildRow)
                        .where(
                            EndpointBuildRow.id == build.id,
                            EndpointBuildRow.version == current_version,
                        )
                        .values(
                            failed_from_state=original,
                            current_stage=WorkflowState.FAILED.value,
                            status=WorkflowStatus.FAILED.value,
                            updated_at=utc_text(),
                            version=current_version + 1,
                        )
                    )
                    if result.rowcount != 1:
                        raise StaleWorkflowVersion("Concurrent startup recovery detected.")
                    session.refresh(build)
                    append_event(
                        session,
                        build,
                        event_type="workflow.recovered_interruption",
                        actor_type=ActorType.SYSTEM.value,
                        actor_id="startup-recovery",
                        idempotency_key=f"startup-recovery:{build.id}:failed",
                        payload={
                            "step_ids": [step.id for step in workflow_steps],
                            "failed_from_state": original,
                        },
                        from_state=original,
                        to_state=WorkflowState.FAILED.value,
                    )
        return recovered

    def _apply_transition(
        self,
        session: Session,
        build: EndpointBuildRow,
        request: TransitionRequest,
        *,
        resume: bool = False,
        retry: bool = False,
    ) -> WorkflowSnapshot:
        existing = session.scalar(
            select(WorkflowEventRow).where(
                WorkflowEventRow.workflow_id == build.id,
                WorkflowEventRow.idempotency_key == request.idempotency_key,
            )
        )
        if existing is not None:
            payload = load_versioned_json(existing.payload_json)
            if payload.get("target_state") != request.target_state.value:
                raise WorkflowConflict("Idempotency key was already used for another transition.")
            return self._snapshot(session, build)
        if build.version != request.expected_version:
            raise StaleWorkflowVersion(
                "Workflow version is stale.",
                detail={
                    "expected_version": request.expected_version,
                    "current_version": build.version,
                },
            )
        current = WorkflowState(build.current_stage)
        if current in {WorkflowState.COMPLETED, WorkflowState.CANCELLED}:
            raise InvalidTransition(f"{current.value} is terminal.")
        if current is WorkflowState.PAUSED and not resume:
            raise InvalidTransition("A paused workflow must be resumed or cancelled.")
        if (
            current is WorkflowState.FAILED
            and not retry
            and request.target_state is not WorkflowState.CANCELLED
        ):
            raise InvalidTransition("A failed workflow must be retried or cancelled.")
        if resume:
            if build.paused_from_state != request.target_state.value:
                raise InvalidTransition("Resume target does not match the preserved stage.")
            spec = None
        elif retry:
            if build.failed_from_state != request.target_state.value:
                raise InvalidTransition("Retry target does not match the failed stage.")
            spec = None
        else:
            spec = self.graph.transition(current, request.target_state, request.initiator)
        self._validate_guards(session, build, spec, request)

        now = utc_text()
        values: dict = {
            "current_stage": request.target_state.value,
            "status": self._status_for(request.target_state).value,
            "version": build.version + 1,
            "updated_at": now,
        }
        if request.target_state is WorkflowState.PAUSED:
            values.update(paused_from_state=current.value, paused_at=now)
        elif resume:
            values.update(paused_from_state=None, paused_at=None)
        if request.target_state is WorkflowState.FAILED:
            values["failed_from_state"] = current.value
        elif retry:
            values["failed_from_state"] = None
        if request.target_state is WorkflowState.CANCELLED:
            values["cancelled_at"] = now
        if request.target_state is WorkflowState.COMPLETED:
            values["completed_at"] = now
        result = session.execute(
            update(EndpointBuildRow)
            .where(EndpointBuildRow.id == build.id, EndpointBuildRow.version == build.version)
            .values(**values)
        )
        if result.rowcount != 1:
            raise StaleWorkflowVersion("Concurrent workflow update detected.")
        session.flush()
        for key, value in values.items():
            setattr(build, key, value)
        append_event(
            session,
            build,
            event_type=(
                "workflow.resumed"
                if resume
                else "workflow.retried"
                if retry
                else "workflow.transitioned"
            ),
            actor_type=request.initiator.value,
            actor_id=request.initiator_id,
            idempotency_key=request.idempotency_key,
            payload={
                "target_state": request.target_state.value,
                "reason": request.reason,
                "artifact_hashes": request.artifact_hashes,
                "new_version": build.version,
            },
            from_state=current.value,
            to_state=request.target_state.value,
        )
        return self._snapshot(session, build)

    def _validate_guards(
        self,
        session: Session,
        build: EndpointBuildRow,
        spec: TransitionSpec | None,
        request: TransitionRequest,
    ) -> None:
        if spec is None:
            return
        missing: list[str] = []
        for required in spec.required_artifacts:
            approval_type = APPROVAL_ARTIFACT_TYPES.get(required)
            if approval_type:
                found = session.scalar(
                    select(ApprovalRow).where(
                        ApprovalRow.workflow_id == build.id,
                        ApprovalRow.approval_type == approval_type.value,
                        ApprovalRow.status.in_(
                            [
                                ApprovalStatus.APPROVED.value,
                                ApprovalStatus.ALTERNATIVE_SELECTED.value,
                            ]
                        ),
                    )
                )
            elif required == "review_decision":
                found = session.scalar(
                    select(HumanDecisionRow).where(HumanDecisionRow.workflow_id == build.id)
                )
            else:
                found = session.scalar(
                    select(ArtifactRow).where(
                        ArtifactRow.workflow_id == build.id,
                        ArtifactRow.artifact_type == required,
                    )
                )
            if found is None:
                missing.append(required)
        if request.artifact_hashes:
            available = set(
                session.scalars(
                    select(ArtifactRow.sha256).where(ArtifactRow.workflow_id == build.id)
                ).all()
            )
            missing_hashes = sorted(set(request.artifact_hashes) - available)
            missing.extend(f"sha256:{value}" for value in missing_hashes)
        if missing:
            raise GuardNotSatisfied(
                "Workflow transition prerequisites are not satisfied.",
                detail={"missing": missing, "guard": spec.guard},
            )

    def _create_approval(
        self,
        session: Session,
        build: EndpointBuildRow,
        request: ApprovalRequest,
        *,
        idempotency_key: str,
        supersedes_id: str | None = None,
    ) -> ApprovalRow:
        if request.workflow_id != build.id:
            raise WorkflowConflict("Approval workflow binding does not match.")
        available = set(
            session.scalars(
                select(ArtifactRow.sha256).where(ArtifactRow.workflow_id == build.id)
            ).all()
        )
        if not set(request.artifact_hashes).issubset(available):
            raise GuardNotSatisfied("Approval references unknown or stale artifact hashes.")
        proposal_hash = hashlib.sha256(canonical_json(request).encode()).hexdigest()
        approval_id = deterministic_id(
            "approval", build.id, request.approval_type.value, proposal_hash
        )
        existing = session.get(ApprovalRow, approval_id)
        if existing is not None:
            return existing
        approval = ApprovalRow(
            id=approval_id,
            workflow_id=build.id,
            stage=request.stage.value,
            approval_type=request.approval_type.value,
            status=ApprovalStatus.PENDING.value,
            proposal_hash=proposal_hash,
            request_json=request.model_dump_json(),
            decision_json=None,
            created_at=utc_text(),
            decided_at=None,
            supersedes_id=supersedes_id,
        )
        session.add(approval)
        session.flush()
        append_event(
            session,
            build,
            event_type="approval.created",
            actor_type=ActorType.ORCHESTRATOR.value,
            actor_id="workflow-service",
            idempotency_key=idempotency_key,
            payload={
                "approval_id": approval.id,
                "approval_type": approval.approval_type,
                "proposal_hash": proposal_hash,
                "artifact_hashes": request.artifact_hashes,
            },
            from_state=build.current_stage,
            to_state=build.current_stage,
        )
        return approval

    def _record_decision(
        self,
        session: Session,
        build: EndpointBuildRow,
        approval: ApprovalRow,
        decision: ApprovalDecision,
        *,
        advance: bool,
    ) -> WorkflowSnapshot:
        existing = session.scalar(
            select(WorkflowEventRow).where(
                WorkflowEventRow.workflow_id == build.id,
                WorkflowEventRow.idempotency_key == decision.idempotency_key,
            )
        )
        if existing is not None:
            return self._snapshot(session, build)
        if approval.status != ApprovalStatus.PENDING.value:
            raise WorkflowConflict("Approval decision is immutable and has already been recorded.")
        if build.version != decision.expected_version:
            raise StaleWorkflowVersion(
                "Workflow version is stale.", detail={"current_version": build.version}
            )
        if decision.decision is not ApprovalDecisionValue.APPROVE and not decision.reviewer_comment:
            raise WorkflowConflict("Reviewer comment is required for this decision.")
        request = ApprovalRequest.model_validate_json(approval.request_json)
        if sorted(request.artifact_hashes) != sorted(decision.artifact_hashes):
            raise GuardNotSatisfied("Approval artifact hashes do not match the bound proposal.")
        available = set(
            session.scalars(
                select(ArtifactRow.sha256).where(ArtifactRow.workflow_id == build.id)
            ).all()
        )
        if not set(decision.artifact_hashes).issubset(available):
            raise GuardNotSatisfied("Approval references artifacts that are no longer current.")
        approved_specification_policy = None
        if (
            approval.approval_type == ApprovalType.DATASET_SPECIFICATION.value
            and decision.decision
            in {ApprovalDecisionValue.APPROVE, ApprovalDecisionValue.CHOOSE_ALTERNATIVE}
            and decision.dataset_specification_policy is not None
        ):
            approved_specification_policy = (
                TrainingDatasetSpecificationApprovalPolicy.model_validate(
                    decision.dataset_specification_policy
                )
            )
        status_by_decision = {
            ApprovalDecisionValue.APPROVE: ApprovalStatus.APPROVED,
            ApprovalDecisionValue.REJECT: ApprovalStatus.REJECTED,
            ApprovalDecisionValue.REQUEST_REVISION: ApprovalStatus.REVISION_REQUESTED,
            ApprovalDecisionValue.CHOOSE_ALTERNATIVE: ApprovalStatus.ALTERNATIVE_SELECTED,
            ApprovalDecisionValue.CANCEL_WORKFLOW: ApprovalStatus.CANCELLED,
        }
        if (
            decision.decision is ApprovalDecisionValue.CHOOSE_ALTERNATIVE
            and not decision.selected_alternative_id
        ):
            raise WorkflowConflict("Choosing an alternative requires an alternative ID.")
        approval.status = status_by_decision[decision.decision].value
        approval.decision_json = decision.model_dump_json()
        approval.decided_at = utc_text()
        human_decision = HumanDecisionRow(
            id=deterministic_id("decision", approval.id, decision.idempotency_key),
            workflow_id=build.id,
            approval_id=approval.id,
            reviewer_id=decision.reviewer_id,
            decision=decision.decision.value,
            payload_json=decision.model_dump_json(),
            created_at=approval.decided_at,
        )
        session.add(human_decision)
        session.flush()
        append_event(
            session,
            build,
            event_type="approval.decided",
            actor_type=ActorType.HUMAN.value,
            actor_id=decision.reviewer_id,
            idempotency_key=decision.idempotency_key,
            payload={
                "approval_id": approval.id,
                "decision_id": human_decision.id,
                "decision": decision.decision.value,
                "artifact_hashes": decision.artifact_hashes,
                "reviewer_comment": decision.reviewer_comment,
                "selected_alternative_id": decision.selected_alternative_id,
            },
            from_state=build.current_stage,
            to_state=build.current_stage,
        )
        if not advance:
            return self._snapshot(session, build)
        if approval.approval_type == ApprovalType.DATASET_SPECIFICATION.value:
            transition_hashes = list(decision.artifact_hashes)
            if decision.decision in {
                ApprovalDecisionValue.APPROVE,
                ApprovalDecisionValue.CHOOSE_ALTERNATIVE,
            }:
                workflow = session.get(TrainingDatasetWorkflowRow, build.id)
                if workflow is None or not workflow.specification_draft_json:
                    raise GuardNotSatisfied("Approved specification draft is missing.")
                draft = TrainingDatasetSpecificationDraft.model_validate(
                    _load_training_document(workflow.specification_draft_json)
                )
                if approved_specification_policy is not None:
                    policy_payload = approved_specification_policy.model_dump(mode="json")
                    policy_artifact = self.artifact_store._put_bytes(
                        session,
                        workflow_id=build.id,
                        content=canonical_json(policy_payload).encode(),
                        mime_type="application/json",
                        artifact_type="dataset_specification_human_policy",
                        logical_name=(
                            "dataset-specification-human-policy-"
                            f"{approved_specification_policy.policy_version}.json"
                        ),
                        producer=decision.reviewer_id,
                        idempotency_key=f"{decision.idempotency_key}:human-policy",
                    )
                    transition_hashes.append(policy_artifact.sha256)
                    append_event(
                        session,
                        build,
                        event_type="training_dataset.human_policy.persisted",
                        actor_type=ActorType.HUMAN.value,
                        actor_id=decision.reviewer_id,
                        idempotency_key=f"{decision.idempotency_key}:human-policy:event",
                        payload={
                            "artifact_id": policy_artifact.id,
                            "sha256": policy_artifact.sha256,
                            "policy_version": approved_specification_policy.policy_version,
                        },
                        from_state=build.current_stage,
                        to_state=build.current_stage,
                    )
                specification = materialize_training_dataset_specification(
                    draft,
                    specification_id=deterministic_id("spec", build.id, approval.proposal_hash),
                    approved_policy=approved_specification_policy,
                )
                payload = specification.model_dump(mode="json")
                artifact = self.artifact_store._put_bytes(
                    session,
                    workflow_id=build.id,
                    content=canonical_json(payload).encode(),
                    mime_type="application/json",
                    artifact_type="training_dataset_specification",
                    logical_name=f"training-dataset-specification-{specification.specification_id}.json",
                    producer="deterministic-orchestrator",
                    idempotency_key=f"{decision.idempotency_key}:approved-contract",
                )
                workflow.specification_json = canonical_json(versioned_payload(document=payload))
                workflow.updated_at = utc_text()
                transition_hashes.append(artifact.sha256)
                append_event(
                    session,
                    build,
                    event_type="training_dataset.approved_contract_materialized",
                    actor_type=ActorType.ORCHESTRATOR.value,
                    actor_id="deterministic-orchestrator",
                    idempotency_key=f"{decision.idempotency_key}:approved-contract:event",
                    payload={
                        "artifact_id": artifact.id,
                        "sha256": artifact.sha256,
                        "policy_derived_thresholds": False,
                    },
                    from_state=build.current_stage,
                    to_state=build.current_stage,
                )
            revision_target = (
                WorkflowState.DRAFT
                if build.current_stage == WorkflowState.AWAITING_DATASET_SPECIFICATION_REVIEW.value
                else WorkflowState.SPECIFYING_TARGET_DATASET
            )
            target_by_decision = {
                ApprovalDecisionValue.APPROVE: WorkflowState.DERIVING_COMPONENT_REQUIREMENTS,
                ApprovalDecisionValue.CHOOSE_ALTERNATIVE: (
                    WorkflowState.DERIVING_COMPONENT_REQUIREMENTS
                ),
                ApprovalDecisionValue.REQUEST_REVISION: revision_target,
                ApprovalDecisionValue.REJECT: WorkflowState.CANCELLED,
                ApprovalDecisionValue.CANCEL_WORKFLOW: WorkflowState.CANCELLED,
            }
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=target_by_decision[decision.decision],
                    expected_version=build.version,
                    idempotency_key=f"{decision.idempotency_key}:transition",
                    initiator=ActorType.HUMAN,
                    initiator_id=decision.reviewer_id,
                    reason=f"Dataset specification decision: {decision.decision.value}.",
                    artifact_hashes=transition_hashes,
                ),
            )
        if approval.approval_type == ApprovalType.TRAINING_DATASET_ASSEMBLY_STRATEGY.value:
            target_by_decision = {
                ApprovalDecisionValue.APPROVE: WorkflowState.COMPLETED,
                ApprovalDecisionValue.CHOOSE_ALTERNATIVE: WorkflowState.COMPLETED,
                ApprovalDecisionValue.REQUEST_REVISION: (
                    WorkflowState.PLANNING_ASSEMBLY_STRATEGIES
                ),
                ApprovalDecisionValue.REJECT: WorkflowState.CANCELLED,
                ApprovalDecisionValue.CANCEL_WORKFLOW: WorkflowState.CANCELLED,
            }
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=target_by_decision[decision.decision],
                    expected_version=build.version,
                    idempotency_key=f"{decision.idempotency_key}:transition",
                    initiator=ActorType.HUMAN,
                    initiator_id=decision.reviewer_id,
                    reason=f"Assembly strategy decision: {decision.decision.value}.",
                    artifact_hashes=decision.artifact_hashes,
                ),
            )
        if approval.approval_type == ApprovalType.SEARCH_REVISION.value:
            if decision.decision not in {
                ApprovalDecisionValue.REQUEST_REVISION,
                ApprovalDecisionValue.REJECT,
                ApprovalDecisionValue.CANCEL_WORKFLOW,
            }:
                raise InvalidTransition(
                    "Search review permits only revised search, rejection, or cancellation."
                )
            target = (
                WorkflowState.DISCOVERING_DATA
                if decision.decision is ApprovalDecisionValue.REQUEST_REVISION
                else WorkflowState.CANCELLED
            )
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=target,
                    expected_version=build.version,
                    idempotency_key=f"{decision.idempotency_key}:transition",
                    initiator=ActorType.HUMAN,
                    initiator_id=decision.reviewer_id,
                    reason=f"Search review decision: {decision.decision.value}.",
                    artifact_hashes=decision.artifact_hashes,
                ),
            )
        if approval.approval_type != ApprovalType.DATASET_SELECTION.value:
            raise InvalidTransition("This approval type does not advance the Phase-1 workflow.")
        target_by_decision = {
            ApprovalDecisionValue.APPROVE: WorkflowState.CURATING_DATA,
            ApprovalDecisionValue.CHOOSE_ALTERNATIVE: WorkflowState.CURATING_DATA,
            ApprovalDecisionValue.REQUEST_REVISION: WorkflowState.DISCOVERING_DATA,
            ApprovalDecisionValue.REJECT: WorkflowState.CANCELLED,
            ApprovalDecisionValue.CANCEL_WORKFLOW: WorkflowState.CANCELLED,
        }
        return self._apply_transition(
            session,
            build,
            TransitionRequest(
                target_state=target_by_decision[decision.decision],
                expected_version=build.version,
                idempotency_key=f"{decision.idempotency_key}:transition",
                initiator=ActorType.HUMAN,
                initiator_id=decision.reviewer_id,
                reason=f"Dataset approval decision: {decision.decision.value}.",
                artifact_hashes=decision.artifact_hashes,
            ),
        )

    def _snapshot(self, session: Session, row: EndpointBuildRow) -> WorkflowSnapshot:
        pending = session.scalar(
            select(ApprovalRow.id)
            .where(
                ApprovalRow.workflow_id == row.id,
                ApprovalRow.status == ApprovalStatus.PENDING.value,
            )
            .order_by(ApprovalRow.created_at.desc())
            .limit(1)
        )
        state = WorkflowState(row.current_stage)
        progress_state = state
        if state is WorkflowState.PAUSED and row.paused_from_state:
            progress_state = WorkflowState(row.paused_from_state)
        if state is WorkflowState.FAILED and row.failed_from_state:
            progress_state = WorkflowState(row.failed_from_state)
        return WorkflowSnapshot(
            id=row.id,
            endpoint_name=row.endpoint_name,
            endpoint_slug=row.endpoint_slug,
            biological_goal=row.biological_goal,
            workflow_kind=WorkflowKind(row.workflow_kind),
            benchmark_mode=row.benchmark_mode,
            state=state,
            status=WorkflowStatus(row.status),
            current_stage=state,
            version=row.version,
            progress=STATE_PROGRESS[progress_state],
            pending_approval_id=pending,
            paused_from_state=(
                WorkflowState(row.paused_from_state) if row.paused_from_state else None
            ),
            failed_from_state=(
                WorkflowState(row.failed_from_state) if row.failed_from_state else None
            ),
            created_by=row.created_by,
            created_at=parse_utc(row.created_at),
            updated_at=parse_utc(row.updated_at),
            paused_at=parse_utc(row.paused_at),
            cancelled_at=parse_utc(row.cancelled_at),
            completed_at=parse_utc(row.completed_at),
        )

    def _approval_dict(self, row: ApprovalRow) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": row.id,
            "workflow_id": row.workflow_id,
            "stage": row.stage,
            "approval_type": row.approval_type,
            "status": row.status,
            "proposal_hash": row.proposal_hash,
            "request": ApprovalRequest.model_validate_json(row.request_json).model_dump(
                mode="json"
            ),
            "decision": (
                ApprovalDecision.model_validate_json(row.decision_json).model_dump(mode="json")
                if row.decision_json
                else None
            ),
            "created_at": row.created_at,
            "decided_at": row.decided_at,
            "supersedes_id": row.supersedes_id,
        }

    @staticmethod
    def _step_dict(row: WorkflowStepRow) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": row.id,
            "workflow_id": row.workflow_id,
            "stage": row.stage,
            "attempt": row.attempt,
            "status": row.status,
            "idempotency_key": row.idempotency_key,
            "started_at": row.started_at,
            "completed_at": row.completed_at,
            "heartbeat_at": row.heartbeat_at,
            "input": load_versioned_json(row.input_json),
            "output": load_versioned_json(row.output_json) if row.output_json else None,
            "error_id": row.error_id,
        }

    @staticmethod
    def _agent_run_dict(session: Session, row: AgentRunRow, *, include_trace: bool) -> dict:
        request_payload = load_versioned_json(row.request_json)
        context = request_payload.get("context")
        run_mode = context.get("run_mode") if isinstance(context, dict) else None
        if run_mode not in {"live", "cached", "replay"}:
            run_mode = None
        tool_calls = session.scalars(
            select(ToolCallRow)
            .where(ToolCallRow.agent_run_id == row.id)
            .order_by(ToolCallRow.created_at, ToolCallRow.id)
        ).all()
        result = {
            "schema_version": SCHEMA_VERSION,
            "id": row.id,
            "workflow_id": row.workflow_id,
            "step_id": row.step_id,
            "agent_name": row.agent_name,
            "agent_version": row.agent_version,
            "provider": row.provider,
            "model_identifier": row.model_identifier,
            "run_mode": run_mode,
            "instruction_version": row.instruction_version,
            "input_hash": row.input_hash,
            "status": row.status,
            "usage": load_versioned_json(row.usage_json),
            "turns": row.turns,
            "duration_ms": row.duration_ms,
            "created_at": row.created_at,
            "completed_at": row.completed_at,
            "tools": [
                {
                    "id": call.id,
                    "tool_name": call.tool_name,
                    "tool_version": call.tool_version,
                    "status": call.status,
                    "duration_ms": call.duration_ms,
                    "created_at": call.created_at,
                }
                for call in tool_calls
            ],
        }
        if include_trace:
            result.update(
                request=request_payload,
                result=load_versioned_json(row.result_json) if row.result_json else None,
                trace=load_versioned_json(row.trace_json),
                tool_calls=[
                    {
                        "schema_version": SCHEMA_VERSION,
                        "id": call.id,
                        "tool_name": call.tool_name,
                        "tool_version": call.tool_version,
                        "status": call.status,
                        "permission_scope": load_versioned_json(call.permission_scope_json),
                        "arguments": load_versioned_json(call.arguments_json),
                        "result": load_versioned_json(call.result_json)
                        if call.result_json
                        else None,
                        "duration_ms": call.duration_ms,
                    }
                    for call in tool_calls
                ],
            )
        return result

    @staticmethod
    def _status_for(state: WorkflowState) -> WorkflowStatus:
        if state is WorkflowState.DRAFT:
            return WorkflowStatus.DRAFT
        if state in {
            WorkflowState.AWAITING_DATASET_SPECIFICATION_REVIEW,
            WorkflowState.AWAITING_DATASET_SPECIFICATION_APPROVAL,
            WorkflowState.AWAITING_DATASET_SPECIFICATION_REVISION,
            WorkflowState.AWAITING_SOURCE_INVENTORY_REVIEW,
            WorkflowState.AWAITING_SOURCE_DISCOVERY_REVISION,
            WorkflowState.SOURCE_ACCESS_BLOCKED,
            WorkflowState.AWAITING_DATASET_APPROVAL,
            WorkflowState.AWAITING_SEARCH_REVIEW,
            WorkflowState.AWAITING_LABEL_APPROVAL,
            WorkflowState.AWAITING_TRAINING_APPROVAL,
            WorkflowState.AWAITING_SCIENTIFIC_APPROVAL,
        }:
            return WorkflowStatus.WAITING
        if state is WorkflowState.PAUSED:
            return WorkflowStatus.PAUSED
        if state is WorkflowState.FAILED:
            return WorkflowStatus.FAILED
        if state is WorkflowState.CANCELLED:
            return WorkflowStatus.CANCELLED
        if state is WorkflowState.COMPLETED:
            return WorkflowStatus.COMPLETED
        return WorkflowStatus.ACTIVE

    def _create_step_in_session(
        self,
        session: Session,
        build: EndpointBuildRow,
        stage: WorkflowState,
        *,
        idempotency_key: str,
        input_payload: dict,
    ) -> WorkflowStepRow:
        attempt = (
            int(
                session.scalar(
                    select(func.coalesce(func.max(WorkflowStepRow.attempt), 0)).where(
                        WorkflowStepRow.workflow_id == build.id,
                        WorkflowStepRow.stage == stage.value,
                    )
                )
                or 0
            )
            + 1
        )
        now = utc_text()
        active_steps = session.scalars(
            select(WorkflowStepRow).where(
                WorkflowStepRow.workflow_id == build.id,
                WorkflowStepRow.stage == stage.value,
                WorkflowStepRow.status == StepStatus.RUNNING.value,
            )
        ).all()
        for active in active_steps:
            error_id = deterministic_id("err", active.id, "superseded", str(attempt))
            session.add(
                WorkflowErrorRow(
                    id=error_id,
                    workflow_id=build.id,
                    step_id=active.id,
                    agent_run_id=None,
                    tool_call_id=None,
                    code="step_attempt_superseded",
                    category="consistency",
                    retryable=0,
                    safe_message="An older running attempt was superseded by a newer attempt.",
                    detail_json=canonical_json(
                        versioned_payload(
                            stage=stage.value,
                            old_attempt=active.attempt,
                            new_attempt=attempt,
                        )
                    ),
                    created_at=now,
                )
            )
            active.status = StepStatus.INTERRUPTED.value
            active.error_id = error_id
            active.completed_at = now
            append_event(
                session,
                build,
                event_type="workflow.step.superseded",
                actor_type=ActorType.ORCHESTRATOR.value,
                actor_id="workflow-service",
                idempotency_key=f"step-superseded:{active.id}:{attempt}",
                payload={
                    "step_id": active.id,
                    "stage": stage.value,
                    "old_attempt": active.attempt,
                    "new_attempt": attempt,
                },
                from_state=build.current_stage,
                to_state=build.current_stage,
            )
        step = WorkflowStepRow(
            id=deterministic_id("step", build.id, stage.value, str(attempt)),
            workflow_id=build.id,
            stage=stage.value,
            attempt=attempt,
            status=StepStatus.RUNNING.value,
            idempotency_key=idempotency_key,
            started_at=now,
            completed_at=None,
            heartbeat_at=now,
            input_json=canonical_json(versioned_payload(**input_payload)),
            output_json=None,
            error_id=None,
        )
        session.add(step)
        session.flush()
        append_event(
            session,
            build,
            event_type="workflow.step.started",
            actor_type=ActorType.ORCHESTRATOR.value,
            actor_id="workflow-service",
            idempotency_key=f"{idempotency_key}:started",
            payload={"step_id": step.id, "stage": stage.value, "attempt": attempt},
            from_state=build.current_stage,
            to_state=build.current_stage,
        )
        return step
