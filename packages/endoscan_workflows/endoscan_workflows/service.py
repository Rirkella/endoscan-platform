"""Transactional workflow service. It owns workflow truth but no scientific logic."""

from __future__ import annotations

import hashlib
import json
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
from .state_machine import TransitionSpec, WorkflowGraph
from .training_dataset import (
    BLIND_TRAINING_DATASET_DISCOVERY,
    SPECIALIZED_AGENT_SEQUENCE,
    TRAINING_DATASET_CONTRACT_VERSION,
    BlindBenchmarkInitialContext,
    DatasetSpecificationAgentOutcome,
    DatasetSpecificationCompiler,
    DatasetSpecificationReviewOutcome,
    DatasetSpecificationReviewRecord,
    DatasetSpecificationSemanticValidation,
    DiscoveryBeforeStrategyGuard,
    SourceCapabilityMatrix,
    SpecializedAgentDefinition,
    TrainingDatasetAssemblyReview,
    TrainingDatasetComponentRequirements,
    TrainingDatasetPreparationPlan,
    TrainingDatasetSpecification,
    TrainingDatasetSpecificationDraft,
    VerifiedSourceInventory,
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
        maximum_workflows: int = 100,
    ):
        self.database = database
        self.artifact_store = artifact_store
        self.graph = graph
        self.repo_root = Path(repo_root).resolve()
        self.harness = harness
        self.agent_configuration = agent_configuration or AgentConfiguration()
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
        ãMvæÚ$z{-®éÜj×6W76–öâÀ¢'V–ÆBÀ¢G&ç6—F–öå&WVW7B€¢F&vWE÷7FFS×F&vWEö'•öFV6—6–öå¶FV6—6–öâæFV6—6–öåÒÀ¢W‡V7FVE÷fW'6–öãÖ'V–ÆBçfW'6–öâÀ¢–FV×÷FVæ7•ö¶W“Öb'¶FV6—6–öâæ–FV×÷FVæ7•ö¶W—Ó§G&ç6—F–öâ"À¢–æ—F–F÷#Ô7F÷%G—Rä…TÔâÀ¢–æ—F–F÷%ö–CÖFV6—6–öâç&Wf–WvW%ö–BÀ¢&V6öãÖb$FF6WB7V6–f–6F–öâFV6—6–öã¢¶FV6—6–öâæFV6—6–öâçfÇVWÒâ"À¢'F–f7Eö†6†W3×G&ç6—F–öåö†6†W2À¢’À¢¢–b&÷fÂæ&÷fÅ÷G—RÓÒ&÷fÅG—RåE$”ä”äuôDD4UEô54TÔ$Å•õ5E$DTu’çfÇVS ¢F&vWEö'•öFV6—6–öâÒ°¢&÷fÄFV6—6–öåfÇVRä$õdS¢v÷&¶fÆ÷u7FFRä4ôÕÄUDTBÀ¢&÷fÄFV6—6–öåfÇVRä4„ôõ4UôÅDU$äD•dS¢v÷&¶fÆ÷u7FFRä4ôÕÄUDTBÀ¢&÷fÄFV6—6–öåfÇVRå$UTU5Eõ$Ud•4”ôã¢€¢v÷&¶fÆ÷u7FFRåÄää”äuô54TÔ$Å•õ5E$DTt”U0¢’À¢&÷fÄFV6—6–öåfÇVRå$T¤T5C¢v÷&¶fÆ÷u7FFRä4ä4TÄÄTBÀ¢&÷fÄFV6—6–öåfÇVRä4ä4TÅõtõ$´dÄõs¢v÷&¶fÆ÷u7FFRä4ä4TÄÄTBÀ¢Ð¢&WGW&â6VÆbåöÇ•÷G&ç6—F–öâ€¢6W76–öâÀ¢'V–ÆBÀ¢G&ç6—F–öå&WVW7B€¢F&vWE÷7FFS×F&vWEö'•öFV6—6–öå¶FV6—6–öâæFV6—6–öåÒÀ¢W‡V7FVE÷fW'6–öãÖ'V–ÆBçfW'6–öâÀ¢–FV×÷FVæ7•ö¶W“Öb'¶FV6—6–öâæ–FV×÷FVæ7•ö¶W—Ó§G&ç6—F–öâ"À¢–æ—F–F÷#Ô7F÷%G—Rä…TÔâÀ¢–æ—F–F÷%ö–CÖFV6—6–öâç&Wf–WvW%ö–BÀ¢&V6öãÖb$76VÖ&Ç’7G&FVw’FV6—6–öã¢¶FV6—6–öâæFV6—6–öâçfÇVWÒâ"À¢'F–f7Eö†6†W3ÖFV6—6–öâæ'F–f7Eö†6†W2À¢’À¢¢–b&÷fÂæ&÷fÅ÷G—RÓÒ&÷fÅG—Rå4T$4…õ$Ud•4”ôâçfÇVS ¢–bFV6—6–öâæFV6—6–öâæ÷B–â°¢&÷fÄFV6—6–öåfÇVRå$UTU5Eõ$Ud•4”ôâÀ¢&÷fÄFV6—6–öåfÇVRå$T¤T5BÀ¢&÷fÄFV6—6–öåfÇVRä4ä4TÅõtõ$´dÄõrÀ¢Ó ¢&—6R–çfÆ–EG&ç6—F–öâ€¢%6V&6‚&Wf–WrW&Ö—G2öæÇ’&Wf—6VB6V&6‚Â&V¦V7F–öâÂ÷"6æ6VÆÆF–öââ ¢¢F&vWBÒ€¢v÷&¶fÆ÷u7FFRäD•44õdU$”äuôDD¢–bFV6—6–öâæFV6—6–öâ—2&÷fÄFV6—6–öåfÇVRå$UTU5Eõ$Ud•4”ôà¢VÇ6Rv÷&¶fÆ÷u7FFRä4ä4TÄÄT@¢¢&WGW&â6VÆbåöÇ•÷G&ç6—F–öâ€¢6W76–öâÀ¢'V–ÆBÀ¢G&ç6—F–öå&WVW7B€¢F&vWE÷7FFS×F&vWBÀ¢W‡V7FVE÷fW'6–öãÖ'V–ÆBçfW'6–öâÀ¢–FV×÷FVæ7•ö¶W“Öb'¶FV6—6–öâæ–FV×÷FVæ7•ö¶W—Ó§G&ç6—F–öâ"À¢–æ—F–F÷#Ô7F÷%G—Rä…TÔâÀ¢–æ—F–F÷%ö–CÖFV6—6–öâç&Wf–WvW%ö–BÀ¢&V6öãÖb%6V&6‚&Wf–WrFV6—6–öã¢¶FV6—6–öâæFV6—6–öâçfÇVWÒâ"À¢'F–f7Eö†6†W3ÖFV6—6–öâæ'F–f7Eö†6†W2À¢’À¢¢–b&÷fÂæ&÷fÅ÷G—RÒ&÷fÅG—RäDD4UEõ4TÄT5D”ôâçfÇVS ¢&—6R–çfÆ–EG&ç6—F–öâ‚%F†—2&÷fÂG—RFöW2æ÷BGfæ6RF†R†6RÓv÷&¶fÆ÷râ"¢F&vWEö'•öFV6—6–öâÒ°¢&÷fÄFV6—6–öåfÇVRä$õdS¢v÷&¶fÆ÷u7FFRä5U$D”äuôDDÀ¢&÷fÄFV6—6–öåfÇVRä4„ôõ4UôÅDU$äD•dS¢v÷&¶fÆ÷u7FFRä5U$D”äuôDDÀ¢&÷fÄFV6—6–öåfÇVRå$UTU5Eõ$Ud•4”ôã¢v÷&¶fÆ÷u7FFRäD•44õdU$”äuôDDÀ¢&÷fÄFV6—6–öåfÇVRå$T¤T5C¢v÷&¶fÆ÷u7FFRä4ä4TÄÄTBÀ¢&÷fÄFV6—6–öåfÇVRä4ä4TÅõtõ$´dÄõs¢v÷&¶fÆ÷u7FFRä4ä4TÄÄTBÀ¢Ð¢&WGW&â6VÆbåöÇ•÷G&ç6—F–öâ€¢6W76–öâÀ¢'V–ÆBÀ¢G&ç6—F–öå&WVW7B€¢F&vWE÷7FFS×F&vWEö'•öFV6—6–öå¶FV6—6–öâæFV6—6–öåÒÀ¢W‡V7FVE÷fW'6–öãÖ'V–ÆBçfW'6–öâÀ¢–FV×÷FVæ7•ö¶W“Öb'¶FV6—6–öâæ–FV×÷FVæ7•ö¶W—Ó§G&ç6—F–öâ"À¢–æ—F–F÷#Ô7F÷%G—Rä…TÔâÀ¢–æ—F–F÷%ö–CÖFV6—6–öâç&Wf–WvW%ö–BÀ¢&V6öãÖb$FF6WB&÷fÂFV6—6–öã¢¶FV6—6–öâæFV6—6–öâçfÇVWÒâ"À¢'F–f7Eö†6†W3ÖFV6—6–öâæ'F–f7Eö†6†W2À¢’À¢ ¢FVb÷6æ6†÷B‡6VÆbÂ6W76–öã¢6W76–öâÂ&÷s¢VæGö–çD'V–ÆE&÷r’Óâv÷&¶fÆ÷u6æ6†÷C ¢VæF–ærÒ6W76–öâç66Æ"€¢6VÆV7B„&÷fÅ&÷ræ–B¢çv†W&R€¢&÷fÅ&÷rçv÷&¶fÆ÷uö–BÓÒ&÷ræ–BÀ¢&÷fÅ&÷rç7FGW2ÓÒ&÷fÅ7FGW2åTäD”ärçfÇVRÀ¢¢æ÷&FW%ö'’„&÷fÅ&÷ræ7&VFVEöBæFW62‚’¢æÆ–Ö—Bƒ¢¢7FFRÒv÷&¶fÆ÷u7FFR‡&÷ræ7W'&VçE÷7FvR¢&öw&W75÷7FFRÒ7FFP¢–b7FFR—2v÷&¶fÆ÷u7FFRåU4TBæB&÷rçW6VEög&öÕ÷7FFS ¢&öw&W75÷7FFRÒv÷&¶fÆ÷u7FFR‡&÷rçW6VEög&öÕ÷7FFR¢–b7FFR—2v÷&¶fÆ÷u7FFRäd”ÄTBæB&÷ræf–ÆVEög&öÕ÷7FFS ¢&öw&W75÷7FFRÒv÷&¶fÆ÷u7FFR‡&÷ræf–ÆVEög&öÕ÷7FFR¢&WGW&âv÷&¶fÆ÷u6æ6†÷B€¢–C×&÷ræ–BÀ¢VæGö–çEöæÖS×&÷ræVæGö–çEöæÖRÀ¢VæGö–çE÷6ÇVs×&÷ræVæGö–çE÷6ÇVrÀ¢&–öÆöv–6ÅövöÃ×&÷ræ&–öÆöv–6ÅövöÂÀ¢v÷&¶fÆ÷uö¶–æCÕv÷&¶fÆ÷t¶–æB‡&÷rçv÷&¶fÆ÷uö¶–æB’À¢&Væ6†Ö&µöÖöFS×&÷ræ&Væ6†Ö&µöÖöFRÀ¢7FFS×7FFRÀ¢7FGW3Õv÷&¶fÆ÷u7FGW2‡&÷rç7FGW2’À¢7W'&VçE÷7FvS×7FFRÀ¢fW'6–öã×&÷rçfW'6–öâÀ¢&öw&W73Õ5DDUõ$ôu$U55·&öw&W75÷7FFUÒÀ¢VæF–æuö&÷fÅö–C×VæF–ærÀ¢W6VEög&öÕ÷7FFSÒ€¢v÷&¶fÆ÷u7FFR‡&÷rçW6VEög&öÕ÷7FFR’–b&÷rçW6VEög&öÕ÷7FFRVÇ6RæöæP¢’À¢f–ÆVEög&öÕ÷7FFSÒ€¢v÷&¶fÆ÷u7FFR‡&÷ræf–ÆVEög&öÕ÷7FFR’–b&÷ræf–ÆVEög&öÕ÷7FFRVÇ6RæöæP¢’À¢7&VFVEö'“×&÷ræ7&VFVEö'’À¢7&VFVEöC×'6U÷WF2‡&÷ræ7&VFVEöB’À¢WFFVEöC×'6U÷WF2‡&÷rçWFFVEöB’À¢W6VEöC×'6U÷WF2‡&÷rçW6VEöB’À¢6æ6VÆÆVEöC×'6U÷WF2‡&÷ræ6æ6VÆÆVEöB’À¢6ö×ÆWFVEöC×'6U÷WF2‡&÷ræ6ö×ÆWFVEöB’À¢ ¢FVbö&÷fÅöF–7B‡6VÆbÂ&÷s¢&÷fÅ&÷r’ÓâF–7C ¢&WGW&â°¢'66†VÖ÷fW'6–öâ#¢44„TÔõdU%4”ôâÀ¢&–B#¢&÷ræ–BÀ¢'v÷&¶fÆ÷uö–B#¢&÷rçv÷&¶fÆ÷uö–BÀ¢'7FvR#¢&÷rç7FvRÀ¢&&÷fÅ÷G—R#¢&÷ræ&÷fÅ÷G—RÀ¢'7FGW2#¢&÷rç7FGW2À¢'&÷÷6Åö†6‚#¢&÷rç&÷÷6Åö†6‚À¢'&WVW7B#¢&÷fÅ&WVW7BæÖöFVÅ÷fÆ–FFUö§6öâ‡&÷rç&WVW7Eö§6öâ’æÖöFVÅöGV×€¢ÖöFSÒ&§6öâ ¢’À¢&FV6—6–öâ#¢€¢&÷fÄFV6—6–öâæÖöFVÅ÷fÆ–FFUö§6öâ‡&÷ræFV6—6–öåö§6öâ’æÖöFVÅöGV×†ÖöFSÒ&§6öâ"¢–b&÷ræFV6—6–öåö§6öà¢VÇ6RæöæP¢’À¢&7&VFVEöB#¢&÷ræ7&VFVEöBÀ¢&FV6–FVEöB#¢&÷ræFV6–FVEöBÀ¢'7WW'6VFW5ö–B#¢&÷rç7WW'6VFW5ö–BÀ¢Ð ¢7FF–6ÖWF†ö@¢FVb÷7FWöF–7B‡&÷s¢v÷&¶fÆ÷u7FW&÷r’ÓâF–7C ¢&WGW&â°¢'66†VÖ÷fW'6–öâ#¢44„TÔõdU%4”ôâÀ¢&–B#¢&÷ræ–BÀ¢'v÷&¶fÆ÷uö–B#¢&÷rçv÷&¶fÆ÷uö–BÀ¢'7FvR#¢&÷rç7FvRÀ¢&GFV×B#¢&÷ræGFV×BÀ¢'7FGW2#¢&÷rç7FGW2À¢&–FV×÷FVæ7•ö¶W’#¢&÷ræ–FV×÷FVæ7•ö¶W’À¢'7F'FVEöB#¢&÷rç7F'FVEöBÀ¢&6ö×ÆWFVEöB#¢&÷ræ6ö×ÆWFVEöBÀ¢&†V'F&VEöB#¢&÷ræ†V'F&VEöBÀ¢&–çWB#¢ÆöE÷fW'6–öæVEö§6öâ‡&÷ræ–çWEö§6öâ’À¢&÷WGWB#¢ÆöE÷fW'6–öæVEö§6öâ‡&÷ræ÷WGWEö§6öâ’–b&÷ræ÷WGWEö§6öâVÇ6RæöæRÀ¢&W'&÷%ö–B#¢&÷ræW'&÷%ö–BÀ¢Ð ¢7FF–6ÖWF†ö@¢FVbövVçE÷'VåöF–7B‡6W76–öã¢6W76–öâÂ&÷s¢vVçE'Vå&÷rÂ¢Â–æ6ÇVFU÷G&6S¢&ööÂ’ÓâF–7C ¢&WVW7E÷–ÆöBÒÆöE÷fW'6–öæVEö§6öâ‡&÷rç&WVW7Eö§6öâ¢6öçFW‡BÒ&WVW7E÷–ÆöBævWB‚&6öçFW‡B"¢'VåöÖöFRÒ6öçFW‡BævWB‚''VåöÖöFR"’–b—6–ç7Fæ6R†6öçFW‡BÂF–7B’VÇ6RæöæP¢–b'VåöÖöFRæ÷B–â²&Æ—fR"Â&66†VB"Â'&WÆ’'Ó ¢'VåöÖöFRÒæöæP¢FööÅö6ÆÇ2Ò6W76–öâç66Æ'2€¢6VÆV7B…FööÄ6ÆÅ&÷r¢çv†W&R…FööÄ6ÆÅ&÷rævVçE÷'Våö–BÓÒ&÷ræ–B¢æ÷&FW%ö'’…FööÄ6ÆÅ&÷ræ7&VFVEöBÂFööÄ6ÆÅ&÷ræ–B¢’æÆÂ‚¢&W7VÇBÒ°¢'66†VÖ÷fW'6–öâ#¢44„TÔõdU%4”ôâÀ¢&–B#¢&÷ræ–BÀ¢'v÷&¶fÆ÷uö–B#¢&÷rçv÷&¶fÆ÷uö–BÀ¢'7FWö–B#¢&÷rç7FWö–BÀ¢&vVçEöæÖR#¢&÷rævVçEöæÖRÀ¢&vVçE÷fW'6–öâ#¢&÷rævVçE÷fW'6–öâÀ¢'&÷f–FW"#¢&÷rç&÷f–FW"À¢&ÖöFVÅö–FVçF–f–W"#¢&÷ræÖöFVÅö–FVçF–f–W"À¢''VåöÖöFR#¢'VåöÖöFRÀ¢&–ç7G'V7F–öå÷fW'6–öâ#¢&÷ræ–ç7G'V7F–öå÷fW'6–öâÀ¢&–çWEö†6‚#¢&÷ræ–çWEö†6‚À¢'7FGW2#¢&÷rç7FGW2À¢'W6vR#¢ÆöE÷fW'6–öæVEö§6öâ‡&÷rçW6vUö§6öâ’À¢'GW&ç2#¢&÷rçGW&ç2À¢&GW&F–öåö×2#¢&÷ræGW&F–öåö×2À¢&7&VFVEöB#¢&÷ræ7&VFVEöBÀ¢&6ö×ÆWFVEöB#¢&÷ræ6ö×ÆWFVEöBÀ¢'FööÇ2#¢°¢°¢&–B#¢6ÆÂæ–BÀ¢'FööÅöæÖR#¢6ÆÂçFööÅöæÖRÀ¢'FööÅ÷fW'6–öâ#¢6ÆÂçFööÅ÷fW'6–öâÀ¢'7FGW2#¢6ÆÂç7FGW2À¢&GW&F–öåö×2#¢6ÆÂæGW&F–öåö×2À¢&7&VFVEöB#¢6ÆÂæ7&VFVEöBÀ¢Ð¢f÷"6ÆÂ–âFööÅö6ÆÇ0¢ÒÀ¢Ð¢–b–æ6ÇVFU÷G&6S ¢&W7VÇBçWFFR€¢&WVW7C×&WVW7E÷–ÆöBÀ¢&W7VÇCÖÆöE÷fW'6–öæVEö§6öâ‡&÷rç&W7VÇEö§6öâ’–b&÷rç&W7VÇEö§6öâVÇ6RæöæRÀ¢G&6SÖÆöE÷fW'6–öæVEö§6öâ‡&÷rçG&6Uö§6öâ’À¢FööÅö6ÆÇ3Õ°¢°¢'66†VÖ÷fW'6–öâ#¢44„TÔõdU%4”ôâÀ¢&–B#¢6ÆÂæ–BÀ¢'FööÅöæÖR#¢6ÆÂçFööÅöæÖRÀ¢'FööÅ÷fW'6–öâ#¢6ÆÂçFööÅ÷fW'6–öâÀ¢'7FGW2#¢6ÆÂç7FGW2À¢'W&Ö—76–öå÷66÷R#¢ÆöE÷fW'6–öæVEö§6öâ†6ÆÂçW&Ö—76–öå÷66÷Uö§6öâ’À¢&&wVÖVçG2#¢ÆöE÷fW'6–öæVEö§6öâ†6ÆÂæ&wVÖVçG5ö§6öâ’À¢'&W7VÇB#¢ÆöE÷fW'6–öæVEö§6öâ†6ÆÂç&W7VÇEö§6öâ¢–b6ÆÂç&W7VÇEö§6öà¢VÇ6RæöæRÀ¢&GW&F–öåö×2#¢6ÆÂæGW&F–öåö×2À¢Ð¢f÷"6ÆÂ–âFööÅö6ÆÇ0¢ÒÀ¢¢&WGW&â&W7VÇ@ ¢7FF–6ÖWF†ö@¢FVb÷7FGW5öf÷"‡7FFS¢v÷&¶fÆ÷u7FFR’Óâv÷&¶fÆ÷u7FGW3 ¢–b7FFR—2v÷&¶fÆ÷u7FFRäE$eC ¢&WGW&âv÷&¶fÆ÷u7FGW2äE$e@¢–b7FFR–â°¢v÷&¶fÆ÷u7FFRät•D”äuôDD4UEõ5T4”d”4D”ôåô$õdÂÀ¢v÷&¶fÆ÷u7FFRät•D”äuôDD4UEõ5T4”d”4D”ôåõ$Ud•4”ôâÀ¢v÷&¶fÆ÷u7FFRät•D”äuôDD4UEô$õdÂÀ¢v÷&¶fÆ÷u7FFRät•D”äuõ4T$4…õ$Ud”UrÀ¢v÷&¶fÆ÷u7FFRät•D”äuôÄ$TÅô$õdÂÀ¢v÷&¶fÆ÷u7FFRät•D”äuõE$”ä”äuô$õdÂÀ¢v÷&¶fÆ÷u7FFRät•D”äuõ44”TåD”d”5ô$õdÂÀ¢Ó ¢&WGW&âv÷&¶fÆ÷u7FGW2åt•D”äp¢–b7FFR—2v÷&¶fÆ÷u7FFRåU4TC ¢&WGW&âv÷&¶fÆ÷u7FGW2åU4T@¢–b7FFR—2v÷&¶fÆ÷u7FFRäd”ÄTC ¢&WGW&âv÷&¶fÆ÷u7FGW2äd”ÄT@¢–b7FFR—2v÷&¶fÆ÷u7FFRä4ä4TÄÄTC ¢&WGW&âv÷&¶fÆ÷u7FGW2ä4ä4TÄÄT@¢–b7FFR—2v÷&¶fÆ÷u7FFRä4ôÕÄUDTC ¢&WGW&âv÷&¶fÆ÷u7FGW2ä4ôÕÄUDT@¢&WGW&âv÷&¶fÆ÷u7FGW2ä5D•dP ¢FVbö7&VFU÷7FWö–å÷6W76–öâ€¢6VÆbÀ¢6W76–öã¢6W76–öâÀ¢'V–ÆC¢VæGö–çD'V–ÆE&÷rÀ¢7FvS¢v÷&¶fÆ÷u7FFRÀ¢¢À¢–FV×÷FVæ7•ö¶W“¢7G"À¢–çWE÷–ÆöC¢F–7BÀ¢’Óâv÷&¶fÆ÷u7FW&÷s ¢GFV×BÒ€¢–çB€¢6W76–öâç66Æ"€¢6VÆV7B†gVæ2æ6öÆW66R†gVæ2æÖ‚…v÷&¶fÆ÷u7FW&÷ræGFV×B’Â’’çv†W&R€¢v÷&¶fÆ÷u7FW&÷rçv÷&¶fÆ÷uö–BÓÒ'V–ÆBæ–BÀ¢v÷&¶fÆ÷u7FW&÷rç7FvRÓÒ7FvRçfÇVRÀ¢¢¢÷" ¢¢²¢¢æ÷rÒWF5÷FW‡B‚¢7F—fU÷7FW2Ò6W76–öâç66Æ'2€¢6VÆV7B…v÷&¶fÆ÷u7FW&÷r’çv†W&R€¢v÷&¶fÆ÷u7FW&÷rçv÷&¶fÆ÷uö–BÓÒ'V–ÆBæ–BÀ¢v÷&¶fÆ÷u7FW&÷rç7FvRÓÒ7FvRçfÇVRÀ¢v÷&¶fÆ÷u7FW&÷rç7FGW2ÓÒ7FW7FGW2å%Tää”ärçfÇVRÀ¢¢’æÆÂ‚¢f÷"7F—fR–â7F—fU÷7FW3 ¢W'&÷%ö–BÒFWFW&Ö–æ—7F–5ö–B‚&W'""Â7F—fRæ–BÂ'7WW'6VFVB"Â7G"†GFV×B’¢6W76–öâæFB€¢v÷&¶fÆ÷tW'&÷%&÷r€¢–CÖW'&÷%ö–BÀ¢v÷&¶fÆ÷uö–CÖ'V–ÆBæ–BÀ¢7FWö–CÖ7F—fRæ–BÀ¢vVçE÷'Våö–CÔæöæRÀ¢FööÅö6ÆÅö–CÔæöæRÀ¢6öFSÒ'7FWöGFV×E÷7WW'6VFVB"À¢6FVv÷'“Ò&6öç6—7FVæ7’"À¢&WG'–&ÆSÓÀ¢6fUöÖW76vSÒ$âöÆFW"'Vææ–ærGFV×Bv27WW'6VFVB'’æWvW"GFV×Bâ"À¢FWF–Åö§6öãÖ6æöæ–6Åö§6öâ€¢fW'6–öæVE÷–ÆöB€¢7FvS×7FvRçfÇVRÀ¢öÆEöGFV×CÖ7F—fRæGFV×BÀ¢æWuöGFV×CÖGFV×BÀ¢¢’À¢7&VFVEöCÖæ÷rÀ¢¢¢7F—fRç7FGW2Ò7FW7FGW2ä”åDU%%UDTBçfÇVP¢7F—fRæW'&÷%ö–BÒW'&÷%ö–@¢7F—fRæ6ö×ÆWFVEöBÒæ÷p¢VæEöWfVçB€¢6W76–öâÀ¢'V–ÆBÀ¢WfVçE÷G—SÒ'v÷&¶fÆ÷rç7FWç7WW'6VFVB"À¢7F÷%÷G—SÔ7F÷%G—Räõ$4„U5E$Dõ"çfÇVRÀ¢7F÷%ö–CÒ'v÷&¶fÆ÷r×6W'f–6R"À¢–FV×÷FVæ7•ö¶W“Öb'7FW×7WW'6VFVC§¶7F—fRæ–GÓ§¶GFV×GÒ"À¢–ÆöC×°¢'7FWö–B#¢7F—fRæ–BÀ¢'7FvR#¢7FvRçfÇVRÀ¢&öÆEöGFV×B#¢7F—fRæGFV×BÀ¢&æWuöGFV×B#¢GFV×BÀ¢ÒÀ¢g&öÕ÷7FFSÖ'V–ÆBæ7W'&VçE÷7FvRÀ¢Fõ÷7FFSÖ'V–ÆBæ7W'&VçE÷7FvRÀ¢¢7FWÒv÷&¶fÆ÷u7FW&÷r€¢–CÖFWFW&Ö–æ—7F–5ö–B‚'7FW"Â'V–ÆBæ–BÂ7FvRçfÇVRÂ7G"†GFV×B’’À¢v÷&¶fÆ÷uö–CÖ'V–ÆBæ–BÀ¢7FvS×7FvRçfÇVRÀ¢GFV×CÖGFV×BÀ¢7FGW3Õ7FW7FGW2å%Tää”ärçfÇVRÀ¢–FV×÷FVæ7•ö¶W“Ö–FV×÷FVæ7•ö¶W’À¢7F'FVEöCÖæ÷rÀ¢6ö×ÆWFVEöCÔæöæRÀ¢†V'F&VEöCÖæ÷rÀ¢–çWEö§6öãÖ6æöæ–6Åö§6öâ‡fW'6–öæVE÷–ÆöB‚¢¦–çWE÷–ÆöB’’À¢÷WGWEö§6öãÔæöæRÀ¢W'&÷%ö–CÔæöæRÀ¢¢6W76–öâæFB‡7FW¢6W76–öâæfÇW6‚‚¢VæEöWfVçB€¢6W76–öâÀ¢'V–ÆBÀ¢WfVçE÷G—SÒ'v÷&¶fÆ÷rç7FWç7F'FVB"À¢7F÷%÷G—SÔ7F÷%G—Räõ$4„U5E$Dõ"çfÇVRÀ¢7F÷%ö–CÒ'v÷&¶fÆ÷r×6W'f–6R"À¢–FV×÷FVæ7•ö¶W“Öb'¶–FV×÷FVæ7•ö¶W—Ó§7F'FVB"À¢–ÆöC×²'7FWö–B#¢7FWæ–BÂ'7FvR#¢7FvRçfÇVRÂ&GFV×B#¢GFV×GÒÀ¢g&öÕ÷7FFSÖ'V–ÆBæ7W'&VçE÷7FvRÀ¢Fõ÷7FFSÖ'V–ÆBæ7W'&VçE÷7FvRÀ¢¢&WGW&â7FW 