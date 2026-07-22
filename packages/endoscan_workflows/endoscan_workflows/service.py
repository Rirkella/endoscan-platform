"""Transactional workflow service. It owns workflow truth but no scientific logic."""

from __future__ import annotations

import hashlib
import io
import json
import pickle
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from endoscan_core.endpoint_building import (
    AssembledDataset,
    assemble_approved_dataset,
    benchmark_models,
    build_explore_payload,
    dataset_quality_summary,
    validate_selected_model,
)
from endoscan_core.registry.schema import (
    EndpointEntry,
    EndpointStatus,
    ExplanationCapability,
    ValidationStatusMetadata,
)
from endoscan_core.registry.store import register_endpoint

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
    ArtifactDescriptor,
    EndpointBuildCreate,
    StepStatus,
    ToolInvocation,
    ToolInvocationFailureDiagnostic,
    TransitionRequest,
    WorkflowKind,
    WorkflowSnapshot,
    WorkflowState,
    WorkflowStatus,
)
from .database import WorkflowDatabase
from .discovery import DiscoveryOutput, discovery_request, validate_evidence_references
from .discovery_strategy import (
    WORKFLOW_SEMANTICS_V2,
    ArtifactReference,
    AssemblyRecipe,
    CombinationCoverageSet,
    DiscoveryBudgets,
    DiscoveryExecutionLedger,
    DiscoveryPlan,
    DiscoveryRevisionRequest,
    HumanApprovalRecord,
    HydratedSourceSet,
    SourceCandidateSet,
    StrategyProposalSet,
    StrategySetRejection,
    build_discovery_plan,
    initial_execution_ledger,
    load_operational_capability_registry,
    validate_candidate_universe,
    validate_coverage_universe,
    validate_execution_ledger,
    validate_hydrated_sources,
    validate_strategy_universe,
)
from .endpoint_lifecycle import (
    BenchmarkComparison,
    BenchmarkPlan,
    DatasetBundleManifest,
    DatasetQualityReport,
    DatasetReviewDecision,
    DeterministicAssemblyStrategyAgent,
    ExplorePublicationManifest,
    FinalValidationReport,
    ModelBenchmarkResult,
    ModelReviewDecision,
    ModelSelectionRecord,
    OfflineAssemblyInput,
    PostApprovalExpressionExtractor,
    PublicationReceipt,
    SelectiveExpressionExtractionRequest,
    SelectiveExpressionExtractionResult,
    StrategyAgentInput,
    ValidationStatusMatrix,
    calculate_combination_coverage,
)
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
from .reviewed_source_adapters import (
    ActivityDiscoveryModality,
    ActivityResultExtractionInput,
    ReviewedSourceAdapterRegistry,
    ReviewedSourceExecutionError,
    ReviewedSourceInvocationError,
    ReviewedSourceOperationInput,
)
from .semantics_v2_executor import SemanticsV2DiscoveryExecutor
from .state_machine import TransitionSpec, WorkflowGraph
from .training_dataset import (
    BLIND_TRAINING_DATASET_DISCOVERY,
    SPECIALIZED_AGENT_SEQUENCE,
    TRAINING_DATASET_CONTRACT_VERSION,
    ActivityEvidenceRow,
    AssemblyGapReport,
    BlindBenchmarkInitialContext,
    CompoundIdentityBridgeRow,
    DatasetSpecificationAgentOutcome,
    DatasetSpecificationCompiler,
    DatasetSpecificationReviewOutcome,
    DatasetSpecificationReviewRecord,
    DatasetSpecificationSemanticValidation,
    DiscoveryAgentReviewOutcome,
    DiscoveryBeforeStrategyGuard,
    EndpointDiscoveryScope,
    SourceCapabilityMatrix,
    SpecializedAgentDefinition,
    TrainingDatasetAssemblyReview,
    TrainingDatasetComponentRequirements,
    TrainingDatasetPreparationPlan,
    TrainingDatasetSpecification,
    TrainingDatasetSpecificationApprovalPolicy,
    TrainingDatasetSpecificationDraft,
    TranscriptomicProfileEvidenceRow,
    VerifiedSourceInventory,
    VerifiedSourceInventoryFragment,
    VerifiedSourceObservation,
    VerifiedSourceObservationBatch,
    VerifiedSourceSearchOutcome,
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
    WorkflowState.APPROVED_SPECIFICATION: 10,
    WorkflowState.DISCOVERY_PLANNING: 15,
    WorkflowState.REGISTERED_PROVIDER_CAPABILITY_BLOCKED: 15,
    WorkflowState.DISCOVERING_SOURCE_CANDIDATES: 25,
    WorkflowState.HYDRATING_SOURCE_CANDIDATES: 40,
    WorkflowState.COMPUTING_COMBINATION_COVERAGE: 58,
    WorkflowState.GENERATING_ASSEMBLY_STRATEGIES: 72,
    WorkflowState.AWAITING_ASSEMBLY_STRATEGY_REVIEW: 48,
    WorkflowState.ASSEMBLY_RECIPE_APPROVED: 55,
    WorkflowState.ASSEMBLING_APPROVED_DATASET: 62,
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

SEMANTICS_V1_ONLY_TRAINING_STATES = {
    WorkflowState.DERIVING_COMPONENT_REQUIREMENTS,
    WorkflowState.DISCOVERING_ACTIVITY_EVIDENCE,
    WorkflowState.DISCOVERING_TRANSCRIPTOMIC_EVIDENCE,
    WorkflowState.DISCOVERING_IDENTITY_AND_STRUCTURE_SOURCES,
    WorkflowState.DISCOVERING_SUPPORTING_METADATA,
    WorkflowState.VALIDATING_DISCOVERED_SOURCES,
    WorkflowState.BUILDING_SOURCE_INVENTORY,
    WorkflowState.AWAITING_SOURCE_INVENTORY_REVIEW,
    WorkflowState.AWAITING_SOURCE_DISCOVERY_REVISION,
    WorkflowState.SOURCE_ACCESS_BLOCKED,
    WorkflowState.PLANNING_ASSEMBLY_STRATEGIES,
    WorkflowState.EVALUATING_JOINABILITY,
    WorkflowState.IDENTIFYING_ASSEMBLY_GAPS,
    WorkflowState.GAP_DIRECTED_DISCOVERY,
    WorkflowState.COMPARING_ASSEMBLY_STRATEGIES,
    WorkflowState.AWAITING_ASSEMBLY_STRATEGY_APPROVAL,
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
        semantics_v2_discovery_executor: SemanticsV2DiscoveryExecutor | None = None,
        maximum_workflows: int = 100,
    ):
        self.database = database
        self.artifact_store = artifact_store
        self.graph = graph
        self.repo_root = Path(repo_root).resolve()
        self.harness = harness
        self.agent_configuration = agent_configuration or AgentConfiguration()
        self.reviewed_source_adapters = reviewed_source_adapters or ReviewedSourceAdapterRegistry()
        self.semantics_v2_discovery_executor = semantics_v2_discovery_executor
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
                raise WorkflowConflict("The configured workflow limit has been reached.")
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
                            "Build creation defines workflow scope only; it does not create a "
                            "scientific model.",
                            "The prepared offline replay is not live dataset discovery.",
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
                        workflow_semantics_version=WORKFLOW_SEMANTICS_V2,
                        benchmark_mode=request.benchmark_mode,
                        initial_context_json=canonical_json(
                            versioned_payload(document=initial_context.model_dump(mode="json"))
                        ),
                        endpoint_discovery_scope_json=None,
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
                        discovery_plan_json=None,
                        discovery_execution_ledger_json=None,
                        source_candidates_json=None,
                        hydrated_sources_json=None,
                        combination_coverage_json=None,
                        strategy_proposals_json=None,
                        assembly_recipe_json=None,
                        provider_capability_findings_json=None,
                        strategy_set_rejection_json=None,
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
                limitations=["Creation is the explicit local-admin endpoint-definition decision."],
                artifact_hashes=[definition.sha256],
                agent_recommendation="No agent recommendation; administrator-authored definition.",
                requested_action=(
                    "Start target training-dataset specification."
                    if request.workflow_kind is WorkflowKind.TRAINING_DATASET_DISCOVERY
                    else "Start bounded prepared offline replay."
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

    def _latest_artifact_json(
        self, session: Session, workflow_id: str, artifact_type: str
    ) -> dict | None:
        row = session.scalar(
            select(ArtifactRow)
            .where(
                ArtifactRow.workflow_id == workflow_id,
                ArtifactRow.artifact_type == artifact_type,
            )
            .order_by(ArtifactRow.created_at.desc(), ArtifactRow.id.desc())
            .limit(1)
        )
        if row is None:
            return None
        _descriptor, content = self.artifact_store.get(row.id)
        value = json.loads(content)
        return value if isinstance(value, dict) else None

    def _artifact_jsons(self, session: Session, workflow_id: str, artifact_type: str) -> list[dict]:
        rows = session.scalars(
            select(ArtifactRow)
            .where(
                ArtifactRow.workflow_id == workflow_id,
                ArtifactRow.artifact_type == artifact_type,
            )
            .order_by(ArtifactRow.created_at, ArtifactRow.id)
        ).all()
        values = []
        for row in rows:
            _descriptor, content = self.artifact_store.get(row.id)
            value = json.loads(content)
            if isinstance(value, dict):
                values.append(value)
        return values

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
            source_items = (
                _load_training_document(row.source_observations_json).get("items", [])
                if row.source_observations_json
                else []
            )
            return {
                "schema_version": SCHEMA_VERSION,
                "contract_version": row.contract_version,
                "workflow_semantics_version": row.workflow_semantics_version,
                "workflow_id": workflow_id,
                "workflow_kind": build.workflow_kind,
                "benchmark_mode": row.benchmark_mode,
                "legacy": row.workflow_semantics_version != WORKFLOW_SEMANTICS_V2,
                "legacy_semantics_read_only": (
                    row.workflow_semantics_version != WORKFLOW_SEMANTICS_V2
                ),
                "initial_context": _load_training_document(row.initial_context_json),
                "endpoint_discovery_scope": (
                    _load_training_document(row.endpoint_discovery_scope_json)
                    if row.endpoint_discovery_scope_json
                    else None
                ),
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
                "source_observations": [
                    item for item in source_items if isinstance(item.get("observation"), dict)
                ],
                "source_search_outcomes": [
                    item for item in source_items if isinstance(item.get("search_outcome"), dict)
                ],
                "source_discovery_execution": self._source_discovery_execution_summary(
                    session, workflow_id
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
                "discovery_plan": (
                    _load_training_document(row.discovery_plan_json)
                    if row.discovery_plan_json
                    else None
                ),
                "discovery_execution_ledger": (
                    _load_training_document(row.discovery_execution_ledger_json)
                    if row.discovery_execution_ledger_json
                    else None
                ),
                "source_candidates": (
                    _load_training_document(row.source_candidates_json)
                    if row.source_candidates_json
                    else None
                ),
                "hydrated_sources": (
                    _load_training_document(row.hydrated_sources_json)
                    if row.hydrated_sources_json
                    else None
                ),
                "combination_coverage": (
                    _load_training_document(row.combination_coverage_json)
                    if row.combination_coverage_json
                    else None
                ),
                "strategy_proposals": (
                    _load_training_document(row.strategy_proposals_json)
                    if row.strategy_proposals_json
                    else None
                ),
                "assembly_recipe": (
                    _load_training_document(row.assembly_recipe_json)
                    if row.assembly_recipe_json
                    else None
                ),
                "provider_capability_findings": (
                    _load_training_document(row.provider_capability_findings_json)
                    if row.provider_capability_findings_json
                    else None
                ),
                "strategy_set_rejection": (
                    _load_training_document(row.strategy_set_rejection_json)
                    if row.strategy_set_rejection_json
                    else None
                ),
                "dataset_bundle": self._latest_artifact_json(
                    session, workflow_id, "dataset_bundle"
                ),
                "dataset_quality_report": self._latest_artifact_json(
                    session, workflow_id, "dataset_quality_report"
                ),
                "dataset_review_decision": self._latest_artifact_json(
                    session, workflow_id, "dataset_review_decision"
                ),
                "explore_manifest": self._latest_artifact_json(
                    session, workflow_id, "explore_manifest"
                ),
                "benchmark_plan": self._latest_artifact_json(
                    session, workflow_id, "benchmark_plan"
                ),
                "model_benchmark_results": self._artifact_jsons(
                    session, workflow_id, "model_benchmark_result"
                ),
                "benchmark_comparison": self._latest_artifact_json(
                    session, workflow_id, "benchmark_comparison"
                ),
                "model_selection_record": self._latest_artifact_json(
                    session, workflow_id, "model_selection_record"
                ),
                "final_validation_report": self._latest_artifact_json(
                    session, workflow_id, "final_validation_report"
                ),
                "publication_receipt": self._latest_artifact_json(
                    session, workflow_id, "publish_receipt"
                ),
                "implementation_validation_status": (
                    self._latest_artifact_json(session, workflow_id, "validation_status")
                ),
                "planned_discovery_agents": self._planned_discovery_agents(),
                "source_discovery_readiness": self.source_discovery_readiness(workflow_id),
                "discovery_round": row.discovery_round,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }

    def _planned_discovery_agents(self) -> list[dict]:
        adapter_labels = {
            "Activity Evidence Discovery Agent": ["official activity-source metadata adapters"],
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

        def role_configuration(agent_name: str):
            return self.agent_configuration.controlled_source_discovery_for_agent(agent_name)

        return [
            {
                "agent_name": item.agent_name,
                "provider": role_configuration(item.agent_name).worker_provider,
                "model": role_configuration(item.agent_name).worker_model,
                "allowed_tools": self.reviewed_source_adapters.filter_available_operations(
                    item.allowed_tools
                ),
                "allowed_official_source_adapters": adapter_labels[item.agent_name],
                "maximum_turns": role_configuration(item.agent_name).maximum_turns,
                "maximum_tool_calls": role_configuration(item.agent_name).maximum_tool_calls,
                "maximum_input_tokens": role_configuration(item.agent_name).maximum_input_tokens,
                "maximum_output_tokens": role_configuration(item.agent_name).maximum_output_tokens,
                "maximum_cost_usd": role_configuration(item.agent_name).maximum_cost_usd,
                "timeout_seconds": role_configuration(item.agent_name).timeout_seconds,
                "provider_retries": role_configuration(item.agent_name).retry_count,
                "output_schema_name": item.output_schema_name,
            }
            for item in SPECIALIZED_AGENT_SEQUENCE
            if item.agent_name in names
        ]

    def _compact_reviewed_adapter_capabilities(
        self, allowed_tools: list[str]
    ) -> list[dict[str, object]]:
        """Return only stage-relevant public adapter metadata for model context."""

        allowed = set(allowed_tools)
        compact = []
        for adapter in self.reviewed_source_adapters.public_inventory():
            operations = [
                item for item in adapter.get("approved_operations", []) if item in allowed
            ]
            if not operations:
                continue
            compact.append(
                {
                    "adapter_id": adapter.get("adapter_id"),
                    "official_source_system": adapter.get("official_source_system"),
                    "supported_component_roles": adapter.get("supported_component_roles", []),
                    "approved_operations": operations,
                }
            )
        return compact

    def _historical_tool_diagnostic(
        self, run: AgentRunRow, call: ToolCallRow, result_payload: dict
    ) -> dict | None:
        error = result_payload.get("error") or {}
        existing = error.get("tool_diagnostic")
        if isinstance(existing, dict):
            return existing
        if error.get("code") != "tool_failure":
            return None
        arguments_payload = load_versioned_json(call.arguments_json)
        normalized = arguments_payload.get("normalized_arguments") or arguments_payload.get(
            "arguments", {}
        )
        original = arguments_payload.get("original_arguments") or arguments_payload.get(
            "arguments", {}
        )
        try:
            request = ReviewedSourceOperationInput.model_validate(normalized)
            self.reviewed_source_adapters._resolve(call.tool_name, request)
        except ReviewedSourceInvocationError as exc:
            schema = ReviewedSourceOperationInput.model_json_schema()
            return ToolInvocationFailureDiagnostic(
                tool_name=call.tool_name,
                tool_schema_version=call.tool_version,
                tool_schema_hash=hashlib.sha256(canonical_json(schema).encode()).hexdigest(),
                agent_role=run.agent_name,
                invocation_stage=exc.invocation_stage,
                supplied_argument_field_names=sorted(original),
                normalized_argument_field_names=sorted(normalized),
                validation_error_category=exc.validation_error_category,
                field_errors=exc.field_errors,
                dependency_status="historical_prerequisites_not_enforced",
                adapter_resolution_status=exc.adapter_resolution_status,
                source_transport_started=False,
                exception_class=type(exc).__name__,
                safe_message=exc.safe_message,
                retryable=False,
            ).model_dump(mode="json")
        except Exception:
            return None
        return None

    def _source_discovery_execution_summary(
        self, session: Session, workflow_id: str
    ) -> dict[str, object]:
        names = {
            "Activity Evidence Discovery Agent",
            "Transcriptomic Evidence Discovery Agent",
            "Chemical Identity and Structure Source Discovery Agent",
            "Supporting Metadata Discovery Agent",
        }
        runs = session.scalars(
            select(AgentRunRow)
            .where(
                AgentRunRow.workflow_id == workflow_id,
                AgentRunRow.agent_name.in_(names),
            )
            .order_by(AgentRunRow.created_at, AgentRunRow.id)
        ).all()
        stages = []
        execution_incomplete = False
        for run in runs:
            calls = session.scalars(
                select(ToolCallRow)
                .where(ToolCallRow.agent_run_id == run.id)
                .order_by(ToolCallRow.created_at, ToolCallRow.id)
            ).all()
            diagnostics = []
            source_requests = 0
            for call in calls:
                result_payload = load_versioned_json(call.result_json) if call.result_json else {}
                diagnostic = self._historical_tool_diagnostic(run, call, result_payload)
                if diagnostic:
                    diagnostics.append(diagnostic)
                source_requests += int(
                    ((result_payload.get("output") or {}).get("source_request_count", 0)) or 0
                )
            result = load_versioned_json(run.result_json) if run.result_json else {}
            error = result.get("error") or {}
            stage_status = (
                "agent_budget_stopped"
                if run.status == AgentRunStatus.BUDGET_EXCEEDED.value
                else ("tool_invocation_rejected_before_transport" if diagnostics else run.status)
            )
            execution_incomplete = execution_incomplete or stage_status in {
                "agent_budget_stopped",
                "tool_invocation_rejected_before_transport",
                "failed",
            }
            stages.append(
                {
                    "agent_name": run.agent_name,
                    "run_id": run.id,
                    "status": stage_status,
                    "provider_invocations": load_versioned_json(run.usage_json).get(
                        "provider_invocations", 0
                    ),
                    "tool_calls": len(calls),
                    "scientific_source_requests": source_requests,
                    "safe_error_code": error.get("code"),
                    "safe_message": error.get("safe_message"),
                    "tool_diagnostics": diagnostics,
                }
            )
        return {
            "status": (
                "discovery_execution_incomplete"
                if execution_incomplete
                else "discovery_execution_complete"
            ),
            "execution_complete": not execution_incomplete,
            "scope_revision_required": False,
            "safe_summary": (
                "Source discovery did not complete; the empty inventory is not evidence that "
                "no usable public sources exist."
                if execution_incomplete
                else "Source discovery completed under the bounded reviewed-source policy."
            ),
            "stages": stages,
        }

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
            "endpoint_discovery_scope": (
                EndpointDiscoveryScope,
                "endpoint_discovery_scope_json",
                "endpoint_discovery_scope",
            ),
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
            self._guard_historical_training_build_read_only(session, build)
            if build.workflow_kind != WorkflowKind.TRAINING_DATASET_DISCOVERY.value:
                raise WorkflowConflict("Legacy workflows cannot store training-dataset documents.")
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None:
                raise WorkflowNotFound("Training-dataset workflow state was not found.")
            document_revision = 1 + int(
                session.scalar(
                    select(func.count(ArtifactRow.id)).where(
                        ArtifactRow.workflow_id == workflow_id,
                        ArtifactRow.artifact_type == artifact_type,
                    )
                )
                or 0
            )
            artifact = self.artifact_store._put_bytes(
                session,
                workflow_id=workflow_id,
                content=canonical_json(payload).encode(),
                mime_type="application/json",
                artifact_type=artifact_type,
                logical_name=f"{artifact_type}-{document_name}-v{document_revision}.json",
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

    @staticmethod
    def _require_semantics_v2(row: TrainingDatasetWorkflowRow | None) -> TrainingDatasetWorkflowRow:
        if row is None:
            raise WorkflowNotFound("Training-dataset workflow state was not found.")
        if row.workflow_semantics_version != WORKFLOW_SEMANTICS_V2:
            raise WorkflowConflict(
                "Historical workflow-semantics v1 builds are read-only and cannot be "
                "upgraded in place."
            )
        return row

    @staticmethod
    def _guard_historical_training_build_read_only(
        session: Session, build: EndpointBuildRow
    ) -> None:
        if build.workflow_kind != WorkflowKind.TRAINING_DATASET_DISCOVERY.value:
            return
        row = session.get(TrainingDatasetWorkflowRow, build.id)
        if row is not None and row.workflow_semantics_version != WORKFLOW_SEMANTICS_V2:
            raise WorkflowConflict(
                "Historical workflow-semantics v1 builds are read-only; their persisted "
                "states and artifacts cannot be mutated or upgraded in place."
            )

    def _put_semantics_v2_document(
        self,
        session: Session,
        *,
        workflow_id: str,
        document: object,
        artifact_type: str,
        logical_name: str,
        producer: str,
        idempotency_key: str,
    ) -> ArtifactRow:
        if not hasattr(document, "model_dump"):
            raise WorkflowConflict("A typed semantics-v2 document is required.")
        payload = document.model_dump(mode="json")
        return self.artifact_store._put_bytes(
            session,
            workflow_id=workflow_id,
            content=canonical_json(payload).encode(),
            mime_type="application/json",
            artifact_type=artifact_type,
            logical_name=logical_name,
            producer=producer,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def _artifact_reference(descriptor: ArtifactDescriptor) -> ArtifactReference:
        return ArtifactReference(
            artifact_id=descriptor.id,
            sha256=descriptor.sha256,
            artifact_type=descriptor.artifact_type,
        )

    def _put_lifecycle_document(
        self,
        session: Session,
        *,
        workflow_id: str,
        document: object,
        artifact_type: str,
        logical_name: str,
        producer: str,
        idempotency_key: str,
    ) -> ArtifactReference:
        descriptor = self._put_semantics_v2_document(
            session,
            workflow_id=workflow_id,
            document=document,
            artifact_type=artifact_type,
            logical_name=logical_name,
            producer=producer,
            idempotency_key=idempotency_key,
        )
        return self._artifact_reference(descriptor)

    def _put_lifecycle_bytes(
        self,
        session: Session,
        *,
        workflow_id: str,
        content: bytes,
        mime_type: str,
        artifact_type: str,
        logical_name: str,
        producer: str,
        idempotency_key: str,
    ) -> ArtifactReference:
        descriptor = self.artifact_store._put_bytes(
            session,
            workflow_id=workflow_id,
            content=content,
            mime_type=mime_type,
            artifact_type=artifact_type,
            logical_name=logical_name,
            producer=producer,
            idempotency_key=idempotency_key,
        )
        return self._artifact_reference(descriptor)

    def _put_lifecycle_json(
        self,
        session: Session,
        *,
        workflow_id: str,
        value: dict,
        artifact_type: str,
        logical_name: str,
        producer: str,
        idempotency_key: str,
    ) -> ArtifactReference:
        return self._put_lifecycle_bytes(
            session,
            workflow_id=workflow_id,
            content=canonical_json(value).encode(),
            mime_type="application/json",
            artifact_type=artifact_type,
            logical_name=logical_name,
            producer=producer,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def _semantics_v2_budgets(configuration: AgentConfiguration) -> DiscoveryBudgets:
        return DiscoveryBudgets(
            maximum_provider_invocations=configuration.global_maximum_provider_invocations,
            maximum_tool_calls=configuration.global_maximum_tool_calls,
            maximum_scientific_source_requests=configuration.global_maximum_source_requests,
            maximum_input_tokens=configuration.global_maximum_input_tokens,
            maximum_output_tokens=configuration.global_maximum_output_tokens,
            maximum_estimated_cost_usd=configuration.global_maximum_cost_usd,
            timeout_seconds=configuration.global_timeout_seconds,
            provider_retries=0,
        )

    def plan_semantics_v2_discovery(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Create the deterministic plan, complete ledger, and capability findings."""

        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = self._require_semantics_v2(session.get(TrainingDatasetWorkflowRow, workflow_id))
            if build.current_stage == WorkflowState.DISCOVERY_PLANNING.value:
                return self._snapshot(session, build)
            if build.current_stage != WorkflowState.APPROVED_SPECIFICATION.value:
                raise InvalidTransition(
                    "Semantics-v2 discovery planning requires APPROVED_SPECIFICATION."
                )
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            if not row.specification_json:
                raise GuardNotSatisfied("Approved training-dataset specification is required.")

        if not self.training_dataset_workflow(workflow_id).get("component_requirements"):
            self.derive_training_dataset_requirements(
                workflow_id,
                actor="deterministic-orchestrator",
                idempotency_key=f"{idempotency_key}:requirements",
            )

        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = self._require_semantics_v2(session.get(TrainingDatasetWorkflowRow, workflow_id))
            specification = TrainingDatasetSpecification.model_validate(
                _load_training_document(row.specification_json)
            )
            specification_artifact = session.scalar(
                select(ArtifactRow)
                .where(
                    ArtifactRow.workflow_id == workflow_id,
                    ArtifactRow.artifact_type == "training_dataset_specification",
                )
                .order_by(ArtifactRow.created_at.desc(), ArtifactRow.id.desc())
                .limit(1)
            )
            if specification_artifact is None:
                raise GuardNotSatisfied("Approved specification artifact is missing.")
            modalities = [item.value for item in specification.candidate_modalities]
            if not modalities and specification.endpoint_modality:
                modalities = [specification.endpoint_modality]
            if not modalities:
                raise GuardNotSatisfied(
                    "Approved specification does not define discovery modalities."
                )
            registry = load_operational_capability_registry(self.repo_root)
            plan = build_discovery_plan(
                workflow_id=workflow_id,
                endpoint_identifier=specification.specification_id,
                approved_specification_artifact=ArtifactReference(
                    artifact_id=specification_artifact.id,
                    sha256=specification_artifact.sha256,
                    artifact_type=specification_artifact.artifact_type,
                ),
                discovery_round=row.discovery_round,
                biological_target=specification.biological_target,
                target_synonyms=[specification.endpoint_name],
                requested_modalities=modalities,
                registry=registry,
                budgets=self._semantics_v2_budgets(self.agent_configuration),
            )
            ledger, findings = initial_execution_ledger(plan, registry)
            plan_artifact = self._put_semantics_v2_document(
                session,
                workflow_id=workflow_id,
                document=plan,
                artifact_type="discovery_plan",
                logical_name=f"discovery-plan-round-{row.discovery_round}.json",
                producer="deterministic-discovery-planner",
                idempotency_key=f"{idempotency_key}:plan",
            )
            ledger_artifact = self._put_semantics_v2_document(
                session,
                workflow_id=workflow_id,
                document=ledger,
                artifact_type="discovery_execution_ledger",
                logical_name=f"discovery-execution-ledger-round-{row.discovery_round}.json",
                producer="deterministic-discovery-planner",
                idempotency_key=f"{idempotency_key}:ledger",
            )
            findings_artifact = self._put_semantics_v2_document(
                session,
                workflow_id=workflow_id,
                document=findings,
                artifact_type="registered_provider_capability_missing",
                logical_name=f"provider-capability-findings-round-{row.discovery_round}.json",
                producer="deterministic-capability-validator",
                idempotency_key=f"{idempotency_key}:findings",
            )
            row.discovery_plan_json = canonical_json(
                versioned_payload(document=plan.model_dump(mode="json"))
            )
            row.discovery_execution_ledger_json = canonical_json(
                versioned_payload(document=ledger.model_dump(mode="json"))
            )
            row.provider_capability_findings_json = canonical_json(
                versioned_payload(document=findings.model_dump(mode="json"))
            )
            row.updated_at = utc_text()
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.DISCOVERY_PLANNING,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:transition",
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id=actor,
                    reason="Immutable semantics-v2 discovery plan and ledger compiled.",
                    artifact_hashes=[
                        specification_artifact.sha256,
                        plan_artifact.sha256,
                        ledger_artifact.sha256,
                        findings_artifact.sha256,
                    ],
                ),
            )

    def request_dataset_assembly_revision(
        self,
        workflow_id: str,
        *,
        decision: str,
        rationale: str,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Reject the current bundle while preserving every immutable prior artifact."""

        if decision not in {"rejected", "revision_requested"}:
            raise GuardNotSatisfied("Unsupported dataset review decision.")
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = self._require_semantics_v2(session.get(TrainingDatasetWorkflowRow, workflow_id))
            if build.current_stage != WorkflowState.AWAITING_DATASET_APPROVAL.value:
                raise InvalidTransition("Dataset revision requires AWAITING_DATASET_APPROVAL.")
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            bundle_value = self._latest_artifact_json(session, workflow_id, "dataset_bundle")
            quality_value = self._latest_artifact_json(
                session, workflow_id, "dataset_quality_report"
            )
            descriptors = self.artifact_store.list_artifacts(workflow_id)
            bundle_descriptor = next(
                item for item in reversed(descriptors) if item.artifact_type == "dataset_bundle"
            )
            quality_descriptor = next(
                item
                for item in reversed(descriptors)
                if item.artifact_type == "dataset_quality_report"
            )
            if bundle_value is None or quality_value is None:
                raise GuardNotSatisfied("Dataset bundle and quality report are required.")
            review = DatasetReviewDecision(
                workflow_id=workflow_id,
                decision=decision,
                reviewer_id=actor,
                dataset_bundle_sha256=bundle_descriptor.sha256,
                quality_report_sha256=quality_descriptor.sha256,
                rationale=rationale,
            )
            review_ref = self._put_lifecycle_document(
                session,
                workflow_id=workflow_id,
                document=review,
                artifact_type="dataset_review_decision",
                logical_name=f"dataset-{decision}-decision.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:decision",
            )
            row.assembly_recipe_json = None
            row.updated_at = utc_text()
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.AWAITING_ASSEMBLY_STRATEGY_REVIEW,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:strategy-review",
                    initiator=ActorType.HUMAN,
                    initiator_id=actor,
                    reason="Developer rejected the current dataset and requested a new recipe.",
                    artifact_hashes=[review_ref.sha256],
                ),
            )

    def authorize_semantics_v2_discovery(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Authorize only the immutable plan; never run a source or provider call here."""

        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = self._require_semantics_v2(session.get(TrainingDatasetWorkflowRow, workflow_id))
            existing = session.scalar(
                select(WorkflowEventRow).where(
                    WorkflowEventRow.workflow_id == workflow_id,
                    WorkflowEventRow.idempotency_key.in_(
                        {
                            f"{idempotency_key}:authorized",
                            f"{idempotency_key}:blocked",
                        }
                    ),
                )
            )
            if existing is not None:
                return self._snapshot(session, build)
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            if build.current_stage != WorkflowState.DISCOVERY_PLANNING.value:
                raise InvalidTransition("Discovery authorization requires DISCOVERY_PLANNING.")
            if not row.discovery_plan_json or not row.discovery_execution_ledger_json:
                raise GuardNotSatisfied("Discovery plan and ledger are required.")
            plan = DiscoveryPlan.model_validate(_load_training_document(row.discovery_plan_json))
            ledger = DiscoveryExecutionLedger.model_validate(
                _load_training_document(row.discovery_execution_ledger_json)
            )
            validate_execution_ledger(plan, ledger)
            blocked_roles = []
            for role in {item.evidence_role for item in ledger.records}:
                role_records = [item for item in ledger.records if item.evidence_role is role]
                if all(
                    item.status.value == "blocked_provider_capability_missing"
                    for item in role_records
                ):
                    blocked_roles.append(role.value)
            artifact_rows = session.scalars(
                select(ArtifactRow).where(
                    ArtifactRow.workflow_id == workflow_id,
                    ArtifactRow.artifact_type.in_({"discovery_plan", "discovery_execution_ledger"}),
                )
            ).all()
            hashes = sorted({item.sha256 for item in artifact_rows})
            if blocked_roles:
                finding = session.scalar(
                    select(ArtifactRow)
                    .where(
                        ArtifactRow.workflow_id == workflow_id,
                        ArtifactRow.artifact_type == "registered_provider_capability_missing",
                    )
                    .order_by(ArtifactRow.created_at.desc())
                    .limit(1)
                )
                if finding is not None:
                    hashes.append(finding.sha256)
                return self._apply_transition(
                    session,
                    build,
                    TransitionRequest(
                        target_state=WorkflowState.REGISTERED_PROVIDER_CAPABILITY_BLOCKED,
                        expected_version=build.version,
                        idempotency_key=f"{idempotency_key}:blocked",
                        initiator=ActorType.ORCHESTRATOR,
                        initiator_id="provider-capability-validator",
                        reason="Required provider roles are blocked: " + ", ".join(blocked_roles),
                        artifact_hashes=hashes,
                    ),
                )
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.DISCOVERING_SOURCE_CANDIDATES,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:authorized",
                    initiator=ActorType.HUMAN,
                    initiator_id=actor,
                    reason="The complete immutable discovery plan was explicitly authorized.",
                    artifact_hashes=hashes,
                ),
            )

    def record_semantics_v2_discovery_review(
        self,
        workflow_id: str,
        *,
        ledger: DiscoveryExecutionLedger,
        candidates: SourceCandidateSet,
        hydrated_sources: HydratedSourceSet,
        combination_coverage: CombinationCoverageSet,
        strategy_proposals: StrategyProposalSet,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Persist offline/future-orchestrator checkpoints through the v2 review gate."""

        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = self._require_semantics_v2(session.get(TrainingDatasetWorkflowRow, workflow_id))
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            if build.current_stage != WorkflowState.DISCOVERING_SOURCE_CANDIDATES.value:
                raise InvalidTransition(
                    "Discovery review checkpoint requires DISCOVERING_SOURCE_CANDIDATES."
                )
            if not row.discovery_plan_json:
                raise GuardNotSatisfied("Discovery plan is missing.")
            plan = DiscoveryPlan.model_validate(_load_training_document(row.discovery_plan_json))
            validate_execution_ledger(plan, ledger)
            if not ledger.complete:
                raise GuardNotSatisfied("Every planned discovery task must be terminal.")
            validate_candidate_universe(ledger, candidates)
            validate_hydrated_sources(candidates, hydrated_sources)
            validate_coverage_universe(hydrated_sources, combination_coverage)
            validate_strategy_universe(combination_coverage, strategy_proposals)
            if any(
                document.workflow_id != workflow_id
                or document.discovery_round != row.discovery_round
                for document in (
                    ledger,
                    candidates,
                    hydrated_sources,
                    combination_coverage,
                    strategy_proposals,
                )
            ):
                raise GuardNotSatisfied(
                    "All review artifacts must bind to the active workflow round."
                )

            ledger_artifact = self._put_semantics_v2_document(
                session,
                workflow_id=workflow_id,
                document=ledger,
                artifact_type="discovery_execution_ledger",
                logical_name=f"discovery-execution-ledger-round-{row.discovery_round}-completed.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:ledger",
            )
            candidate_artifact = self._put_semantics_v2_document(
                session,
                workflow_id=workflow_id,
                document=candidates,
                artifact_type="source_candidates",
                logical_name=f"source-candidates-round-{row.discovery_round}.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:candidates",
            )
            row.discovery_execution_ledger_json = canonical_json(
                versioned_payload(document=ledger.model_dump(mode="json"))
            )
            row.source_candidates_json = canonical_json(
                versioned_payload(document=candidates.model_dump(mode="json"))
            )
            self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.HYDRATING_SOURCE_CANDIDATES,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:to-hydration",
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id=actor,
                    reason=(
                        "All planned candidate-search tasks are terminal; every candidate "
                        "is retained."
                    ),
                    artifact_hashes=[ledger_artifact.sha256, candidate_artifact.sha256],
                ),
            )
            hydrated_artifact = self._put_semantics_v2_document(
                session,
                workflow_id=workflow_id,
                document=hydrated_sources,
                artifact_type="hydrated_sources",
                logical_name=f"hydrated-sources-round-{row.discovery_round}.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:hydrated",
            )
            row.hydrated_sources_json = canonical_json(
                versioned_payload(document=hydrated_sources.model_dump(mode="json"))
            )
            self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.COMPUTING_COMBINATION_COVERAGE,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:to-coverage",
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id=actor,
                    reason="Candidate hydration and explicit missing fields are persisted.",
                    artifact_hashes=[hydrated_artifact.sha256],
                ),
            )
            coverage_artifact = self._put_semantics_v2_document(
                session,
                workflow_id=workflow_id,
                document=combination_coverage,
                artifact_type="combination_coverage",
                logical_name=f"combination-coverage-round-{row.discovery_round}.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:coverage",
            )
            row.combination_coverage_json = canonical_json(
                versioned_payload(document=combination_coverage.model_dump(mode="json"))
            )
            self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.GENERATING_ASSEMBLY_STRATEGIES,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:to-strategies",
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id=actor,
                    reason=(
                        "Deterministic coverage over all supplied hydrated combinations "
                        "is persisted."
                    ),
                    artifact_hashes=[coverage_artifact.sha256],
                ),
            )
            proposals_artifact = self._put_semantics_v2_document(
                session,
                workflow_id=workflow_id,
                document=strategy_proposals,
                artifact_type="strategy_proposals",
                logical_name=f"strategy-proposals-round-{row.discovery_round}.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:proposals",
            )
            row.strategy_proposals_json = canonical_json(
                versioned_payload(document=strategy_proposals.model_dump(mode="json"))
            )
            row.updated_at = utc_text()
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.AWAITING_ASSEMBLY_STRATEGY_REVIEW,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:to-review",
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id=actor,
                    reason=(
                        "Alternative coverage-bound strategies are ready for explicit "
                        "human review."
                    ),
                    artifact_hashes=[proposals_artifact.sha256, coverage_artifact.sha256],
                ),
            )

    def approve_semantics_v2_strategy(
        self,
        workflow_id: str,
        *,
        strategy_proposal_id: str,
        approved_modality_aggregation: dict,
        approved_context_filters: dict,
        approved_dose_time_rules: dict,
        approved_label_policy: dict,
        exclusion_rules: list[str],
        required_extraction_fields: list[str],
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Approve exactly one proposal and create one immutable AssemblyRecipe."""

        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = self._require_semantics_v2(session.get(TrainingDatasetWorkflowRow, workflow_id))
            existing = session.scalar(
                select(WorkflowEventRow).where(
                    WorkflowEventRow.workflow_id == workflow_id,
                    WorkflowEventRow.idempotency_key == f"{idempotency_key}:decision",
                )
            )
            if existing is not None:
                return self._snapshot(session, build)
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            if build.current_stage != WorkflowState.AWAITING_ASSEMBLY_STRATEGY_REVIEW.value:
                raise InvalidTransition("Strategy approval requires the v2 strategy review gate.")
            if row.assembly_recipe_json:
                raise WorkflowConflict("An immutable assembly recipe already exists.")
            if not row.strategy_proposals_json or not row.hydrated_sources_json:
                raise GuardNotSatisfied("Strategy proposals and hydrated sources are required.")
            proposals = StrategyProposalSet.model_validate(
                _load_training_document(row.strategy_proposals_json)
            )
            selected = next(
                (item for item in proposals.proposals if item.proposal_id == strategy_proposal_id),
                None,
            )
            if selected is None:
                raise GuardNotSatisfied("Selected strategy proposal does not exist.")
            if selected.proposal_status.value not in {"proposed", "viable"}:
                raise GuardNotSatisfied("Blocked or rejected strategies cannot be approved.")
            if approved_label_policy.get("operator") != selected.proposed_label_policy.get(
                "operator"
            ):
                raise GuardNotSatisfied(
                    "Label-policy changes require a new versioned strategy proposal."
                )
            aggregation_operator = approved_modality_aggregation.get("operator")
            if aggregation_operator not in {"none", "functional_or"}:
                raise GuardNotSatisfied("Unsupported modality aggregation policy.")
            if aggregation_operator == "functional_or" and len(selected.modalities) < 2:
                raise GuardNotSatisfied(
                    "Functional union requires a multi-modality strategy proposal."
                )
            if not set(exclusion_rules).issubset(set(selected.exclusions)):
                raise GuardNotSatisfied(
                    "Exclusion changes require a new versioned strategy proposal."
                )
            hydrated = HydratedSourceSet.model_validate(
                _load_training_document(row.hydrated_sources_json)
            )
            by_id = {item.hydrated_source_id: item for item in hydrated.sources}
            unknown = sorted(set(selected.included_source_combination) - set(by_id))
            if unknown:
                raise GuardNotSatisfied(
                    "Strategy references unknown hydrated sources.", detail={"unknown": unknown}
                )
            missing_versions = sorted(
                item_id
                for item_id in selected.included_source_combination
                if not by_id[item_id].source_version
            )
            if missing_versions:
                raise GuardNotSatisfied(
                    "Every approved source requires an exact version.",
                    detail={"missing_source_versions": missing_versions},
                )
            decision_event = append_event(
                session,
                build,
                event_type="assembly_strategy.approved",
                actor_type=ActorType.HUMAN.value,
                actor_id=actor,
                idempotency_key=f"{idempotency_key}:decision",
                payload={
                    "strategy_proposal_id": strategy_proposal_id,
                    "discovery_round": row.discovery_round,
                },
                from_state=build.current_stage,
                to_state=build.current_stage,
            )
            recipe = AssemblyRecipe.create(
                workflow_id=workflow_id,
                discovery_round=row.discovery_round,
                selected_strategy_proposal_id=strategy_proposal_id,
                exact_source_versions={
                    item_id: str(by_id[item_id].source_version)
                    for item_id in selected.included_source_combination
                },
                exact_source_combination=selected.included_source_combination,
                approved_modalities=selected.modalities,
                endpoint_semantics_version=WORKFLOW_SEMANTICS_V2,
                approved_activity_sources=[
                    item_id
                    for item_id in selected.included_source_combination
                    if by_id[item_id].evidence_role.value == "activity"
                ],
                approved_transcriptomic_sources=[
                    item_id
                    for item_id in selected.included_source_combination
                    if by_id[item_id].evidence_role.value == "transcriptomic"
                ],
                approved_modality_aggregation=approved_modality_aggregation,
                approved_identity_mapping_policy={"operator": "stable_identifier_exact"},
                approved_context_filters=approved_context_filters,
                approved_cell_tissue_contexts=[
                    str(value)
                    for value in approved_context_filters.get("cell", [])
                    if value is not None
                ]
                if isinstance(approved_context_filters.get("cell", []), list)
                else [str(approved_context_filters["cell"])]
                if approved_context_filters.get("cell") is not None
                else [],
                approved_dose_time_rules=approved_dose_time_rules,
                approved_label_policy=approved_label_policy,
                conflict_resolution_policy={"operator": "exclude_conflicts"},
                replicate_aggregation_policy={"operator": "preserve_profiles"},
                exclusion_rules=exclusion_rules,
                split_requirements={
                    "group_by": "compound_id",
                    "partitions": ["train", "validation", "test"],
                    "leakage_tolerance": 0,
                },
                required_artifact_references=[
                    reference
                    for item_id in selected.included_source_combination
                    for reference in by_id[item_id].evidence_artifacts
                ],
                required_extraction_fields=required_extraction_fields,
                approver_action=HumanApprovalRecord(
                    approval_id=deterministic_id(
                        "approval", workflow_id, strategy_proposal_id, idempotency_key
                    ),
                    reviewer_id=actor,
                    decision_event_id=decision_event.id,
                ),
            )
            recipe_artifact = self._put_semantics_v2_document(
                session,
                workflow_id=workflow_id,
                document=recipe,
                artifact_type="assembly_recipe",
                logical_name=f"assembly-recipe-{recipe.recipe_fingerprint}.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:recipe",
            )
            proposals_artifact = session.scalar(
                select(ArtifactRow)
                .where(
                    ArtifactRow.workflow_id == workflow_id,
                    ArtifactRow.artifact_type == "strategy_proposals",
                )
                .order_by(ArtifactRow.created_at.desc())
                .limit(1)
            )
            if proposals_artifact is None:
                raise GuardNotSatisfied("Strategy proposal artifact is missing.")
            row.assembly_recipe_json = canonical_json(
                versioned_payload(document=recipe.model_dump(mode="json"))
            )
            row.updated_at = utc_text()
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.ASSEMBLY_RECIPE_APPROVED,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:transition",
                    initiator=ActorType.HUMAN,
                    initiator_id=actor,
                    reason="Exactly one strategy was approved as an immutable assembly recipe.",
                    artifact_hashes=[proposals_artifact.sha256, recipe_artifact.sha256],
                ),
            )

    def generate_semantics_v2_strategies(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Run the provider-neutral strategy boundary over persisted coverage."""

        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = self._require_semantics_v2(session.get(TrainingDatasetWorkflowRow, workflow_id))
            if build.current_stage != WorkflowState.GENERATING_ASSEMBLY_STRATEGIES.value:
                raise InvalidTransition(
                    "Strategy generation requires GENERATING_ASSEMBLY_STRATEGIES."
                )
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            if not row.specification_json or not row.hydrated_sources_json:
                raise GuardNotSatisfied("Approved specification and hydrated sources are required.")
            if not row.combination_coverage_json:
                raise GuardNotSatisfied("Combination coverage is required.")
            request = StrategyAgentInput(
                workflow_id=workflow_id,
                approved_endpoint_specification=_load_training_document(row.specification_json),
                hydrated_sources=HydratedSourceSet.model_validate(
                    _load_training_document(row.hydrated_sources_json)
                ),
                coverage=CombinationCoverageSet.model_validate(
                    _load_training_document(row.combination_coverage_json)
                ),
                allowed_scientific_policies=[
                    "source_backed_activity_call",
                    "functional_or",
                    "modalities_kept_separate",
                ],
                quality_constraints=[
                    "stable compound identifiers required",
                    "compound-level split leakage prohibited",
                    "raw source outcomes preserved",
                ],
            )
            proposals = DeterministicAssemblyStrategyAgent().generate(request)
            validate_strategy_universe(
                request.coverage,
                proposals,
                allowed_scientific_policies=set(request.allowed_scientific_policies),
            )
            artifact = self._put_semantics_v2_document(
                session,
                workflow_id=workflow_id,
                document=proposals,
                artifact_type="strategy_proposals",
                logical_name=f"strategy-proposals-round-{row.discovery_round}.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:proposals",
            )
            row.strategy_proposals_json = canonical_json(
                versioned_payload(document=proposals.model_dump(mode="json"))
            )
            row.updated_at = utc_text()
            coverage_artifact = session.scalar(
                select(ArtifactRow)
                .where(
                    ArtifactRow.workflow_id == workflow_id,
                    ArtifactRow.artifact_type == "combination_coverage",
                )
                .order_by(ArtifactRow.created_at.desc(), ArtifactRow.id.desc())
            )
            if coverage_artifact is None:
                raise GuardNotSatisfied("Combination coverage artifact is missing.")
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.AWAITING_ASSEMBLY_STRATEGY_REVIEW,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:review",
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id=actor,
                    reason="Coverage-bound strategy alternatives are ready for human review.",
                    artifact_hashes=[artifact.sha256, coverage_artifact.sha256],
                ),
            )

    def calculate_semantics_v2_coverage(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Persist coverage for every hydrated activity/transcriptomic pair."""

        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = self._require_semantics_v2(session.get(TrainingDatasetWorkflowRow, workflow_id))
            if build.current_stage != WorkflowState.COMPUTING_COMBINATION_COVERAGE.value:
                raise InvalidTransition(
                    "Coverage calculation requires COMPUTING_COMBINATION_COVERAGE."
                )
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            if not row.hydrated_sources_json:
                raise GuardNotSatisfied("HydratedSourceSet is required for coverage.")
            hydrated = HydratedSourceSet.model_validate(
                _load_training_document(row.hydrated_sources_json)
            )
            coverage = calculate_combination_coverage(workflow_id, row.discovery_round, hydrated)
            validate_coverage_universe(hydrated, coverage)
            artifact = self._put_semantics_v2_document(
                session,
                workflow_id=workflow_id,
                document=coverage,
                artifact_type="combination_coverage",
                logical_name=f"combination-coverage-round-{row.discovery_round}.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:coverage",
            )
            row.combination_coverage_json = canonical_json(
                versioned_payload(document=coverage.model_dump(mode="json"))
            )
            row.updated_at = utc_text()
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.GENERATING_ASSEMBLY_STRATEGIES,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:strategies",
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id=actor,
                    reason=(
                        "All activity/transcriptomic source combinations were evaluated "
                        "from compact metadata and artifact-backed evidence."
                    ),
                    artifact_hashes=[artifact.sha256],
                ),
            )

    def extract_recipe_bound_expression_slice(
        self,
        workflow_id: str,
        *,
        signature_ids_artifact: ArtifactReference,
        extractor: PostApprovalExpressionExtractor,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> SelectiveExpressionExtractionResult:
        """Run the registered local LINCS slicer only for an immutable approved recipe."""

        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = self._require_semantics_v2(session.get(TrainingDatasetWorkflowRow, workflow_id))
            if build.current_stage != WorkflowState.ASSEMBLY_RECIPE_APPROVED.value:
                raise InvalidTransition(
                    "Selective expression extraction requires an approved immutable recipe."
                )
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            if not row.assembly_recipe_json:
                raise GuardNotSatisfied("AssemblyRecipe is missing.")
            recipe = AssemblyRecipe.model_validate(
                _load_training_document(row.assembly_recipe_json)
            )
            approved_artifact_ids = {
                item.artifact_id for item in recipe.required_artifact_references
            }
            if signature_ids_artifact.artifact_id not in approved_artifact_ids:
                raise GuardNotSatisfied(
                    "The signature-ID artifact is not approved by the immutable recipe."
                )
            request = SelectiveExpressionExtractionRequest(
                workflow_id=workflow_id,
                recipe_fingerprint=recipe.recipe_fingerprint,
                signature_ids_artifact=signature_ids_artifact,
                approved_context={
                    "context_filters": recipe.approved_context_filters,
                    "dose_time_rules": recipe.approved_dose_time_rules,
                },
            )

        result = extractor.extract(request)

        with self.database.session() as session:
            build = require_build(session, workflow_id)
            if (
                build.version != expected_version
                or build.current_stage != WorkflowState.ASSEMBLY_RECIPE_APPROVED.value
            ):
                raise StaleWorkflowVersion(
                    "Workflow changed while the approved expression slice was extracted.",
                    detail={"current_version": build.version},
                )
            append_event(
                session,
                build,
                event_type="endpoint_dataset.expression_slice_extracted",
                actor_type=ActorType.SYSTEM.value,
                actor_id=actor,
                idempotency_key=f"{idempotency_key}:event",
                payload={
                    "recipe_fingerprint": request.recipe_fingerprint,
                    "expression_matrix": result.expression_matrix.model_dump(mode="json"),
                    "selected_signature_ids": result.selected_signature_ids.model_dump(mode="json"),
                    "gene_schema": result.gene_schema.model_dump(mode="json"),
                    "extraction_manifest": result.extraction_manifest.model_dump(mode="json"),
                },
                from_state=build.current_stage,
                to_state=build.current_stage,
            )
        return result

    def run_approved_dataset_assembly(
        self,
        workflow_id: str,
        *,
        offline_input: OfflineAssemblyInput,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Execute only an immutable recipe and persist an artifact-backed dataset bundle."""

        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = self._require_semantics_v2(session.get(TrainingDatasetWorkflowRow, workflow_id))
            if build.current_stage != WorkflowState.ASSEMBLY_RECIPE_APPROVED.value:
                raise InvalidTransition("Dataset assembly requires an approved immutable recipe.")
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            if not row.assembly_recipe_json:
                raise GuardNotSatisfied("AssemblyRecipe is missing.")
            recipe = AssemblyRecipe.model_validate(
                _load_training_document(row.assembly_recipe_json)
            )
            self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.ASSEMBLING_APPROVED_DATASET,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:authorize",
                    initiator=ActorType.HUMAN,
                    initiator_id=actor,
                    reason="Human authorized recipe-bound deterministic assembly.",
                    artifact_hashes=[],
                ),
            )
            source_refs = [
                self._put_lifecycle_json(
                    session,
                    workflow_id=workflow_id,
                    value={"rows": rows},
                    artifact_type=artifact_type,
                    logical_name=f"{offline_input.fixture_id}-{artifact_type}.json",
                    producer="offline-dataset-builder",
                    idempotency_key=f"{idempotency_key}:{artifact_type}",
                )
                for rows, artifact_type in (
                    (offline_input.activity_rows, "approved_activity_rows"),
                    (offline_input.transcriptomic_rows, "approved_transcriptomic_rows"),
                    (offline_input.expression_rows, "approved_expression_slice"),
                )
            ]
            assembled = assemble_approved_dataset(
                activity_rows=offline_input.activity_rows,
                transcriptomic_rows=offline_input.transcriptomic_rows,
                expression_rows=offline_input.expression_rows,
                approved_activity_sources=set(recipe.approved_activity_sources),
                approved_transcriptomic_sources=set(recipe.approved_transcriptomic_sources),
                approved_modalities=set(recipe.approved_modalities),
                approved_label_policy=recipe.approved_label_policy,
                approved_context_filters=recipe.approved_context_filters,
                exclusion_rules=recipe.exclusion_rules,
            )

            def table_ref(frame: pd.DataFrame, name: str, artifact_type: str) -> ArtifactReference:
                return self._put_lifecycle_bytes(
                    session,
                    workflow_id=workflow_id,
                    content=frame.to_csv(index=False, lineterminator="\n").encode(),
                    mime_type="text/csv",
                    artifact_type=artifact_type,
                    logical_name=f"{offline_input.fixture_id}-{name}.csv",
                    producer="offline-dataset-builder",
                    idempotency_key=f"{idempotency_key}:{name}",
                )

            x_ref = table_ref(assembled.X, "X", "expression_matrix")
            y_ref = table_ref(assembled.y.to_frame("label"), "y", "derived_labels")
            sample_ref = table_ref(assembled.sample_metadata, "sample-metadata", "sample_metadata")
            compound_ref = table_ref(
                assembled.compound_metadata, "compound-metadata", "compound_metadata"
            )
            split_ref = table_ref(assembled.split_assignments, "compound-splits", "split_manifest")
            excluded_ref = table_ref(
                assembled.excluded_records, "excluded-records", "excluded_record_ledger"
            )
            raw_activity_ref = table_ref(
                assembled.raw_activity_records, "raw-activity", "raw_activity_records"
            )
            signatures_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={"signature_ids": assembled.selected_signature_ids},
                artifact_type="selected_signature_ids",
                logical_name=f"{offline_input.fixture_id}-selected-signatures.json",
                producer="offline-dataset-builder",
                idempotency_key=f"{idempotency_key}:signatures",
            )
            gene_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={"features": assembled.gene_schema, "input_type": "transcriptomics"},
                artifact_type="feature_schema",
                logical_name=f"{offline_input.fixture_id}-feature-schema.json",
                producer="offline-dataset-builder",
                idempotency_key=f"{idempotency_key}:gene-schema",
            )
            dataset_card_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "endpoint": build.endpoint_name,
                    "fixture": offline_input.fixture_id,
                    "recipe_fingerprint": recipe.recipe_fingerprint,
                    "research_use_only": True,
                    "measured_profiles": True,
                    "predicted_profiles": False,
                    "limitations": [
                        "Deterministic synthetic offline fixture; scientific validation pending."
                    ],
                },
                artifact_type="dataset_card",
                logical_name=f"{offline_input.fixture_id}-dataset-card.json",
                producer="offline-dataset-builder",
                idempotency_key=f"{idempotency_key}:dataset-card",
            )
            provenance_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "recipe": recipe.model_dump(mode="json"),
                    "source_artifacts": [
                        item.model_dump(mode="json")
                        for item in [*source_refs, *offline_input.source_artifacts]
                    ],
                    "network_requests": 0,
                    "provider_requests": 0,
                    "gctx_network_access": False,
                },
                artifact_type="dataset_provenance_manifest",
                logical_name=f"{offline_input.fixture_id}-provenance.json",
                producer="offline-dataset-builder",
                idempotency_key=f"{idempotency_key}:provenance",
            )
            bundle = DatasetBundleManifest(
                workflow_id=workflow_id,
                recipe_fingerprint=recipe.recipe_fingerprint,
                x_matrix=x_ref,
                y_labels=y_ref,
                sample_metadata=sample_ref,
                compound_metadata=compound_ref,
                selected_signature_ids=signatures_ref,
                gene_schema=gene_ref,
                split_assignments=split_ref,
                excluded_unresolved_ledger=excluded_ref,
                raw_activity_records=raw_activity_ref,
                dataset_card=dataset_card_ref,
                provenance_manifest=provenance_ref,
                unique_compounds=int(assembled.sample_metadata["compound_id"].nunique()),
                total_profiles=len(assembled.sample_metadata),
                builder_mode="deterministic_offline_fixture",
            )
            bundle_ref = self._put_lifecycle_document(
                session,
                workflow_id=workflow_id,
                document=bundle,
                artifact_type="dataset_bundle",
                logical_name=f"{offline_input.fixture_id}-dataset-bundle.json",
                producer="offline-dataset-builder",
                idempotency_key=f"{idempotency_key}:bundle",
            )
            quality = dataset_quality_summary(assembled)
            quality_report = DatasetQualityReport(
                workflow_id=workflow_id,
                dataset_bundle_artifact=bundle_ref,
                unique_compounds=quality["unique_compounds"],
                total_profiles=quality["total_profiles"],
                class_distribution=quality["class_distribution"],
                context_distributions={
                    "cell": quality["cell_distribution"],
                    "dose": quality["dose_distribution"],
                    "time": quality["time_distribution"],
                    "provider": quality["provider_distribution"],
                },
                missing_values=quality["missing_values"],
                identifier_resolution_losses=0,
                conflicts_and_exclusions=quality["excluded_records"],
                split_sizes=quality["split_sizes"],
                compound_split_leakage=quality["compound_split_leakage"],
                leakage_check_passed=quality["leakage_check_passed"],
                source_provenance=[*source_refs, *offline_input.source_artifacts],
                quality_warnings=quality["warnings"],
            )
            quality_ref = self._put_lifecycle_document(
                session,
                workflow_id=workflow_id,
                document=quality_report,
                artifact_type="dataset_quality_report",
                logical_name=f"{offline_input.fixture_id}-quality-report.json",
                producer="offline-dataset-builder",
                idempotency_key=f"{idempotency_key}:quality",
            )
            leakage_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "group_by": "compound_id",
                    "passed": quality["leakage_check_passed"],
                    "leaking_compounds": quality["compound_split_leakage"],
                },
                artifact_type="leakage_audit",
                logical_name=f"{offline_input.fixture_id}-leakage-audit.json",
                producer="offline-dataset-builder",
                idempotency_key=f"{idempotency_key}:leakage",
            )
            status_ref = self._put_lifecycle_document(
                session,
                workflow_id=workflow_id,
                document=ValidationStatusMatrix(
                    limitations=[
                        "Provider and scientific validation remain pending after offline "
                        "validation."
                    ]
                ),
                artifact_type="validation_status",
                logical_name="implementation-validation-status.json",
                producer="offline-dataset-builder",
                idempotency_key=f"{idempotency_key}:status",
            )
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.AWAITING_DATASET_APPROVAL,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:review",
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id="dataset-builder",
                    reason="Artifact-backed dataset and leakage report await human review.",
                    artifact_hashes=[
                        bundle_ref.sha256,
                        quality_ref.sha256,
                        leakage_ref.sha256,
                        status_ref.sha256,
                    ],
                ),
            )

    def _load_assembled_dataset(self, bundle: DatasetBundleManifest) -> AssembledDataset:
        def frame(reference: ArtifactReference) -> pd.DataFrame:
            _descriptor, content = self.artifact_store.get(reference.artifact_id)
            return pd.read_csv(io.BytesIO(content))

        _descriptor, signature_content = self.artifact_store.get(
            bundle.selected_signature_ids.artifact_id
        )
        _descriptor, gene_content = self.artifact_store.get(bundle.gene_schema.artifact_id)
        signatures = json.loads(signature_content)["signature_ids"]
        genes = json.loads(gene_content)["features"]
        labels = frame(bundle.y_labels)["label"].astype(int)
        return AssembledDataset(
            X=frame(bundle.x_matrix),
            y=labels,
            sample_metadata=frame(bundle.sample_metadata),
            compound_metadata=frame(bundle.compound_metadata),
            selected_signature_ids=list(signatures),
            gene_schema=list(genes),
            split_assignments=frame(bundle.split_assignments),
            excluded_records=frame(bundle.excluded_unresolved_ledger),
            raw_activity_records=frame(bundle.raw_activity_records),
        )

    def approve_dataset_for_benchmarking(
        self,
        workflow_id: str,
        *,
        rationale: str,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Persist dataset review and Explore artifacts before enabling benchmarking."""

        with self.database.session() as session:
            build = require_build(session, workflow_id)
            if build.current_stage != WorkflowState.AWAITING_DATASET_APPROVAL.value:
                raise InvalidTransition("Dataset review requires AWAITING_DATASET_APPROVAL.")
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            bundle_value = self._latest_artifact_json(session, workflow_id, "dataset_bundle")
            quality_value = self._latest_artifact_json(
                session, workflow_id, "dataset_quality_report"
            )
            if bundle_value is None or quality_value is None:
                raise GuardNotSatisfied("Dataset bundle and quality report are required.")
            bundle = DatasetBundleManifest.model_validate(bundle_value)
            quality = DatasetQualityReport.model_validate(quality_value)
            if not quality.leakage_check_passed:
                raise GuardNotSatisfied("A dataset with compound leakage cannot be approved.")
            quality_descriptor = [
                item
                for item in self.artifact_store.list_artifacts(workflow_id)
                if item.artifact_type == "dataset_quality_report"
            ][-1]
            decision = DatasetReviewDecision(
                workflow_id=workflow_id,
                decision="approved_for_benchmarking",
                reviewer_id=actor,
                dataset_bundle_sha256=quality.dataset_bundle_artifact.sha256,
                quality_report_sha256=quality_descriptor.sha256,
                rationale=rationale,
            )
            decision_ref = self._put_lifecycle_document(
                session,
                workflow_id=workflow_id,
                document=decision,
                artifact_type="dataset_review_decision",
                logical_name="dataset-review-decision.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:decision",
            )
            dataset = self._load_assembled_dataset(bundle)
            explore = build_explore_payload(dataset)
            compounds_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={"records": dataset.compound_metadata.to_dict(orient="records")},
                artifact_type="explore_compounds",
                logical_name="explore-compounds.json",
                producer="explore-publisher",
                idempotency_key=f"{idempotency_key}:compounds",
            )
            points = explore["points"]
            map_points = explore["map_points"]
            signatures_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={"records": points},
                artifact_type="explore_reference_signatures",
                logical_name="explore-reference-signatures.json",
                producer="explore-publisher",
                idempotency_key=f"{idempotency_key}:signatures",
            )
            context_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "records": dataset.sample_metadata[
                        ["signature_id", "cell", "dose", "time", "provider", "provenance_id"]
                    ].to_dict(orient="records")
                },
                artifact_type="explore_context",
                logical_name="explore-context.json",
                producer="explore-publisher",
                idempotency_key=f"{idempotency_key}:context",
            )
            labels_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "records": dataset.sample_metadata[
                        ["signature_id", "compound_id", "derived_label", "provenance_id"]
                    ].to_dict(orient="records")
                },
                artifact_type="explore_source_labels",
                logical_name="explore-source-labels.json",
                producer="explore-publisher",
                idempotency_key=f"{idempotency_key}:labels",
            )
            neighbor_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "records": [
                        {"signature_id": item["signature_id"], "neighbors": item["neighbors"]}
                        for item in points
                    ]
                },
                artifact_type="explore_nearest_neighbor_index",
                logical_name="explore-nearest-neighbors.json",
                producer="explore-publisher",
                idempotency_key=f"{idempotency_key}:neighbors",
            )
            projection_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "method": explore["projection_method"],
                    "records": [
                        {
                            "compound_id": item["compound_id"],
                            "x": item["x"],
                            "y": item["y"],
                        }
                        for item in map_points
                    ],
                },
                artifact_type="explore_projection",
                logical_name="explore-projection.json",
                producer="explore-publisher",
                idempotency_key=f"{idempotency_key}:projection",
            )
            reference_support = explore["reference_support"]
            if not isinstance(reference_support, pd.DataFrame):
                raise WorkflowConflict("Explore support must be an artifact-backed table.")
            support_buffer = io.BytesIO()
            np.save(support_buffer, reference_support.to_numpy(dtype=float), allow_pickle=False)
            support_ref = self._put_lifecycle_bytes(
                session,
                workflow_id=workflow_id,
                content=support_buffer.getvalue(),
                mime_type="application/octet-stream",
                artifact_type="explore_support_matrix",
                logical_name="explore-support.npy",
                producer="explore-publisher",
                idempotency_key=f"{idempotency_key}:support",
            )
            label_counts = {
                "active": sum(item["label"] == "active" for item in map_points),
                "inactive": sum(item["label"] == "inactive" for item in map_points),
            }
            user_map_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "points": map_points,
                    "counts": {
                        "n_total": len(map_points),
                        "n_active": label_counts["active"],
                        "n_inactive": label_counts["inactive"],
                        "n_unlabeled": 0,
                    },
                },
                artifact_type="explore_user_facing_map",
                logical_name="explore-umap.json",
                producer="explore-publisher",
                idempotency_key=f"{idempotency_key}:user-map",
            )
            user_manifest_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "target": build.endpoint_name,
                    "n_compounds": len(map_points),
                    "compound_ids": list(reference_support.index.astype(str)),
                    "feature_names": list(reference_support.columns.astype(str)),
                    "umap": {
                        "algorithm": explore["projection_method"],
                        "random_state": 17,
                    },
                    "domain_metric": explore["domain_metric"],
                    "labels": {"status": "source_backed_derived_labels"},
                    "source": {
                        "key": support_ref.artifact_id,
                        "sha256": support_ref.sha256,
                    },
                    "support_sha256": support_ref.sha256,
                    "point_definition": "one aggregated reference profile per compound",
                    "aggregation": {
                        "profiles": "arithmetic mean after recipe-bound selection",
                        "labels": "approved source-backed endpoint policy",
                    },
                    "recomputed_on_page_load": False,
                },
                artifact_type="explore_user_facing_manifest",
                logical_name="explore-user-manifest.json",
                producer="explore-publisher",
                idempotency_key=f"{idempotency_key}:user-manifest",
            )
            manifest = ExplorePublicationManifest(
                workflow_id=workflow_id,
                compounds=compounds_ref,
                reference_signatures=signatures_ref,
                experimental_context=context_ref,
                endpoint_source_labels=labels_ref,
                nearest_neighbor_index=neighbor_ref,
                projection_coordinates=projection_ref,
                user_facing_map=user_map_ref,
                support_matrix=support_ref,
                user_facing_manifest=user_manifest_ref,
                projection_method=explore["projection_method"],
                filters=explore["filters"],
            )
            manifest_ref = self._put_lifecycle_document(
                session,
                workflow_id=workflow_id,
                document=manifest,
                artifact_type="explore_manifest",
                logical_name="explore-manifest.json",
                producer="explore-publisher",
                idempotency_key=f"{idempotency_key}:manifest",
            )
            leakage = next(
                item
                for item in self.artifact_store.list_artifacts(workflow_id)
                if item.artifact_type == "leakage_audit"
            )
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.AWAITING_TRAINING_APPROVAL,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:training-gate",
                    initiator=ActorType.HUMAN,
                    initiator_id=actor,
                    reason="Developer approved the immutable dataset for baseline benchmarking.",
                    artifact_hashes=[decision_ref.sha256, manifest_ref.sha256, leakage.sha256],
                ),
            )

    def run_endpoint_benchmark(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Run three registered baselines after the explicit training gate."""

        with self.database.session() as session:
            build = require_build(session, workflow_id)
            if build.current_stage != WorkflowState.AWAITING_TRAINING_APPROVAL.value:
                raise InvalidTransition("Benchmarking requires AWAITING_TRAINING_APPROVAL.")
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            bundle_value = self._latest_artifact_json(session, workflow_id, "dataset_bundle")
            if bundle_value is None:
                raise GuardNotSatisfied("Approved dataset bundle is missing.")
            bundle = DatasetBundleManifest.model_validate(bundle_value)
            plan = BenchmarkPlan(
                workflow_id=workflow_id,
                candidate_models=[
                    "logistic_regression",
                    "random_forest",
                    "hist_gradient_boosting",
                ],
            )
            plan_ref = self._put_lifecycle_document(
                session,
                workflow_id=workflow_id,
                document=plan,
                artifact_type="training_config",
                logical_name="benchmark-plan.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:plan",
            )
            self._put_lifecycle_document(
                session,
                workflow_id=workflow_id,
                document=plan,
                artifact_type="benchmark_plan",
                logical_name="benchmark-plan-contract.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:plan-contract",
            )
            authorization_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "decision": "benchmark_authorized",
                    "reviewer_id": actor,
                    "dataset_bundle_sha256": next(
                        item.sha256
                        for item in self.artifact_store.list_artifacts(workflow_id)
                        if item.artifact_type == "dataset_bundle"
                    ),
                    "candidate_models": plan.candidate_models,
                },
                artifact_type="benchmark_authorization",
                logical_name="benchmark-authorization.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:authorization",
            )
            self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.TRAINING,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:training",
                    initiator=ActorType.HUMAN,
                    initiator_id=actor,
                    reason="Developer authorized bounded baseline benchmarking.",
                    artifact_hashes=[plan_ref.sha256, authorization_ref.sha256],
                ),
            )
            dataset = self._load_assembled_dataset(bundle)
            candidates = benchmark_models(dataset)
            result_refs: list[ArtifactReference] = []
            candidate_model_refs: list[ArtifactReference] = []
            candidate_card_refs: list[ArtifactReference] = []
            for candidate in candidates:
                candidate_id = f"candidate-{candidate.name}"
                model_ref = self._put_lifecycle_bytes(
                    session,
                    workflow_id=workflow_id,
                    content=pickle.dumps(candidate.model, protocol=4),
                    mime_type="application/octet-stream",
                    artifact_type="candidate_model",
                    logical_name=f"{candidate_id}.pkl",
                    producer="baseline-benchmark",
                    idempotency_key=f"{idempotency_key}:model:{candidate.name}",
                )
                candidate_model_refs.append(model_ref)
                predictions_ref = self._put_lifecycle_json(
                    session,
                    workflow_id=workflow_id,
                    value={
                        "partition": "validation",
                        "probabilities": candidate.validation_probabilities.tolist(),
                    },
                    artifact_type="validation_predictions",
                    logical_name=f"{candidate_id}-validation-predictions.json",
                    producer="baseline-benchmark",
                    idempotency_key=f"{idempotency_key}:predictions:{candidate.name}",
                )
                card_ref = self._put_lifecycle_json(
                    session,
                    workflow_id=workflow_id,
                    value={
                        "candidate_id": candidate_id,
                        "model_name": candidate.name,
                        "status": "draft",
                        "research_use_only": True,
                        "metrics": candidate.metrics,
                        "limitations": [
                            "Offline synthetic validation only; no toxicity conclusion is "
                            "supported."
                        ],
                    },
                    artifact_type="model_card_draft",
                    logical_name=f"{candidate_id}-model-card-draft.json",
                    producer="baseline-benchmark",
                    idempotency_key=f"{idempotency_key}:card:{candidate.name}",
                )
                candidate_card_refs.append(card_ref)
                result = ModelBenchmarkResult(
                    workflow_id=workflow_id,
                    candidate_id=candidate_id,
                    model_name=candidate.name,
                    model_artifact=model_ref,
                    metrics=candidate.metrics,
                    validation_predictions=predictions_ref,
                    model_card_draft=card_ref,
                    reproducibility_metadata={
                        "seed": 17,
                        "grouped_by": "compound_id",
                        "preprocessing_fit_scope": "training_only",
                        "environment": "scikit-learn workspace runtime",
                    },
                )
                result_refs.append(
                    self._put_lifecycle_document(
                        session,
                        workflow_id=workflow_id,
                        document=result,
                        artifact_type="model_benchmark_result",
                        logical_name=f"{candidate_id}-benchmark-result.json",
                        producer="baseline-benchmark",
                        idempotency_key=f"{idempotency_key}:result:{candidate.name}",
                    )
                )
            comparison = BenchmarkComparison(
                workflow_id=workflow_id,
                candidate_results=result_refs,
                metric_names=[
                    "roc_auc",
                    "pr_auc",
                    "balanced_accuracy",
                    "sensitivity",
                    "specificity",
                    "precision",
                    "f1",
                    "brier_score",
                    "calibration_summary",
                    "fold_stability",
                    "training_time_ms",
                    "inference_time_ms",
                    "model_size_bytes",
                    "explanation_available",
                ],
            )
            comparison_ref = self._put_lifecycle_document(
                session,
                workflow_id=workflow_id,
                document=comparison,
                artifact_type="benchmark_comparison",
                logical_name="benchmark-comparison.json",
                producer="baseline-benchmark",
                idempotency_key=f"{idempotency_key}:comparison",
            )
            models_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "candidate_models": [
                        item.model_dump(mode="json") for item in candidate_model_refs
                    ],
                    "automatic_winner_selected": False,
                },
                artifact_type="candidate_models",
                logical_name="candidate-models.json",
                producer="baseline-benchmark",
                idempotency_key=f"{idempotency_key}:models",
            )
            training_log_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "status": "completed",
                    "candidate_count": len(candidates),
                    "failed_folds": [],
                    "network_requests": 0,
                },
                artifact_type="training_log",
                logical_name="training-log.json",
                producer="baseline-benchmark",
                idempotency_key=f"{idempotency_key}:training-log",
            )
            self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.EVALUATING,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:evaluating",
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id="baseline-benchmark",
                    reason="All registered candidate models and validation predictions persisted.",
                    artifact_hashes=[models_ref.sha256, training_log_ref.sha256],
                ),
            )
            evaluation_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "comparison_artifact": comparison_ref.model_dump(mode="json"),
                    "automatic_winner_selected": False,
                    "human_review_required": True,
                },
                artifact_type="evaluation_report",
                logical_name="evaluation-report.json",
                producer="baseline-benchmark",
                idempotency_key=f"{idempotency_key}:evaluation",
            )
            model_card_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "status": "draft_candidates",
                    "candidate_card_artifacts": [
                        item.model_dump(mode="json") for item in candidate_card_refs
                    ],
                },
                artifact_type="model_card",
                logical_name="candidate-model-cards-index.json",
                producer="baseline-benchmark",
                idempotency_key=f"{idempotency_key}:model-card-index",
            )
            endpoint_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "endpoint_name": build.endpoint_name,
                    "endpoint_slug": build.endpoint_slug,
                    "publication_status": "candidate",
                },
                artifact_type="endpoint_candidate",
                logical_name="endpoint-candidate.json",
                producer="baseline-benchmark",
                idempotency_key=f"{idempotency_key}:endpoint",
            )
            dataset_card = next(
                item
                for item in self.artifact_store.list_artifacts(workflow_id)
                if item.artifact_type == "dataset_card"
            )
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.AWAITING_SCIENTIFIC_APPROVAL,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:scientific-review",
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id="baseline-benchmark",
                    reason="Multi-metric comparison awaits explicit human model selection.",
                    artifact_hashes=[
                        evaluation_ref.sha256,
                        comparison_ref.sha256,
                        model_card_ref.sha256,
                        dataset_card.sha256,
                        endpoint_ref.sha256,
                    ],
                ),
            )

    def request_new_model_benchmark(
        self,
        workflow_id: str,
        *,
        decision: str,
        rationale: str,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Record human rejection without deleting the prior benchmark evidence."""

        if decision not in {"reject_all_models", "request_new_benchmark"}:
            raise GuardNotSatisfied("Unsupported model review decision.")
        with self.database.session() as session:
            build = require_build(session, workflow_id)
            if build.current_stage != WorkflowState.AWAITING_SCIENTIFIC_APPROVAL.value:
                raise InvalidTransition("Model review requires AWAITING_SCIENTIFIC_APPROVAL.")
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            if self._latest_artifact_json(session, workflow_id, "final_validation_report"):
                raise GuardNotSatisfied("A validated model cannot be silently replaced.")
            comparison = next(
                (
                    item
                    for item in reversed(self.artifact_store.list_artifacts(workflow_id))
                    if item.artifact_type == "benchmark_comparison"
                ),
                None,
            )
            if comparison is None:
                raise GuardNotSatisfied("Benchmark comparison is required.")
            review = ModelReviewDecision(
                workflow_id=workflow_id,
                decision=decision,
                reviewer_id=actor,
                rationale=rationale,
                benchmark_comparison_sha256=comparison.sha256,
            )
            review_ref = self._put_lifecycle_document(
                session,
                workflow_id=workflow_id,
                document=review,
                artifact_type="model_review_decision",
                logical_name=f"model-{decision}-decision.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:decision",
            )
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.AWAITING_TRAINING_APPROVAL,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:benchmark-gate",
                    initiator=ActorType.HUMAN,
                    initiator_id=actor,
                    reason="Developer rejected the current candidate models.",
                    artifact_hashes=[review_ref.sha256],
                ),
            )

    def select_and_validate_endpoint_model(
        self,
        workflow_id: str,
        *,
        candidate_id: str,
        decision_threshold: float,
        rationale: str,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Persist human selection and evaluate only that model on held-out compounds."""

        with self.database.session() as session:
            build = require_build(session, workflow_id)
            if build.current_stage != WorkflowState.AWAITING_SCIENTIFIC_APPROVAL.value:
                raise InvalidTransition("Model selection requires the scientific review gate.")
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            results = [
                ModelBenchmarkResult.model_validate(item)
                for item in self._artifact_jsons(session, workflow_id, "model_benchmark_result")
            ]
            selected = next((item for item in results if item.candidate_id == candidate_id), None)
            if selected is None:
                raise GuardNotSatisfied("Selected model candidate does not exist.")
            selection = ModelSelectionRecord(
                workflow_id=workflow_id,
                candidate_id=candidate_id,
                reviewer_id=actor,
                decision_threshold=decision_threshold,
                rationale=rationale,
                approved_validation_plan={
                    "partition": "test",
                    "grouped_by": "compound_id",
                    "single_evaluation": True,
                },
            )
            selection_ref = self._put_lifecycle_document(
                session,
                workflow_id=workflow_id,
                document=selection,
                artifact_type="model_selection_record",
                logical_name="model-selection-record.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:selection",
            )
            bundle_value = self._latest_artifact_json(session, workflow_id, "dataset_bundle")
            if bundle_value is None:
                raise GuardNotSatisfied("Dataset bundle is missing.")
            bundle = DatasetBundleManifest.model_validate(bundle_value)
            dataset = self._load_assembled_dataset(bundle)
            _descriptor, model_bytes = self.artifact_store.get(selected.model_artifact.artifact_id)
            validation = validate_selected_model(
                dataset, pickle.loads(model_bytes), threshold=decision_threshold
            )
            final_model_ref = self._put_lifecycle_bytes(
                session,
                workflow_id=workflow_id,
                content=pickle.dumps(validation["model"], protocol=4),
                mime_type="application/octet-stream",
                artifact_type="final_model",
                logical_name="final-model.pkl",
                producer="held-out-validator",
                idempotency_key=f"{idempotency_key}:final-model",
            )
            threshold_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "threshold": decision_threshold,
                    "decision_label": "endpoint signal score",
                    "safe_or_toxic_verdict": False,
                },
                artifact_type="threshold_policy",
                logical_name="threshold-policy.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:threshold",
            )
            card_ref = self._put_lifecycle_bytes(
                session,
                workflow_id=workflow_id,
                content=(
                    f"# {build.endpoint_name}\n\n"
                    "Research-use endpoint model. It reports an endpoint signal score, not a "
                    "safe/toxic verdict or confirmed toxicity.\n\n"
                    "Offline synthetic validation completed; live and scientific validation "
                    "remain pending.\n"
                ).encode(),
                mime_type="text/markdown",
                artifact_type="final_model_card",
                logical_name="final-model-card.md",
                producer="held-out-validator",
                idempotency_key=f"{idempotency_key}:card",
            )
            report = FinalValidationReport(
                workflow_id=workflow_id,
                candidate_id=candidate_id,
                held_out_metrics=validation["metrics"],
                feature_schema=bundle.gene_schema,
                threshold_policy=threshold_ref,
                final_model=final_model_ref,
                final_model_card=card_ref,
                limitations=[
                    "Deterministic synthetic fixture only.",
                    "Live provider validation pending.",
                    "Scientific validation pending; no toxicity conclusion is supported.",
                ],
                intended_use="Internal research demonstration and endpoint-building validation.",
                validation_status=ValidationStatusMatrix(),
            )
            report_ref = self._put_lifecycle_document(
                session,
                workflow_id=workflow_id,
                document=report,
                artifact_type="final_validation_report",
                logical_name="final-validation-report.json",
                producer="held-out-validator",
                idempotency_key=f"{idempotency_key}:validation",
            )
            append_event(
                session,
                build,
                event_type="endpoint_model.selected_and_validated",
                actor_type=ActorType.HUMAN.value,
                actor_id=actor,
                idempotency_key=f"{idempotency_key}:event",
                payload={
                    "candidate_id": candidate_id,
                    "selection_sha256": selection_ref.sha256,
                    "validation_sha256": report_ref.sha256,
                },
                from_state=build.current_stage,
                to_state=build.current_stage,
            )
            return self._snapshot(session, build)

    def publish_validated_endpoint(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Publish only after explicit human model selection and held-out validation."""

        with self.database.session() as session:
            build = require_build(session, workflow_id)
            if build.current_stage != WorkflowState.AWAITING_SCIENTIFIC_APPROVAL.value:
                raise InvalidTransition("Publication requires the scientific approval gate.")
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            selection_value = self._latest_artifact_json(
                session, workflow_id, "model_selection_record"
            )
            validation_value = self._latest_artifact_json(
                session, workflow_id, "final_validation_report"
            )
            bundle_value = self._latest_artifact_json(session, workflow_id, "dataset_bundle")
            if selection_value is None or validation_value is None or bundle_value is None:
                raise GuardNotSatisfied(
                    "Model selection, held-out validation and dataset bundle are required."
                )
            selection = ModelSelectionRecord.model_validate(selection_value)
            validation = FinalValidationReport.model_validate(validation_value)
            bundle = DatasetBundleManifest.model_validate(bundle_value)
            approval_id = deterministic_id("publication-approval", workflow_id, idempotency_key)
            approval_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "approval_id": approval_id,
                    "reviewer_id": actor,
                    "candidate_id": selection.candidate_id,
                    "validation_report": validation.model_dump(mode="json"),
                    "decision": "publish_experimental_endpoint",
                },
                artifact_type="publication_authorization",
                logical_name="publication-authorization.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:authorization",
            )
            selection_descriptor = next(
                item
                for item in self.artifact_store.list_artifacts(workflow_id)
                if item.artifact_type == "model_selection_record"
            )
            validation_descriptor = next(
                item
                for item in self.artifact_store.list_artifacts(workflow_id)
                if item.artifact_type == "final_validation_report"
            )
            self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.REGISTERING,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:registering",
                    initiator=ActorType.HUMAN,
                    initiator_id=actor,
                    reason="Developer approved immutable experimental endpoint publication.",
                    artifact_hashes=[
                        selection_descriptor.sha256,
                        validation_descriptor.sha256,
                        approval_ref.sha256,
                    ],
                ),
            )
            endpoint_id = re.sub(r"[^A-Z0-9_]", "_", build.endpoint_slug.upper())
            if not endpoint_id or not endpoint_id[0].isalpha():
                endpoint_id = f"ENDPOINT_{endpoint_id}"
            endpoint_dir = self.repo_root / "models" / endpoint_id
            endpoint_dir.mkdir(parents=True, exist_ok=True)
            artifact_outputs = {
                "model.pkl": validation.final_model,
                "feature_schema.json": validation.feature_schema,
                "model_card.md": validation.final_model_card,
                "dataset_card.json": bundle.dataset_card,
            }
            for filename, reference in artifact_outputs.items():
                _descriptor, content = self.artifact_store.get(reference.artifact_id)
                (endpoint_dir / filename).write_bytes(content)
            explore_value = self._latest_artifact_json(session, workflow_id, "explore_manifest")
            if explore_value is None:
                raise GuardNotSatisfied("Approved Explore publication artifacts are required.")
            explore_manifest = ExplorePublicationManifest.model_validate(explore_value)
            explore_dir = endpoint_dir / "explore"
            explore_dir.mkdir(parents=True, exist_ok=True)
            for filename, reference in {
                "umap.json": explore_manifest.user_facing_map,
                "support.npy": explore_manifest.support_matrix,
                "manifest.json": explore_manifest.user_facing_manifest,
            }.items():
                _descriptor, content = self.artifact_store.get(reference.artifact_id)
                (explore_dir / filename).write_bytes(content)
            metrics_path = endpoint_dir / "metrics.json"
            metrics_path.write_text(
                json.dumps(validation.held_out_metrics, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            index_path = self.repo_root / "registry" / "models" / "endpoints.json"
            if not index_path.exists():
                index_path.parent.mkdir(parents=True, exist_ok=True)
                index_path.write_text('{"endpoints": []}\n', encoding="utf-8")
            model_name = selection.candidate_id.removeprefix("candidate-")
            recipe_row = self._require_semantics_v2(
                session.get(TrainingDatasetWorkflowRow, workflow_id)
            )
            if not recipe_row.assembly_recipe_json:
                raise GuardNotSatisfied("AssemblyRecipe is required for publication.")
            recipe = AssemblyRecipe.model_validate(
                _load_training_document(recipe_row.assembly_recipe_json)
            )
            entry = EndpointEntry(
                endpoint_id=endpoint_id,
                biological_target=build.endpoint_name,
                model_path=(endpoint_dir / "model.pkl").relative_to(self.repo_root).as_posix(),
                feature_schema_path=(endpoint_dir / "feature_schema.json")
                .relative_to(self.repo_root)
                .as_posix(),
                metrics_path=metrics_path.relative_to(self.repo_root).as_posix(),
                explanation=ExplanationCapability(
                    method=(
                        "linear_coefficient" if model_name == "logistic_regression" else "tree_shap"
                    ),
                    required_dependencies=([] if model_name == "logistic_regression" else ["shap"]),
                ),
                model_card_path=(endpoint_dir / "model_card.md")
                .relative_to(self.repo_root)
                .as_posix(),
                dataset_card_path=(endpoint_dir / "dataset_card.json")
                .relative_to(self.repo_root)
                .as_posix(),
                status=EndpointStatus.experimental,
                version="1.0.0",
                created_at=datetime.now(UTC),
                source_refs=recipe.exact_source_combination,
                validation_status=ValidationStatusMetadata(
                    offline="offline_validated",
                    live="live_validation_pending",
                    scientific="scientifically_validation_pending",
                ),
                applicability={
                    "input": "measured transcriptomic profile matching the feature schema",
                    "claim": "endpoint signal score for research use",
                },
                training_dataset_summary={
                    "unique_compounds": bundle.unique_compounds,
                    "total_profiles": bundle.total_profiles,
                },
                model_metrics_summary={
                    key: value
                    for key, value in validation.held_out_metrics.items()
                    if isinstance(value, int | float | str)
                },
                limitations=validation.limitations,
            )
            register_endpoint(entry, repo_root=self.repo_root)
            registry_sha = hashlib.sha256(
                canonical_json(entry.model_dump(mode="json")).encode()
            ).hexdigest()
            receipt = PublicationReceipt(
                workflow_id=workflow_id,
                endpoint_id=endpoint_id,
                endpoint_version=entry.version,
                registry_entry_sha256=registry_sha,
                model_library_visible=True,
                publication_approval_id=approval_id,
            )
            receipt_ref = self._put_lifecycle_document(
                session,
                workflow_id=workflow_id,
                document=receipt,
                artifact_type="publish_receipt",
                logical_name="publish-receipt.json",
                producer="endpoint-publisher",
                idempotency_key=f"{idempotency_key}:receipt",
            )
            serving_ref = self._put_lifecycle_json(
                session,
                workflow_id=workflow_id,
                value={
                    "endpoint_id": endpoint_id,
                    "registry_entry_present": True,
                    "model_library_visible": True,
                    "endpoint_signal_score_supported": True,
                    "safe_or_toxic_verdict_supported": False,
                },
                artifact_type="serving_verification",
                logical_name="serving-verification.json",
                producer="endpoint-publisher",
                idempotency_key=f"{idempotency_key}:serving",
            )
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.COMPLETED,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:completed",
                    initiator=ActorType.ORCHESTRATOR,
                    initiator_id="endpoint-publisher",
                    reason="Immutable experimental endpoint is registered and visible.",
                    artifact_hashes=[receipt_ref.sha256, serving_ref.sha256],
                ),
            )

    def reject_semantics_v2_strategy_set(
        self,
        workflow_id: str,
        *,
        reason: str,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Reject the current proposal set without selecting or assembling anything."""

        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = self._require_semantics_v2(session.get(TrainingDatasetWorkflowRow, workflow_id))
            existing = session.scalar(
                select(WorkflowEventRow).where(
                    WorkflowEventRow.workflow_id == workflow_id,
                    WorkflowEventRow.idempotency_key == f"{idempotency_key}:event",
                )
            )
            if existing is not None:
                return self._snapshot(session, build)
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            if build.current_stage != WorkflowState.AWAITING_ASSEMBLY_STRATEGY_REVIEW.value:
                raise InvalidTransition("Strategy-set rejection requires the v2 review gate.")
            if not row.strategy_proposals_json:
                raise GuardNotSatisfied("Strategy proposals are missing.")
            proposals = StrategyProposalSet.model_validate(
                _load_training_document(row.strategy_proposals_json)
            )
            rejection = StrategySetRejection(
                discovery_round=row.discovery_round,
                proposal_ids=[item.proposal_id for item in proposals.proposals],
                rejected_by=actor,
                reason=reason,
            )
            artifact = self._put_semantics_v2_document(
                session,
                workflow_id=workflow_id,
                document=rejection,
                artifact_type="strategy_set_rejection",
                logical_name=f"strategy-set-rejection-round-{row.discovery_round}.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:artifact",
            )
            row.strategy_set_rejection_json = canonical_json(
                versioned_payload(document=rejection.model_dump(mode="json"))
            )
            row.updated_at = utc_text()
            append_event(
                session,
                build,
                event_type="assembly_strategy_set.rejected",
                actor_type=ActorType.HUMAN.value,
                actor_id=actor,
                idempotency_key=f"{idempotency_key}:event",
                payload={
                    "artifact_id": artifact.id,
                    "sha256": artifact.sha256,
                    "proposal_ids": rejection.proposal_ids,
                },
                from_state=build.current_stage,
                to_state=build.current_stage,
            )
            return self._snapshot(session, build)

    def request_semantics_v2_discovery_revision(
        self,
        workflow_id: str,
        *,
        reason: str,
        provider_policy_revision: dict,
        modality_policy_revision: dict,
        context_constraint_revision: dict,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        """Create a new plan/ledger round while preserving every prior artifact."""

        with self.database.session() as session:
            build = require_build(session, workflow_id)
            row = self._require_semantics_v2(session.get(TrainingDatasetWorkflowRow, workflow_id))
            prior_event = session.scalar(
                select(WorkflowEventRow).where(
                    WorkflowEventRow.workflow_id == workflow_id,
                    WorkflowEventRow.idempotency_key == f"{idempotency_key}:transition",
                )
            )
            if prior_event is not None:
                return self._snapshot(session, build)
            if build.version != expected_version:
                raise StaleWorkflowVersion(
                    "Workflow version is stale.", detail={"current_version": build.version}
                )
            if build.current_stage not in {
                WorkflowState.AWAITING_ASSEMBLY_STRATEGY_REVIEW.value,
                WorkflowState.REGISTERED_PROVIDER_CAPABILITY_BLOCKED.value,
            }:
                raise InvalidTransition("Discovery revision is not available in the current state.")
            if not row.discovery_plan_json or not row.specification_json:
                raise GuardNotSatisfied("Prior discovery plan and specification are required.")
            prior_plan = DiscoveryPlan.model_validate(
                _load_training_document(row.discovery_plan_json)
            )
            revision = DiscoveryRevisionRequest(
                prior_plan_fingerprint=prior_plan.plan_fingerprint,
                requested_by=actor,
                reason=reason,
                provider_policy_revision=provider_policy_revision,
                modality_policy_revision=modality_policy_revision,
                context_constraint_revision=context_constraint_revision,
            )
            registry = load_operational_capability_registry(self.repo_root)
            include = provider_policy_revision.get("include_providers")
            exclude = set(provider_policy_revision.get("exclude_providers") or [])
            if include is not None:
                include = set(include)
                providers = [item for item in registry.providers if item.provider in include]
            else:
                providers = [item for item in registry.providers if item.provider not in exclude]
            registry = registry.model_copy(update={"providers": providers})
            requested_modalities = modality_policy_revision.get("requested_modalities") or list(
                prior_plan.requested_modalities
            )
            specification_artifact = session.get(
                ArtifactRow, prior_plan.approved_endpoint_specification_artifact.artifact_id
            )
            if specification_artifact is None:
                raise GuardNotSatisfied("Approved specification artifact is missing.")
            next_round = row.discovery_round + 1
            plan = build_discovery_plan(
                workflow_id=workflow_id,
                endpoint_identifier=prior_plan.endpoint_identifier,
                approved_specification_artifact=prior_plan.approved_endpoint_specification_artifact,
                discovery_round=next_round,
                biological_target=prior_plan.target_identifiers[0],
                target_synonyms=prior_plan.target_synonyms,
                requested_modalities=requested_modalities,
                registry=registry,
                budgets=prior_plan.scientific_and_execution_budgets,
            )
            if context_constraint_revision:
                plan_values = plan.model_dump(mode="json", exclude={"plan_fingerprint"})
                for task in plan_values["provider_specific_query_tasks"]:
                    task["query_parameters"]["context_constraints"] = context_constraint_revision
                plan = DiscoveryPlan.create(**plan_values)
            ledger, findings = initial_execution_ledger(plan, registry)
            revision_artifact = self._put_semantics_v2_document(
                session,
                workflow_id=workflow_id,
                document=revision,
                artifact_type="discovery_revision_request",
                logical_name=f"discovery-revision-request-round-{next_round}.json",
                producer=actor,
                idempotency_key=f"{idempotency_key}:revision",
            )
            plan_artifact = self._put_semantics_v2_document(
                session,
                workflow_id=workflow_id,
                document=plan,
                artifact_type="discovery_plan",
                logical_name=f"discovery-plan-round-{next_round}.json",
                producer="deterministic-discovery-planner",
                idempotency_key=f"{idempotency_key}:plan",
            )
            ledger_artifact = self._put_semantics_v2_document(
                session,
                workflow_id=workflow_id,
                document=ledger,
                artifact_type="discovery_execution_ledger",
                logical_name=f"discovery-execution-ledger-round-{next_round}.json",
                producer="deterministic-discovery-planner",
                idempotency_key=f"{idempotency_key}:ledger",
            )
            findings_artifact = self._put_semantics_v2_document(
                session,
                workflow_id=workflow_id,
                document=findings,
                artifact_type="registered_provider_capability_missing",
                logical_name=f"provider-capability-findings-round-{next_round}.json",
                producer="deterministic-capability-validator",
                idempotency_key=f"{idempotency_key}:findings",
            )
            row.discovery_round = next_round
            row.discovery_plan_json = canonical_json(
                versioned_payload(document=plan.model_dump(mode="json"))
            )
            row.discovery_execution_ledger_json = canonical_json(
                versioned_payload(document=ledger.model_dump(mode="json"))
            )
            row.provider_capability_findings_json = canonical_json(
                versioned_payload(document=findings.model_dump(mode="json"))
            )
            row.source_candidates_json = None
            row.hydrated_sources_json = None
            row.combination_coverage_json = None
            row.strategy_proposals_json = None
            row.assembly_recipe_json = None
            row.strategy_set_rejection_json = None
            row.source_discovery_authorization_json = None
            row.updated_at = utc_text()
            return self._apply_transition(
                session,
                build,
                TransitionRequest(
                    target_state=WorkflowState.DISCOVERY_PLANNING,
                    expected_version=build.version,
                    idempotency_key=f"{idempotency_key}:transition",
                    initiator=ActorType.HUMAN,
                    initiator_id=actor,
                    reason="A new immutable discovery round was requested.",
                    artifact_hashes=[
                        revision_artifact.sha256,
                        plan_artifact.sha256,
                        ledger_artifact.sha256,
                        findings_artifact.sha256,
                    ],
                ),
            )

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
            semantics_v2 = bool(
                row is not None and row.workflow_semantics_version == WORKFLOW_SEMANTICS_V2
            )
            stage_ready = build.current_stage == (
                WorkflowState.DISCOVERY_PLANNING.value
                if semantics_v2
                else WorkflowState.DERIVING_COMPONENT_REQUIREMENTS.value
            )
            ledger_payload = (
                _load_training_document(row.discovery_execution_ledger_json)
                if semantics_v2 and row and row.discovery_execution_ledger_json
                else None
            )
        if semantics_v2:
            records = list((ledger_payload or {}).get("records") or [])
            roles = {item.get("evidence_role") for item in records}
            blocked_roles = sorted(
                str(role)
                for role in roles
                if role
                and all(
                    item.get("status") == "blocked_provider_capability_missing"
                    for item in records
                    if item.get("evidence_role") == role
                )
            )
            return {
                "schema_version": SCHEMA_VERSION,
                "workflow_semantics_version": WORKFLOW_SEMANTICS_V2,
                "ready": bool(stage_ready and records and not blocked_roles),
                "provider_execution_deferred": True,
                "provider_capability_blockers": blocked_roles,
                "discovery_plan_present": bool(records),
                "specification_approved": specification_ready,
                "component_requirements_present": requirements_ready,
                "stage_ready": stage_ready,
                "reviewed_adapters": adapter_readiness,
                "reviewed_adapter_inventory": self.reviewed_source_adapters.public_inventory(),
            }
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
            raise SourceDiscoveryNotReady("Explicit source-discovery confirmation is required.")
        with self.database.session() as session:
            workflow = session.get(TrainingDatasetWorkflowRow, workflow_id)
            semantics_v2 = bool(
                workflow is not None
                and workflow.workflow_semantics_version == WORKFLOW_SEMANTICS_V2
            )
        if semantics_v2:
            return self.authorize_semantics_v2_discovery(
                workflow_id,
                expected_version=expected_version,
                actor=actor,
                idempotency_key=idempotency_key,
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
            if batch.search_outcome is not None and not any(
                item.get("search_outcome", {}).get("outcome_id") == batch.search_outcome.outcome_id
                for item in current
                if isinstance(item, dict)
            ):
                current.append(
                    {
                        "agent_name": agent_name,
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "search_outcome": batch.search_outcome.model_dump(mode="json"),
                    }
                )
            deterministic_payload = {
                "activity_rows": [item.model_dump(mode="json") for item in batch.activity_rows],
                "identity_bridge_rows": [
                    item.model_dump(mode="json") for item in batch.identity_bridge_rows
                ],
                "transcriptomic_profile_rows": [
                    item.model_dump(mode="json") for item in batch.transcriptomic_profile_rows
                ],
                "inspected_record_count": batch.inspected_record_count,
                "excluded_record_count": batch.excluded_record_count,
                "transport_attempts": batch.transport_attempts,
                "source_request_count": batch.source_request_count,
                "limitations": batch.limitations,
                "source_errors": batch.source_errors,
            }
            if any(
                (
                    batch.activity_rows,
                    batch.identity_bridge_rows,
                    batch.transcriptomic_profile_rows,
                    batch.transport_attempts,
                    batch.source_errors,
                )
            ) and not any(
                item.get("deterministic_records", {}).get("tool_call_id") == tool_call_id
                for item in current
                if isinstance(item, dict)
            ):
                deterministic_artifact = self.artifact_store._put_bytes(
                    session,
                    workflow_id=workflow_id,
                    step_id=step_id,
                    content=canonical_json(deterministic_payload).encode(),
                    mime_type="application/json",
                    artifact_type="deterministic_source_records",
                    logical_name=f"deterministic-source-records-{tool_call_id}.json",
                    producer=f"{batch.adapter_id}@{batch.adapter_version}",
                    idempotency_key=f"deterministic-records:{tool_call_id}",
                )
                current.append(
                    {
                        "agent_name": agent_name,
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "deterministic_records_artifact_id": deterministic_artifact.id,
                        "deterministic_records_artifact_hash": deterministic_artifact.sha256,
                        "deterministic_records": {
                            "tool_call_id": tool_call_id,
                            **deterministic_payload,
                        },
                    }
                )
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
                    "search_outcome": (
                        batch.search_outcome.model_dump(mode="json")
                        if batch.search_outcome
                        else None
                    ),
                    "activity_row_count": len(batch.activity_rows),
                    "identity_bridge_row_count": len(batch.identity_bridge_rows),
                    "transcriptomic_profile_row_count": len(batch.transcriptomic_profile_rows),
                    "transport_attempt_count": len(batch.transport_attempts),
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

    def _agent_search_outcomes(
        self, workflow_id: str, agent_name: str
    ) -> list[VerifiedSourceSearchOutcome]:
        with self.database.session() as session:
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            if row is None or not row.source_observations_json:
                return []
            items = _load_training_document(row.source_observations_json).get("items", [])
        return [
            VerifiedSourceSearchOutcome.model_validate(item["search_outcome"])
            for item in items
            if item.get("agent_name") == agent_name and isinstance(item.get("search_outcome"), dict)
        ]

    def _agent_deterministic_records(self, workflow_id: str, agent_name: str) -> dict[str, object]:
        with self.database.session() as session:
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            items = (
                _load_training_document(row.source_observations_json).get("items", [])
                if row and row.source_observations_json
                else []
            )
        records = [
            item["deterministic_records"]
            for item in items
            if item.get("agent_name") == agent_name
            and isinstance(item.get("deterministic_records"), dict)
        ]
        return {
            "activity_rows": [
                ActivityEvidenceRow.model_validate(value)
                for item in records
                for value in item.get("activity_rows", [])
            ],
            "identity_bridge_rows": [
                CompoundIdentityBridgeRow.model_validate(value)
                for item in records
                for value in item.get("identity_bridge_rows", [])
            ],
            "transcriptomic_profile_rows": [
                TranscriptomicProfileEvidenceRow.model_validate(value)
                for item in records
                for value in item.get("transcriptomic_profile_rows", [])
            ],
            "inspected_record_count": sum(
                int(item.get("inspected_record_count", 0) or 0) for item in records
            ),
            "excluded_record_count": sum(
                int(item.get("excluded_record_count", 0) or 0) for item in records
            ),
            "source_request_count": sum(
                int(item.get("source_request_count", 0) or 0) for item in records
            ),
            "limitations": list(
                dict.fromkeys(
                    str(value)
                    for item in records
                    for value in item.get("limitations", [])
                    if str(value).strip()
                )
            ),
            "source_errors": [
                value
                for item in records
                for value in item.get("source_errors", [])
                if isinstance(value, dict)
            ],
        }

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

    def _assert_source_discovery_budget(self, workflow_id: str, agent_name: str) -> dict[str, int]:
        configuration = self.agent_configuration.controlled_source_discovery_for_agent(agent_name)
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
            totals = {
                "input": 0,
                "output": 0,
                "cost_cents": 0.0,
                "tools": 0,
                "provider_invocations": 0,
                "source_requests": 0,
            }
            for run in runs:
                usage = load_versioned_json(run.usage_json)
                totals["input"] += int(usage.get("input_tokens", 0) or 0)
                totals["output"] += int(usage.get("output_tokens", 0) or 0)
                totals["cost_cents"] += float(usage.get("cost_cents", 0.0) or 0.0)
                totals["provider_invocations"] += int(usage.get("provider_invocations", 0) or 0)
            totals["tools"] = int(
                session.scalar(
                    select(func.count(ToolCallRow.id)).where(
                        ToolCallRow.workflow_id == workflow_id,
                        ToolCallRow.agent_run_id.in_([item.id for item in runs] or ["none"]),
                    )
                )
                or 0
            )
            persisted_calls = session.scalars(
                select(ToolCallRow).where(
                    ToolCallRow.workflow_id == workflow_id,
                    ToolCallRow.agent_run_id.in_([item.id for item in runs] or ["none"]),
                )
            ).all()
            totals["source_requests"] = sum(
                int(
                    (
                        (load_versioned_json(call.result_json).get("output") or {}).get(
                            "source_request_count", 0
                        )
                    )
                    or 0
                )
                for call in persisted_calls
                if call.result_json
            )
            row = session.get(TrainingDatasetWorkflowRow, workflow_id)
            authorization = (
                _load_training_document(row.source_discovery_authorization_json)
                if row and row.source_discovery_authorization_json
                else None
            )
            fragment_items = (
                _load_training_document(row.source_fragments_json).get("items", [])
                if row and row.source_fragments_json
                else []
            )
            if fragment_items:
                totals["tools"] = sum(
                    int(item.get("fragment", {}).get("tool_calls", 0) or 0)
                    for item in fragment_items
                )
                totals["source_requests"] = sum(
                    int(item.get("fragment", {}).get("scientific_source_requests", 0) or 0)
                    for item in fragment_items
                )
        if authorization is None:
            raise GuardNotSatisfied("Source discovery has not been explicitly authorized.")
        if any(run.agent_name == agent_name for run in runs):
            # The harness rehydrates the deterministic persisted terminal result. This path
            # consumes no provider, source-request, token, tool-call, or cost budget.
            return {
                "provider_invocations": max(
                    0,
                    configuration.global_maximum_provider_invocations
                    - int(totals["provider_invocations"]),
                ),
                "scientific_source_requests": max(
                    0,
                    configuration.global_maximum_source_requests - int(totals["source_requests"]),
                ),
            }
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
        if totals["provider_invocations"] + configuration.maximum_turns > (
            configuration.global_maximum_provider_invocations
        ):
            raise GuardNotSatisfied("Remaining global provider-invocation budget is insufficient.")
        if totals["source_requests"] >= configuration.global_maximum_source_requests:
            raise GuardNotSatisfied("The global scientific-source request budget is exhausted.")
        return {
            "provider_invocations": (
                configuration.global_maximum_provider_invocations
                - int(totals["provider_invocations"])
            ),
            "scientific_source_requests": (
                configuration.global_maximum_source_requests - int(totals["source_requests"])
            ),
        }

    def _extract_selected_activity_rows(
        self,
        workflow_id: str,
        *,
        step_id: str,
        biological_target: str,
        observations: list[VerifiedSourceObservation],
        review: DiscoveryAgentReviewOutcome | None,
    ) -> VerifiedSourceObservationBatch | None:
        """Extract one model-ranked, hydrated assay per modality without another LLM turn."""

        ranked = list(review.ranked_observation_ids if review else [])
        rank = {observation_id: index for index, observation_id in enumerate(ranked)}
        candidates = sorted(
            [
                item
                for item in observations
                if item.adapter_id == "pubchem-bioassay"
                and re.fullmatch(r"AID:[1-9][0-9]{0,11}", item.stable_source_identifier)
            ],
            key=lambda item: (rank.get(item.observation_id, 100_000), item.observation_id),
        )
        selected: dict[str, ActivityDiscoveryModality] = {}
        used: set[str] = set()
        for modality in ("binding", "agonism", "antagonism"):
            candidate = next(
                (
                    item
                    for item in candidates
                    if modality in item.candidate_modalities
                    and item.stable_source_identifier not in used
                ),
                None,
            )
            if candidate is not None:
                selected[candidate.stable_source_identifier] = ActivityDiscoveryModality(modality)
                used.add(candidate.stable_source_identifier)
        if not selected:
            return None
        request = ActivityResultExtractionInput(
            source_system="pubchem-bioassay",
            source_identifiers=list(selected),
            modality_by_source=selected,
            biological_target=biological_target,
            maximum_rows_per_assay=500,
            maximum_results=5,
        )
        invocation = ToolInvocation(
            tool_name="extract_activity_result_rows",
            arguments=request.model_dump(mode="json"),
            workflow_id=workflow_id,
            step_id=step_id,
            workflow_stage=WorkflowState.DISCOVERING_ACTIVITY_EVIDENCE,
            permission_scope=["training-dataset:extract_activity_result_rows"],
            idempotency_key="compound-first:activity-result-extraction:v1",
        )
        return self.reviewed_source_adapters.execute(
            "extract_activity_result_rows", request, invocation
        )

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
        remaining_global_budget = self._assert_source_discovery_budget(
            workflow_id, definition.agent_name
        )
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
        missing_prerequisites = self._source_discovery_missing_prerequisites(
            definition.agent_name, prior_fragments
        )
        if missing_prerequisites:
            return self._skip_source_discovery_agent(
                workflow_id,
                definition=definition,
                stage=stage,
                next_stage=next_stage,
                snapshot=snapshot,
                requirements=requirements,
                missing_prerequisites=missing_prerequisites,
            )
        requirement_summary = [
            {
                "role": item.role.value,
                "mandatory": item.mandatory,
                "minimum_metadata": item.minimum_metadata,
                "acceptable_identifier_types": item.acceptable_identifier_types,
            }
            for item in requirements.requirements
        ]
        validated_artifacts: dict[str, object] = {
            "endpoint_discovery_scope": (
                specification.endpoint_discovery_scope.model_dump(mode="json")
                if specification.endpoint_discovery_scope
                else None
            ),
            "requirement_summary": requirement_summary,
            "approved_scientific_policies": initial_context.approved_scientific_policies,
            "reviewed_adapter_capabilities": self._compact_reviewed_adapter_capabilities(
                definition.allowed_tools
            ),
        }
        if definition.agent_name == "Chemical Identity and Structure Source Discovery Agent":
            compound_identifier_groups = self._compound_identifier_groups(prior_fragments)
            validated_artifacts = {
                "requirement_summary": requirement_summary,
                "verified_identifier_field_summaries": [
                    {
                        "source_id": record.get("source_id"),
                        "identifier_fields": record.get("identifier_fields", []),
                        "sampled_compound_identifiers": record.get(
                            "sampled_compound_identifiers", []
                        ),
                    }
                    for item in prior_fragments
                    for record in item.get("fragment", {}).get("candidate_records", [])
                ],
                "compound_identifier_groups": compound_identifier_groups,
                "reviewed_adapter_capabilities": self._compact_reviewed_adapter_capabilities(
                    definition.allowed_tools
                ),
            }
        elif definition.agent_name == "Transcriptomic Evidence Discovery Agent":
            identity_rows = [
                record
                for item in prior_fragments
                for record in item.get("fragment", {}).get("identity_bridge_rows", [])
                if isinstance(record, dict)
                and record.get("mapping_status") == "exact"
                and record.get("canonical_name")
            ]
            validated_artifacts = {
                "requirement_summary": requirement_summary,
                "compound_first_dependency": {
                    "resolved_identity_count": len(identity_rows),
                    "search_batch": self._compound_first_transcriptomic_search_batch(
                        identity_rows, prior_fragments
                    ),
                    "complete_compound_list_withheld_from_model": True,
                },
                "reviewed_adapter_capabilities": self._compact_reviewed_adapter_capabilities(
                    definition.allowed_tools
                ),
            }
        elif definition.agent_name == "Supporting Metadata Discovery Agent":
            validated_artifacts = {
                "requirement_summary": requirement_summary,
                "verified_source_identifiers": sorted(
                    {
                        str(record.get("stable_accession"))
                        for item in prior_fragments
                        if item.get("agent_name")
                        in {
                            "Activity Evidence Discovery Agent",
                            "Transcriptomic Evidence Discovery Agent",
                        }
                        for record in item.get("fragment", {}).get("candidate_records", [])
                        if record.get("stable_accession")
                    }
                ),
                "reviewed_adapter_capabilities": self._compact_reviewed_adapter_capabilities(
                    definition.allowed_tools
                ),
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
        configuration = self.agent_configuration.controlled_source_discovery_for_agent(
            definition.agent_name
        )
        request = specialized_agent_request(
            definition=available_definition,
            workflow_id=workflow_id,
            step_id=step.id,
            workflow_stage=stage,
            initial_context=initial_context,
            validated_artifacts=validated_artifacts,
            configuration=configuration,
        )
        request = request.model_copy(
            update={
                "context": {
                    **request.context,
                    "remaining_global_provider_invocations": remaining_global_budget[
                        "provider_invocations"
                    ],
                    "remaining_global_scientific_source_requests": remaining_global_budget[
                        "scientific_source_requests"
                    ],
                }
            }
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
        search_outcomes = self._agent_search_outcomes(workflow_id, definition.agent_name)
        deterministic_tool_calls = 0
        deterministic_source_requests = 0
        if definition.agent_name == "Activity Evidence Discovery Agent":
            try:
                extraction = self._extract_selected_activity_rows(
                    workflow_id,
                    step_id=step.id,
                    biological_target=(
                        specification.endpoint_discovery_scope.biological_target
                        if specification.endpoint_discovery_scope
                        else specification.biological_target
                    )
                    or specification.biological_target,
                    observations=observations,
                    review=review,
                )
            except ReviewedSourceExecutionError as exc:
                attempts = list(exc.telemetry.transport_attempts)
                extraction = VerifiedSourceObservationBatch(
                    adapter_id="pubchem-bioassay",
                    adapter_version="1.0.0",
                    operation="extract_activity_result_rows",
                    cache_status="live",
                    source_request_count=(
                        max(1, len(attempts))
                        if exc.telemetry.http_status is not None
                        or exc.telemetry.final_allowlisted_host
                        or attempts
                        else 0
                    ),
                    limitations=[
                        "Compound-level activity extraction failed safely; downstream identity "
                        "and transcriptomic discovery may be dependency-skipped."
                    ],
                    source_errors=[
                        {
                            "stable_identifier": "selected_activity_assays",
                            "error_code": exc.telemetry.failure_category or "source_request_failed",
                            "safe_message": str(exc)[:500],
                            "source_request_started": bool(
                                exc.telemetry.http_status is not None
                                or exc.telemetry.final_allowlisted_host
                                or attempts
                            ),
                        }
                    ],
                    transport_attempts=attempts[:20],
                )
            except ReviewedSourceInvocationError as exc:
                extraction = VerifiedSourceObservationBatch(
                    adapter_id="pubchem-bioassay",
                    adapter_version="1.0.0",
                    operation="extract_activity_result_rows",
                    cache_status="live",
                    source_request_count=0,
                    limitations=[
                        "Compound-level activity extraction was rejected before transport."
                    ],
                    source_errors=[
                        {
                            "stable_identifier": "selected_activity_assays",
                            "error_code": "source_input_or_request_invalid",
                            "safe_message": str(exc)[:500],
                            "source_request_started": False,
                        }
                    ],
                )
            if extraction is not None:
                deterministic_tool_calls = 1
                deterministic_source_requests = extraction.source_request_count
                self._persist_tool_observations(
                    workflow_id,
                    step_id=step.id,
                    agent_name=definition.agent_name,
                    tool_call_id=deterministic_id(
                        "tool", workflow_id, "compound-first-activity-extraction"
                    ),
                    tool_name="extract_activity_result_rows",
                    batch=extraction,
                )
                observations = self._agent_observations(workflow_id, definition.agent_name)
        deterministic_records = self._agent_deterministic_records(
            workflow_id, definition.agent_name
        )
        if definition.agent_name == "Chemical Identity and Structure Source Discovery Agent":
            deterministic_records["identity_bridge_rows"] = self._merge_identity_bridge_rows(
                list(deterministic_records["identity_bridge_rows"]), prior_fragments
            )
        with self.database.session() as session:
            calls = session.scalars(
                select(ToolCallRow).where(ToolCallRow.agent_run_id == run_id)
            ).all()
            scientific_source_requests = sum(
                int(
                    (load_versioned_json(call.result_json).get("output") or {}).get(
                        "source_request_count", 0
                    )
                    or 0
                )
                for call in calls
                if call.result_json
            )
        incomplete_stage = next(
            (
                str(event.detail.get("discovery_substage"))
                for event in reversed(result.trace)
                if event.event_type == "agent_run.context_precheck"
                and event.detail.get("discovery_substage")
            ),
            None,
        )
        roles = list(
            dict.fromkeys(role for observation in observations for role in observation.source_roles)
        ) or [
            role
            for role in self.reviewed_source_adapters.MANDATORY_ROLES
            if role in {requirement.role for requirement in requirements.requirements}
        ]
        fragment = compile_verified_source_fragment(
            fragment_id=deterministic_id("fragment", workflow_id, definition.agent_name),
            component_roles=roles or [requirements.requirements[0].role],
            observations=observations,
            review=review,
            search_outcomes=search_outcomes,
            run_status=result.status.value,
            error_code=result.error.code if result.error else None,
            error_category=result.error.category if result.error else None,
            provider_invocations=result.usage.provider_invocations,
            tool_calls=result.tool_calls + deterministic_tool_calls,
            scientific_source_requests=(scientific_source_requests + deterministic_source_requests),
            incomplete_stage=(
                incomplete_stage if result.status is not AgentRunStatus.COMPLETED else None
            ),
            activity_rows=list(deterministic_records["activity_rows"]),
            identity_bridge_rows=list(deterministic_records["identity_bridge_rows"]),
            transcriptomic_profile_rows=list(deterministic_records["transcriptomic_profile_rows"]),
            inspected_record_count=int(deterministic_records.get("inspected_record_count", 0) or 0),
            excluded_record_count=int(deterministic_records.get("excluded_record_count", 0) or 0),
            deterministic_limitations=list(deterministic_records.get("limitations", []))
            + [
                "Deterministic source item "
                f"{item.get('stable_identifier', 'unresolved')} failed safely "
                f"({item.get('error_code', 'source_error')}): "
                f"{item.get('safe_message', 'No safe detail available.')}"
                for item in deterministic_records.get("source_errors", [])
                if isinstance(item, dict)
            ],
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

    @staticmethod
    def _compound_identifier_groups(prior_fragments: list[dict]) -> list[dict[str, object]]:
        """Return only typed, stable compound identifiers; assay/source IDs are excluded."""

        sampled = {
            str(value).strip()
            for item in prior_fragments
            for record in item.get("fragment", {}).get("candidate_records", [])
            if isinstance(record, dict)
            for value in record.get("sampled_compound_identifiers", [])
            if isinstance(value, str) and value.strip()
        }
        sampled.update(
            str(row.get("pubchem_cid"))
            for item in prior_fragments
            for row in item.get("fragment", {}).get("activity_rows", [])
            if isinstance(row, dict) and row.get("pubchem_cid")
        )
        cids = sorted(
            {
                match.group(1)
                for value in sampled
                if (match := re.fullmatch(r"CID:([1-9][0-9]{0,11})", value, flags=re.I))
            },
            key=int,
        )
        inchikeys = sorted(
            value.upper()
            for value in sampled
            if re.fullmatch(r"[A-Z]{14}-[A-Z]{10}-[A-Z]", value.upper())
        )
        return [
            {"identifier_type": identifier_type, "identifiers": values}
            for identifier_type, values in (("cid", cids), ("inchikey", inchikeys))
            if values
        ]

    @staticmethod
    def _merge_identity_bridge_rows(
        rows: list[CompoundIdentityBridgeRow], prior_fragments: list[dict]
    ) -> list[CompoundIdentityBridgeRow]:
        aids_by_cid: dict[str, set[str]] = {}
        for item in prior_fragments:
            for row in item.get("fragment", {}).get("activity_rows", []):
                if isinstance(row, dict) and row.get("pubchem_cid") and row.get("pubchem_aid"):
                    aids_by_cid.setdefault(str(row["pubchem_cid"]), set()).add(
                        str(row["pubchem_aid"])
                    )
        merged: dict[str, CompoundIdentityBridgeRow] = {}
        rank = {"failed": 0, "missing": 1, "ambiguous": 2, "exact": 3}
        for row in rows:
            prior = merged.get(row.pubchem_cid)
            if prior is None:
                merged[row.pubchem_cid] = row
                continue
            preferred = row if rank[row.mapping_status] > rank[prior.mapping_status] else prior
            merged[row.pubchem_cid] = preferred.model_copy(
                update={
                    "synonyms": sorted(set(prior.synonyms + row.synonyms))[:50],
                    "raw_artifact_ids": sorted(set(prior.raw_artifact_ids + row.raw_artifact_ids)),
                    "raw_artifact_hashes": sorted(
                        set(prior.raw_artifact_hashes + row.raw_artifact_hashes)
                    ),
                    "evidence_references": sorted(
                        set(prior.evidence_references + row.evidence_references)
                    ),
                }
            )
        return [
            row.model_copy(update={"activity_aids": sorted(aids_by_cid.get(cid, set()))})
            for cid, row in sorted(merged.items())
        ]

    @staticmethod
    def _compound_first_transcriptomic_search_batch(
        identity_rows: list[dict], prior_fragments: list[dict], maximum: int = 5
    ) -> list[dict[str, object]]:
        """Select a bounded identity batch with modality coverage before CID ordering."""

        exact_by_cid = {
            str(item.get("pubchem_cid")): item
            for item in identity_rows
            if item.get("mapping_status") == "exact"
            and item.get("pubchem_cid")
            and item.get("canonical_name")
        }
        activity_rows = [
            row
            for item in prior_fragments
            for row in item.get("fragment", {}).get("activity_rows", [])
            if isinstance(row, dict)
            and row.get("extraction_status") == "included"
            and row.get("pubchem_cid") in exact_by_cid
        ]
        selected: list[str] = []
        for modality in ("binding", "agonism", "antagonism"):
            candidate = next(
                (
                    str(row["pubchem_cid"])
                    for row in activity_rows
                    if row.get("modality") == modality and str(row["pubchem_cid"]) not in selected
                ),
                None,
            )
            if candidate:
                selected.append(candidate)
        selected.extend(cid for cid in sorted(exact_by_cid) if cid not in selected)
        return [
            {
                "pubchem_cid": exact_by_cid[cid].get("pubchem_cid"),
                "inchikey": exact_by_cid[cid].get("inchikey"),
                "canonical_name": exact_by_cid[cid].get("canonical_name"),
                "verified_synonyms": list(exact_by_cid[cid].get("synonyms", []))[:3],
            }
            for cid in selected[:maximum]
        ]

    @staticmethod
    def _source_discovery_missing_prerequisites(
        agent_name: str, prior_fragments: list[dict]
    ) -> list[str]:
        records = [
            record
            for item in prior_fragments
            for record in item.get("fragment", {}).get("candidate_records", [])
            if isinstance(record, dict)
        ]
        if agent_name == "Chemical Identity and Structure Source Discovery Agent":
            if not records:
                return ["verified activity or transcriptomic source candidate"]
            if not WorkflowService._compound_identifier_groups(prior_fragments):
                return [
                    "dependency_unmet: hydrated activity candidates did not expose valid "
                    "sampled compound identifiers"
                ]
        if agent_name == "Transcriptomic Evidence Discovery Agent":
            exact_identity_rows = [
                row
                for item in prior_fragments
                for row in item.get("fragment", {}).get("identity_bridge_rows", [])
                if isinstance(row, dict)
                and row.get("mapping_status") == "exact"
                and row.get("pubchem_cid")
                and row.get("canonical_name")
            ]
            if not exact_identity_rows:
                return [
                    "dependency_unmet: activity-derived compounds have no exact canonical "
                    "identity mapping for compound-centric transcriptomic discovery"
                ]
        if agent_name == "Supporting Metadata Discovery Agent":
            has_linked_source = any(
                (record.get("stable_accession") not in {None, "", "unresolved", "linked-metadata"})
                or bool(record.get("downloadable_artifacts"))
                or bool(record.get("source_references"))
                for record in records
            )
            if not has_linked_source:
                return ["verified source candidate with stable identifier or linked artifact"]
        return []

    def _skip_source_discovery_agent(
        self,
        workflow_id: str,
        *,
        definition: SpecializedAgentDefinition,
        stage: WorkflowState,
        next_stage: WorkflowState,
        snapshot: WorkflowSnapshot,
        requirements: TrainingDatasetComponentRequirements,
        missing_prerequisites: list[str],
    ) -> WorkflowSnapshot:
        step = self.create_step(
            workflow_id,
            stage,
            idempotency_key=f"source-discovery:{stage.value}:step",
            input_payload={
                "agent_name": definition.agent_name,
                "dependency_status": "skipped_dependency_not_met",
                "missing_prerequisites": missing_prerequisites,
            },
        )
        relevant_roles = [
            item.role
            for item in requirements.requirements
            if (
                definition.agent_name.startswith("Chemical Identity")
                and item.role.value
                in {"compound_identity", "chemical_structure", "source_id_mapping"}
            )
            or (
                definition.agent_name.startswith("Supporting Metadata")
                and item.role.value
                in {
                    "assay_metadata",
                    "transcriptomic_conditions",
                    "sample_metadata",
                    "provenance_license",
                    "counter_screen",
                    "methodological_evidence",
                }
            )
        ]
        fragment = compile_verified_source_fragment(
            fragment_id=deterministic_id("fragment", workflow_id, definition.agent_name),
            component_roles=relevant_roles or [requirements.requirements[0].role],
            observations=[],
            review=None,
            run_status="skipped_dependency_not_met",
            provider_invocations=0,
            tool_calls=0,
            scientific_source_requests=0,
            missing_prerequisites=missing_prerequisites,
            incomplete_stage=stage.value,
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
                "agent_run_id": None,
                "agent_run_status": "skipped_dependency_not_met",
                "fragment_id": fragment.fragment_id,
                "fragment_hash": fragment_hash,
                "observation_count": 0,
                "provider_invocations": 0,
                "tool_calls": 0,
                "scientific_source_requests": 0,
                "missing_prerequisites": missing_prerequisites,
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
                reason=f"{definition.agent_name} skipped because dependencies were not met.",
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
            fragments=fragments,
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

        definitions = {item.agent_name: item for item in SPECIALIZED_AGENT_SEQUENCE}
        stages = {
            WorkflowState.DISCOVERING_ACTIVITY_EVIDENCE: (
                definitions["Activity Evidence Discovery Agent"],
                WorkflowState.DISCOVERING_IDENTITY_AND_STRUCTURE_SOURCES,
            ),
            WorkflowState.DISCOVERING_IDENTITY_AND_STRUCTURE_SOURCES: (
                definitions["Chemical Identity and Structure Source Discovery Agent"],
                WorkflowState.DISCOVERING_TRANSCRIPTOMIC_EVIDENCE,
            ),
            WorkflowState.DISCOVERING_TRANSCRIPTOMIC_EVIDENCE: (
                definitions["Transcriptomic Evidence Discovery Agent"],
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
        repeated: WorkflowSnapshot | None = None
        with self.database.session() as session:
            prior = session.scalar(
                select(WorkflowEventRow).where(
                    WorkflowEventRow.workflow_id == workflow_id,
                    WorkflowEventRow.idempotency_key.in_(
                        {
                            f"{idempotency_key}:specification:finalize:transition",
                            f"{idempotency_key}:compile:transition",
                            f"{idempotency_key}:discovery:transition",
                            f"{idempotency_key}:hydration:transition",
                        }
                    ),
                )
            )
            if prior is not None:
                repeated = self._snapshot(session, require_build(session, workflow_id))
        if repeated is not None:
            if (
                self.semantics_v2_discovery_executor is not None
                and repeated.workflow_semantics_version == WORKFLOW_SEMANTICS_V2
                and repeated.current_stage
                in {
                    WorkflowState.DISCOVERING_SOURCE_CANDIDATES,
                    WorkflowState.HYDRATING_SOURCE_CANDIDATES,
                    WorkflowState.COMPUTING_COMBINATION_COVERAGE,
                }
            ):
                repeated = repeated.model_copy(
                    update={
                        "discovery_progress": self.semantics_v2_discovery_executor.progress(
                            workflow_id, repeated.current_stage, repeated.version
                        )
                    }
                )
            return repeated
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
        if snapshot.current_stage is WorkflowState.APPROVED_SPECIFICATION:
            return self.plan_semantics_v2_discovery(
                workflow_id,
                expected_version=expected_version,
                actor="deterministic-orchestrator",
                idempotency_key=f"{idempotency_key}:v2-plan",
            )
        if snapshot.current_stage is WorkflowState.DISCOVERY_PLANNING:
            return snapshot
        if snapshot.current_stage is WorkflowState.DISCOVERING_SOURCE_CANDIDATES:
            if self.semantics_v2_discovery_executor is None:
                raise GuardNotSatisfied("Semantics-v2 discovery executor is not configured.")
            result = self.semantics_v2_discovery_executor.execute_discovery(workflow_id)
            snapshot = self.get_build(workflow_id)
            if result.candidate_set is not None:
                with self.database.session() as session:
                    build = require_build(session, workflow_id)
                    snapshot = self._apply_transition(
                        session,
                        build,
                        TransitionRequest(
                            target_state=WorkflowState.HYDRATING_SOURCE_CANDIDATES,
                            expected_version=build.version,
                            idempotency_key=f"{idempotency_key}:discovery:transition",
                            initiator=ActorType.ORCHESTRATOR,
                            initiator_id="semantics-v2-discovery-executor",
                            reason=(
                                "Every immutable discovery ledger task is terminal and the "
                                "complete source candidate inventory is persisted."
                            ),
                            artifact_hashes=[item.sha256 for item in result.artifact_references],
                        ),
                    )
            return snapshot.model_copy(
                update={
                    "discovery_progress": self.semantics_v2_discovery_executor.progress(
                        workflow_id, snapshot.current_stage, snapshot.version
                    )
                }
            )
        if snapshot.current_stage is WorkflowState.HYDRATING_SOURCE_CANDIDATES:
            if self.semantics_v2_discovery_executor is None:
                raise GuardNotSatisfied("Semantics-v2 discovery executor is not configured.")
            result = self.semantics_v2_discovery_executor.execute_hydration(workflow_id)
            with self.database.session() as session:
                build = require_build(session, workflow_id)
                snapshot = self._apply_transition(
                    session,
                    build,
                    TransitionRequest(
                        target_state=WorkflowState.COMPUTING_COMBINATION_COVERAGE,
                        expected_version=build.version,
                        idempotency_key=f"{idempotency_key}:hydration:transition",
                        initiator=ActorType.ORCHESTRATOR,
                        initiator_id="semantics-v2-discovery-executor",
                        reason=(
                            "Every retained source candidate has a persisted hydration "
                            "outcome; combination coverage has not run."
                        ),
                        artifact_hashes=[item.sha256 for item in result.artifact_references],
                    ),
                )
            return snapshot.model_copy(
                update={
                    "discovery_progress": self.semantics_v2_discovery_executor.progress(
                        workflow_id, snapshot.current_stage, snapshot.version
                    )
                }
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
        endpoint_discovery_scope: EndpointDiscoveryScope | None = None,
    ) -> WorkflowSnapshot:
        """Human-authorized deterministic recompilation; never a provider retry."""

        snapshot = self.get_build(workflow_id)
        if snapshot.current_stage is not WorkflowState.AWAITING_DATASET_SPECIFICATION_REVISION:
            raise InvalidTransition("Only a specification revision gate may be retried.")
        if snapshot.version != expected_version:
            raise StaleWorkflowVersion(
                "Workflow version is stale.", detail={"current_version": snapshot.version}
            )
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
        artifact_hashes = [outcome.sha256]
        if endpoint_discovery_scope is not None:
            stored_scope = self.persist_training_dataset_document(
                workflow_id,
                document_name="endpoint_discovery_scope",
                value=endpoint_discovery_scope.model_dump(mode="json"),
                actor=actor,
                idempotency_key=f"{idempotency_key}:discovery-scope",
            )
            artifact_hashes.append(stored_scope["sha256"])
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
                artifact_hashes=artifact_hashes,
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
            discovery_scope = (
                EndpointDiscoveryScope.model_validate(
                    _load_training_document(row.endpoint_discovery_scope_json)
                )
                if row.endpoint_discovery_scope_json
                else None
            )
        step = self.create_step(
            workflow_id,
            WorkflowState.COMPILING_TARGET_DATASET_SPECIFICATION,
            idempotency_key=f"{idempotency_key}:step",
            input_payload={
                "compiler": "DatasetSpecificationCompiler",
                "compiler_version": DatasetSpecificationCompiler.version,
                "provider_invocations": 0,
                "human_scoped_discovery_scope": discovery_scope is not None,
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
            discovery_scope=discovery_scope,
            schema_version=TRAINING_DATASET_CONTRACT_VERSION,
        )
        compilation_revision = 1 + sum(
            item.artifact_type == "dataset_specification_compilation_outcome"
            for item in self.artifact_store.list_artifacts(workflow_id)
        )
        hints_artifact = self.artifact_store.put_json(
            workflow_id=workflow_id,
            step_id=step.id,
            value=hints.model_dump(mode="json"),
            artifact_type="endpoint_request_semantic_hints",
            logical_name=f"endpoint-request-semantic-hints-v{compilation_revision}.json",
            producer="deterministic-orchestrator",
            original_source=None,
            idempotency_key=f"{idempotency_key}:semantic-hints",
        )
        outcome_artifact = self.artifact_store.put_json(
            workflow_id=workflow_id,
            step_id=step.id,
            value=outcome.model_dump(mode="json"),
            artifact_type="dataset_specification_compilation_outcome",
            logical_name=(
                f"dataset-specification-compilation-outcome-v{compilation_revision}.json"
            ),
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
            logical_name=f"dataset-specification-review-record-v{compilation_revision}.json",
            producer="deterministic-orchestrator",
            original_source=None,
            idempotency_key=f"{idempotency_key}:review-status",
        )
        artifact_hashes = [
            hints_artifact.sha256,
            outcome_artifact.sha256,
            review_record_artifact.sha256,
        ]
        compiled_scope = outcome.endpoint_discovery_scope
        if compiled_scope is not None:
            if discovery_scope is None:
                stored_scope = self.persist_training_dataset_document(
                    workflow_id,
                    document_name="endpoint_discovery_scope",
                    value=compiled_scope.model_dump(mode="json"),
                    actor="DatasetSpecificationCompiler",
                    idempotency_key=f"{idempotency_key}:discovery-scope",
                )
                artifact_hashes.append(stored_scope["sha256"])
            else:
                scope_artifact = next(
                    (
                        item
                        for item in reversed(self.artifact_store.list_artifacts(workflow_id))
                        if item.artifact_type == "endpoint_discovery_scope"
                    ),
                    None,
                )
                if scope_artifact is None:
                    raise GuardNotSatisfied("Endpoint discovery scope artifact is missing.")
                artifact_hashes.append(scope_artifact.sha256)
        draft_artifact = None
        if outcome.specification is not None:
            draft_artifact = self.artifact_store.put_json(
                workflow_id=workflow_id,
                step_id=step.id,
                value=outcome.specification.model_dump(mode="json"),
                artifact_type="training_dataset_specification_draft",
                logical_name=f"training-dataset-specification-draft-v{compilation_revision}.json",
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
            raise WorkflowConflict("No agent harness is configured.")
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
            raise WorkflowConflict("No agent harness is configured.")
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
                "offline-replay://validated"
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
            self._guard_historical_training_build_read_only(session, build)
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
            self._guard_historical_training_build_read_only(session, build)
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
            errors = []
            for row in rows:
                detail = load_versioned_json(row.detail_json)
                safe_message = row.safe_message
                run_id = detail.get("agent_run_id")
                if row.code == "tool_failure" and isinstance(run_id, str):
                    run = session.get(AgentRunRow, run_id)
                    call = session.scalar(
                        select(ToolCallRow)
                        .where(ToolCallRow.agent_run_id == run_id)
                        .order_by(ToolCallRow.created_at, ToolCallRow.id)
                        .limit(1)
                    )
                    if run is not None and call is not None and call.result_json:
                        result_payload = load_versioned_json(call.result_json)
                        diagnostic = self._historical_tool_diagnostic(run, call, result_payload)
                        if diagnostic:
                            detail = {**detail, "tool_diagnostic": diagnostic}
                            safe_message = diagnostic["safe_message"]
                errors.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "id": row.id,
                        "step_id": row.step_id,
                        "code": row.code,
                        "category": row.category,
                        "retryable": bool(row.retryable),
                        "safe_message": safe_message,
                        "detail": detail,
                        "created_at": row.created_at,
                    }
                )
            return errors

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
                    code="offline_fixture_controlled_failure",
                    category="demonstration",
                    retryable=1,
                    safe_message="Prepared controlled offline-provider failure; retry is allowed.",
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
        self._guard_historical_training_build_read_only(session, build)
        if build.workflow_kind == WorkflowKind.TRAINING_DATASET_DISCOVERY.value:
            workflow = session.get(TrainingDatasetWorkflowRow, build.id)
            if (
                workflow is not None
                and workflow.workflow_semantics_version == WORKFLOW_SEMANTICS_V2
                and request.target_state in SEMANTICS_V1_ONLY_TRAINING_STATES
            ):
                raise InvalidTransition(
                    "Workflow-semantics v2 builds cannot enter legacy post-specification states."
                )
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
                endpoint_discovery_scope = (
                    EndpointDiscoveryScope.model_validate(
                        _load_training_document(workflow.endpoint_discovery_scope_json)
                    )
                    if workflow.endpoint_discovery_scope_json
                    else None
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
                    endpoint_discovery_scope=endpoint_discovery_scope,
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
            workflow_row = session.get(TrainingDatasetWorkflowRow, build.id)
            approved_target = (
                WorkflowState.APPROVED_SPECIFICATION
                if workflow_row is not None
                and workflow_row.workflow_semantics_version == WORKFLOW_SEMANTICS_V2
                else WorkflowState.DERIVING_COMPONENT_REQUIREMENTS
            )
            target_by_decision = {
                ApprovalDecisionValue.APPROVE: approved_target,
                ApprovalDecisionValue.CHOOSE_ALTERNATIVE: approved_target,
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
            raise InvalidTransition("This approval type does not advance the discovery workflow.")
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
        training_workflow = session.get(TrainingDatasetWorkflowRow, row.id)
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
            workflow_semantics_version=(
                training_workflow.workflow_semantics_version
                if training_workflow is not None
                else "1.0.0"
            ),
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

    def _agent_run_dict(self, session: Session, row: AgentRunRow, *, include_trace: bool) -> dict:
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

        def public_tool_result(call: ToolCallRow) -> dict | None:
            if not call.result_json:
                return None
            payload = load_versioned_json(call.result_json)
            diagnostic = self._historical_tool_diagnostic(row, call, payload)
            if diagnostic:
                error = dict(payload.get("error") or {})
                error["safe_message"] = diagnostic["safe_message"]
                error["tool_diagnostic"] = diagnostic
                payload = {**payload, "error": error}
            return payload

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
                        "result": public_tool_result(call),
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
            WorkflowState.REGISTERED_PROVIDER_CAPABILITY_BLOCKED,
            WorkflowState.AWAITING_ASSEMBLY_STRATEGY_REVIEW,
            WorkflowState.ASSEMBLY_RECIPE_APPROVED,
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
