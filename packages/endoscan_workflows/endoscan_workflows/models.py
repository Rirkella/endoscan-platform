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
    …124438 tokens truncated…bounded_smoke",
                maximum_hydration_candidates=10,
            ),
            _invocation(workflow_id, provider_name),
        )
        assert replay.cache_only_replay
        assert not replay.normalization_ran
        assert replay.dataset_manifest.bundle_fingerprint == cold_fingerprints[provider_name]
    assert transport.calls == cold_calls


def test_registered_capabilities_match_live_validated_readiness() -> None:
    registry = load_operational_capability_registry(REPO_ROOT)
    expected = {
        "lincs-l1000",
        "toxcast",
        "tox21",
        "pubchem-bioassay",
        "pubchem-compound",
        "ncbi-geo",
        "ncbi-supporting-metadata",
    }
    assert expected <= {item.provider for item in registry.providers}
    for provider in expected:
        profile: ProviderOperationalProfile = registry.by_provider(provider)
        for declaration in profile.capabilities:
            if declaration.capability is OperationalCapability.HEAVY_EXPRESSION_EXTRACTION:
                assert not declaration.satisfies_preapproval_requirement
            else:
                assert declaration.satisfies_preapproval_requirement, (
                    provider,
                    declaration.capability,
                )
