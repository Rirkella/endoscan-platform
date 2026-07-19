"""SQLAlchemy persistence models. Structured JSON is always versioned and validated on read."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class EndpointBuildRow(Base):
    __tablename__ = "endpoint_builds"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    endpoint_name: Mapped[str] = mapped_column(String(120))
    endpoint_slug: Mapped[str] = mapped_column(String(80), index=True)
    biological_goal: Mapped[str] = mapped_column(Text)
    workflow_kind: Mapped[str] = mapped_column(
        String(80), default="legacy_single_source_discovery", index=True
    )
    benchmark_mode: Mapped[str] = mapped_column(String(120), default="none", index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    current_stage: Mapped[str] = mapped_column(String(64), index=True)
    paused_from_state: Mapped[str | None] = mapped_column(String(64))
    failed_from_state: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40))
    paused_at: Mapped[str | None] = mapped_column(String(40))
    cancelled_at: Mapped[str | None] = mapped_column(String(40))
    completed_at: Mapped[str | None] = mapped_column(String(40))
    payload_json: Mapped[str] = mapped_column(Text)


class WorkflowStepRow(Base):
    __tablename__ = "workflow_steps"
    __table_args__ = (
        UniqueConstraint("workflow_id", "stage", "attempt", name="uq_step_attempt"),
        UniqueConstraint("workflow_id", "idempotency_key", name="uq_step_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("endpoint_builds.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(64), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    started_at: Mapped[str | None] = mapped_column(String(40))
    completed_at: Mapped[str | None] = mapped_column(String(40))
    heartbeat_at: Mapped[str | None] = mapped_column(String(40))
    input_json: Mapped[str] = mapped_column(Text)
    output_json: Mapped[str | None] = mapped_column(Text)
    error_id: Mapped[str | None] = mapped_column(String(128))


class WorkflowEventRow(Base):
    __tablename__ = "workflow_events"
    __table_args__ = (
        UniqueConstraint("workflow_id", "sequence", name="uq_event_sequence"),
        UniqueConstraint("workflow_id", "idempotency_key", name="uq_event_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("endpoint_builds.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(120), index=True)
    actor_type: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str] = mapped_column(String(120))
    from_state: Mapped[str | None] = mapped_column(String(64))
    to_state: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    payload_json: Mapped[str] = mapped_column(Text)
    previous_event_hash: Mapped[str | None] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(40))


class ApprovalRow(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("endpoint_builds.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(64))
    approval_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    proposal_hash: Mapped[str] = mapped_column(String(64))
    request_json: Mapped[str] = mapped_column(Text)
    decision_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40))
    decided_at: Mapped[str | None] = mapped_column(String(40))
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("approvals.id"))


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("workflow_id", "logical_name", "sha256"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("endpoint_builds.id", ondelete="CASCADE"), index=True
    )
    step_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_steps.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    mime_type: Mapped[str] = mapped_column(String(160))
    artifact_type: Mapped[str] = mapped_column(String(120), index=True)
    logical_name: Mapped[str] = mapped_column(String(160))
    storage_key: Mapped[str] = mapped_column(String(200))
    producer: Mapped[str] = mapped_column(String(160))
    original_source: Mapped[str | None] = mapped_column(String(1000))
    metadata_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40))


class AgentRunRow(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("endpoint_builds.id", ondelete="CASCADE"), index=True
    )
    step_id: Mapped[str] = mapped_column(ForeignKey("workflow_steps.id"), index=True)
    agent_name: Mapped[str] = mapped_column(String(120))
    agent_version: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(80))
    model_identifier: Mapped[str] = mapped_column(String(160))
    instruction_version: Mapped[str] = mapped_column(String(40))
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(40), index=True)
    request_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    trace_json: Mapped[str] = mapped_column(Text)
    usage_json: Mapped[str] = mapped_column(Text)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40))
    completed_at: Mapped[str | None] = mapped_column(String(40))


class ToolCallRow(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("agent_run_id", "idempotency_key"),
        UniqueConstraint(
            "workflow_id", "tool_name", "idempotency_key", name="uq_tool_logical_call"
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("endpoint_builds.id", ondelete="CASCADE"), index=True
    )
    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(80), index=True)
    tool_version: Mapped[str] = mapped_column(String(40))
    permission_scope_json: Mapped[str] = mapped_column(Text)
    arguments_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40))
    completed_at: Mapped[str | None] = mapped_column(String(40))


class HumanDecisionRow(Base):
    __tablename__ = "human_decisions"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("endpoint_builds.id", ondelete="CASCADE"), index=True
    )
    approval_id: Mapped[str] = mapped_column(ForeignKey("approvals.id"), index=True)
    reviewer_id: Mapped[str] = mapped_column(String(120))
    decision: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40))


class WorkflowErrorRow(Base):
    __tablename__ = "workflow_errors"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("endpoint_builds.id", ondelete="CASCADE"), index=True
    )
    step_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_steps.id"))
    agent_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))
    tool_call_id: Mapped[str | None] = mapped_column(ForeignKey("tool_calls.id"))
    code: Mapped[str] = mapped_column(String(120), index=True)
    category: Mapped[str] = mapped_column(String(80))
    retryable: Mapped[int] = mapped_column(Integer, default=0)
    safe_message: Mapped[str] = mapped_column(Text)
    detail_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40))


class SourceCacheRow(Base):
    __tablename__ = "source_response_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    tool_name: Mapped[str] = mapped_column(String(80), index=True)
    normalized_arguments_json: Mapped[str] = mapped_column(Text)
    policy_version: Mapped[str] = mapped_column(String(40))
    source_version: Mapped[str | None] = mapped_column(String(120))
    source_url: Mapped[str] = mapped_column(String(1000))
    http_metadata_json: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    parsed_output_json: Mapped[str] = mapped_column(Text)
    raw_artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), index=True)
    retrieved_at: Mapped[str] = mapped_column(String(40))
    expires_at: Mapped[str] = mapped_column(String(40), index=True)


class TrainingDatasetWorkflowRow(Base):
    """Mutable latest-document pointers; every durable decision remains an artifact/event."""

    __tablename__ = "training_dataset_workflows"

    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("endpoint_builds.id", ondelete="CASCADE"), primary_key=True
    )
    contract_version: Mapped[str] = mapped_column(String(40), default="1.0.0")
    benchmark_mode: Mapped[str] = mapped_column(String(120), index=True)
    initial_context_json: Mapped[str] = mapped_column(Text)
    specification_draft_json: Mapped[str | None] = mapped_column(Text)
    specification_outcome_json: Mapped[str | None] = mapped_column(Text)
    specification_semantic_validation_json: Mapped[str | None] = mapped_column(Text)
    specification_json: Mapped[str | None] = mapped_column(Text)
    component_requirements_json: Mapped[str | None] = mapped_column(Text)
    source_inventory_json: Mapped[str | None] = mapped_column(Text)
    capability_matrix_json: Mapped[str | None] = mapped_column(Text)
    assembly_strategies_json: Mapped[str | None] = mapped_column(Text)
    joinability_diagnostics_json: Mapped[str | None] = mapped_column(Text)
    gap_report_json: Mapped[str | None] = mapped_column(Text)
    preparation_plan_json: Mapped[str | None] = mapped_column(Text)
    assembly_review_json: Mapped[str | None] = mapped_column(Text)
    discovery_round: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40))


Index("ix_build_status_updated", EndpointBuildRow.status, EndpointBuildRow.updated_at)
