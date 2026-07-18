"""Transactional workflow service. It owns workflow truth but no scientific logic."""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .artifacts import LocalArtifactStore
from .config import AgentConfiguration
from .contracts import (
    SCHEMA_VERSION,
    ActorType,
    ApprovalDecision,
    ApprovalDecisionValue,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalType,
    EndpointBuildCreate,
    StepStatus,
    TransitionRequest,
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

STATE_PROGRESS = {
    WorkflowState.DRAFT: 0,
    WorkflowState.DISCOVERING_DATA: 8,
    WorkflowState.AWAITING_DATASET_APPROVAL: 15,
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
    "dataset_selection_approval": ApprovalType.DATASET_SELECTION,
    "label_rules_approval": ApprovalType.LABEL_RULES,
    "training_approval": ApprovalType.TRAINING_AUTHORIZATION,
    "model_acceptance_approval": ApprovalType.MODEL_ACCEPTANCE,
    "registration_approval": ApprovalType.REGISTRY_PUBLICATION,
    "identity_conflict_decisions": ApprovalType.IDENTITY_CONFLICT,
}


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
                requested_action="Start bounded prepared discovery simulation.",
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

    def start_build(
        self,
        workflow_id: str,
        *,
        expected_version: int,
        actor: str,
        idempotency_key: str,
    ) -> WorkflowSnapshot:
        if self.harness is None:
            raise WorkflowConflict("No Phase-0 agent harness is configured.")
        artifacts = self.artifact_store.list_artifacts(workflow_id)
        definition = next(
            (item for item in artifacts if item.artifact_type == "endpoint_definition"), None
        )
        if definition is None:
            raise GuardNotSatisfied("Endpoint definition artifact is missing.")
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
            with self.database.session() as session:
                stored_step = session.get(WorkflowStepRow, step.id)
                if stored_step and stored_step.status == StepStatus.RUNNING.value:
                    retryable = bool(result.error and result.error.retryable)
                    stored_step.status = (
                        StepStatus.FAILED_RETRYABLE.value if retryable else StepStatus.FAILED.value
                    )
                    if result.error:
                        stored_step.error_id = deterministic_id("err", run_id, result.error.code)
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
        self.transition(
            workflow_id,
            TransitionRequest(
                target_state=WorkflowState.AWAITING_DATASET_APPROVAL,
                expected_version=expected_version,
                idempotency_key=f"{idempotency_key}:awaiting-approval",
                initiator=ActorType.ORCHESTRATOR,
                initiator_id="workflow-service",
                reason=f"{run_mode.title()} candidates are ready for human dataset review.",
                artifact_hashes=[
                    candidate_artifact.sha256,
                    strategy_artifact.sha256,
                    recommendation_artifact.sha256,
                    trace_artifact.sha256,
                ],
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
            attempt = (
                int(
                    session.scalar(
                        select(func.coalesce(func.max(WorkflowStepRow.attempt), 0)).where(
                            WorkflowStepRow.workflow_id == workflow_id,
                            WorkflowStepRow.stage == stage.value,
                        )
                    )
                    or 0
                )
                + 1
            )
            now = utc_text()
            step = WorkflowStepRow(
                id=deterministic_id("step", workflow_id, stage.value, str(attempt)),
                workflow_id=workflow_id,
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
            stage = WorkflowState(build.current_stage)
            completed = session.scalar(
                select(WorkflowStepRow).where(
                    WorkflowStepRow.workflow_id == workflow_id,
                    WorkflowStepRow.stage == stage.value,
                    WorkflowStepRow.status == StepStatus.COMPLETED.value,
                )
            )
            if completed is None:
                self._create_step_in_session(
                    session,
                    build,
                    stage,
                    idempotency_key=f"{idempotency_key}:attempt",
                    input_payload={"retry_of_error_id": retryable.id},
                )
            return snapshot

    def recover_interrupted(self) -> int:
        recovered = 0
        with self.database.session() as session:
            steps = session.scalars(
                select(WorkflowStepRow).where(WorkflowStepRow.status == StepStatus.RUNNING.value)
            ).all()
            for step in steps:
                build = require_build(session, step.workflow_id)
                original = build.current_stage
                error_id = deterministic_id("err", build.id, step.id, "startup-recovery")
                error = WorkflowErrorRow(
                    id=error_id,
                    workflow_id=build.id,
                    step_id=step.id,
                    agent_run_id=None,
                    tool_call_id=None,
                    code="interrupted_by_restart",
                    category="recovery",
                    retryable=1,
                    safe_message="Running step was interrupted by backend restart.",
                    detail_json=canonical_json(versioned_payload(previous_stage=original)),
                    created_at=utc_text(),
                )
                session.add(error)
                step.status = StepStatus.FAILED_RETRYABLE.value
                step.error_id = error_id
                step.completed_at = utc_text()
                build.failed_from_state = original
                build.current_stage = WorkflowState.FAILED.value
                build.status = WorkflowStatus.FAILED.value
                build.updated_at = utc_text()
                build.version += 1
                append_event(
                    session,
                    build,
                    event_type="workflow.recovered_interruption",
                    actor_type=ActorType.SYSTEM.value,
                    actor_id="startup-recovery",
                    idempotency_key=f"startup-recovery:{step.id}",
                    payload={"step_id": step.id, "failed_from_state": original},
                    from_state=original,
                    to_state=WorkflowState.FAILED.value,
                )
                recovered += 1
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
        if approval.approval_type != ApprovalType.DATASET_SELECTION.value:
            raise InvalidTransition("Phase 0 advances only dataset-selection approvals.")
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
                request=load_versioned_json(row.request_json),
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
            WorkflowState.AWAITING_DATASET_APPROVAL,
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
