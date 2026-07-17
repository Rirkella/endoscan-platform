from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import inspect, select, text, update
from sqlalchemy.exc import DatabaseError

from endoscan_workflows.contracts import (
    ApprovalDecision,
    ApprovalDecisionValue,
    EndpointBuildCreate,
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
from endoscan_workflows.models import EndpointBuildRow, WorkflowEventRow
from endoscan_workflows.repository import load_versioned_json


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
        "alembic_version",
    }.issubset(tables)
    assert database.capability() == {
        "available": True,
        "journal_mode": "wal",
        "foreign_keys": True,
    }
    database.dispose()


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
    assert service.steps(curating.id)[-1]["status"] == "failed_retryable"
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
