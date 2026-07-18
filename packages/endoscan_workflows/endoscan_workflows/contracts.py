"""Strict, versioned contracts crossing the Phase-0 workflow boundary."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "1.0.0"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,127}$")


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal["1.0.0"] = SCHEMA_VERSION


class WorkflowState(str, Enum):
    DRAFT = "DRAFT"
    DISCOVERING_DATA = "DISCOVERING_DATA"
    AWAITING_DATASET_APPROVAL = "AWAITING_DATASET_APPROVAL"
    CURATING_DATA = "CURATING_DATA"
    AWAITING_LABEL_APPROVAL = "AWAITING_LABEL_APPROVAL"
    RESOLVING_IDENTITIES = "RESOLVING_IDENTITIES"
    AUDITING_DATASET = "AUDITING_DATASET"
    AUDITING_LEAKAGE = "AUDITING_LEAKAGE"
    AWAITING_TRAINING_APPROVAL = "AWAITING_TRAINING_APPROVAL"
    TRAINING = "TRAINING"
    EVALUATING = "EVALUATING"
    AWAITING_SCIENTIFIC_APPROVAL = "AWAITING_SCIENTIFIC_APPROVAL"
    REGISTERING = "REGISTERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"


class WorkflowStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    WAITING = "waiting"
    PAUSED = "paused"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    FAILED_RETRYABLE = "failed_retryable"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class ActorType(str, Enum):
    HUMAN = "human"
    ORCHESTRATOR = "orchestrator"
    AGENT = "agent"
    TOOL = "tool"
    SYSTEM = "system"


class ApprovalType(str, Enum):
    ENDPOINT_DEFINITION = "endpoint_definition"
    DATASET_SELECTION = "dataset_selection"
    LABEL_RULES = "label_rules"
    IDENTITY_CONFLICT = "identity_conflict"
    TRAINING_AUTHORIZATION = "training_authorization"
    MODEL_ACCEPTANCE = "model_acceptance"
    REGISTRY_PUBLICATION = "registry_publication"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVISION_REQUESTED = "revision_requested"
    ALTERNATIVE_SELECTED = "alternative_selected"
    CANCELLED = "cancelled"


class ApprovalDecisionValue(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    REQUEST_REVISION = "request_revision"
    CHOOSE_ALTERNATIVE = "choose_alternative"
    CANCEL_WORKFLOW = "cancel_workflow"


class AgentRunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    APPROVAL_REQUIRED = "approval_required"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BUDGET_EXCEEDED = "budget_exceeded"


class ToolCallStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PROHIBITED = "prohibited"
    TIMED_OUT = "timed_out"


class EndpointBuildCreate(StrictContract):
    endpoint_name: str = Field(min_length=3, max_length=120)
    endpoint_slug: str = Field(min_length=3, max_length=80)
    biological_goal: str = Field(min_length=10, max_length=4000)
    created_by: str = Field(min_length=2, max_length=120)
    idempotency_key: str = Field(min_length=8, max_length=160)

    @field_validator("endpoint_slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        if not ID_PATTERN.fullmatch(value):
            raise ValueError("endpoint_slug must contain lowercase letters, digits, and hyphens")
        return value


class WorkflowSnapshot(StrictContract):
    id: str
    endpoint_name: str
    endpoint_slug: str
    biological_goal: str
    state: WorkflowState
    status: WorkflowStatus
    current_stage: WorkflowState
    version: int = Field(ge=0)
    progress: int = Field(ge=0, le=100)
    pending_approval_id: str | None = None
    paused_from_state: WorkflowState | None = None
    failed_from_state: WorkflowState | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime
    paused_at: datetime | None = None
    cancelled_at: datetime | None = None
    completed_at: datetime | None = None


class TransitionRequest(StrictContract):
    target_state: WorkflowState
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=160)
    initiator: ActorType
    initiator_id: str = Field(min_length=2, max_length=120)
    reason: str = Field(default="", max_length=2000)
    artifact_hashes: list[str] = Field(default_factory=list, max_length=100)


class ApprovalRequest(StrictContract):
    workflow_id: str
    stage: WorkflowState
    approval_type: ApprovalType
    proposed_decision: str = Field(min_length=1, max_length=4000)
    evidence_summary: str = Field(min_length=1, max_length=8000)
    source_references: list[str] = Field(default_factory=list, max_length=100)
    limitations: list[str] = Field(default_factory=list, max_length=100)
    artifact_hashes: list[str] = Field(min_length=1, max_length=100)
    agent_recommendation: str = Field(default="", max_length=4000)
    requested_action: str = Field(min_length=1, max_length=1000)


class ApprovalDecision(StrictContract):
    decision: ApprovalDecisionValue
    reviewer_id: str = Field(min_length=2, max_length=120)
    reviewer_comment: str = Field(default="", max_length=4000)
    selected_alternative_id: str | None = Field(default=None, max_length=160)
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=160)
    artifact_hashes: list[str] = Field(min_length=1, max_length=100)


class ArtifactDescriptor(StrictContract):
    id: str
    workflow_id: str
    step_id: str | None = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    mime_type: str = Field(min_length=3, max_length=160)
    artifact_type: str = Field(min_length=2, max_length=120)
    logical_name: str = Field(min_length=1, max_length=160)
    producer: str = Field(min_length=1, max_length=160)
    original_source: str | None = Field(default=None, max_length=1000)
    created_at: datetime


class ModelConfiguration(StrictContract):
    provider: str = Field(min_length=1, max_length=80)
    model_identifier: str = Field(min_length=1, max_length=160)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


class AgentBudget(StrictContract):
    maximum_turns: int = Field(default=8, ge=1, le=32)
    maximum_tool_calls: int = Field(default=12, ge=0, le=64)
    timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    maximum_input_tokens: int = Field(default=30_000, ge=1)
    maximum_output_tokens: int = Field(default=6_000, ge=1)
    maximum_cost_cents: float = Field(default=100.0, ge=0)
    retry_count: int = Field(default=2, ge=0, le=5)


class AgentRunRequest(StrictContract):
    workflow_id: str
    step_id: str
    agent_name: str = Field(min_length=2, max_length=120)
    agent_version: str = Field(min_length=1, max_length=40)
    instruction_version: str = Field(min_length=1, max_length=40)
    instructions: str = Field(min_length=1, max_length=20_000)
    model: ModelConfiguration
    output_schema_name: str = Field(min_length=1, max_length=160)
    available_tools: list[str] = Field(default_factory=list, max_length=64)
    context: dict[str, Any] = Field(default_factory=dict)
    budget: AgentBudget = Field(default_factory=AgentBudget)


class UsageReport(StrictContract):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    cost_cents: float = Field(default=0.0, ge=0)
    provider_request_ids: list[str] = Field(default_factory=list)


class NormalizedAgentError(StrictContract):
    code: str = Field(min_length=1, max_length=120)
    safe_message: str = Field(min_length=1, max_length=2000)
    retryable: bool = False
    category: str = Field(default="provider", max_length=80)


class ProviderPreflightResult(StrictContract):
    provider: str = Field(min_length=1, max_length=80)
    configured_model: str = Field(min_length=1, max_length=160)
    api_key_present: bool
    authentication_accepted: bool
    model_accessible: bool
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_error_code: str | None = Field(default=None, max_length=200)
    provider_error_type: str | None = Field(default=None, max_length=200)
    request_id: str | None = Field(default=None, max_length=200)
    billing_status: Literal["not_checked"] = "not_checked"
    generation_capability: Literal["not_checked"] = "not_checked"
    checked_at: datetime = Field(default_factory=utc_now)


class TraceEvent(StrictContract):
    sequence: int = Field(ge=0)
    event_type: str = Field(min_length=1, max_length=120)
    timestamp: datetime = Field(default_factory=utc_now)
    workflow_id: str
    step_id: str | None = None
    agent_run_id: str | None = None
    tool_call_id: str | None = None
    status: str = Field(default="", max_length=80)
    duration_ms: int | None = Field(default=None, ge=0)
    detail: dict[str, Any] = Field(default_factory=dict)


class AgentRunResult(StrictContract):
    status: AgentRunStatus
    output: dict[str, Any] | None = None
    approval_proposal: ApprovalRequest | None = None
    usage: UsageReport = Field(default_factory=UsageReport)
    trace: list[TraceEvent] = Field(default_factory=list)
    error: NormalizedAgentError | None = None
    turns: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)


class SideEffectClassification(str, Enum):
    NONE = "none"
    ARTIFACT_WRITE = "artifact_write"
    EXTERNAL_READ = "external_read"
    PRODUCTION_WRITE = "production_write"


class IdempotencyClassification(str, Enum):
    IDEMPOTENT = "idempotent"
    IDEMPOTENT_WITH_KEY = "idempotent_with_key"
    NON_IDEMPOTENT = "non_idempotent"


class ToolDefinition(StrictContract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,79}$")
    description: str = Field(min_length=5, max_length=1000)
    input_schema_name: str
    output_schema_name: str
    required_permissions: list[str] = Field(default_factory=list)
    side_effect: SideEffectClassification
    idempotency: IdempotencyClassification
    timeout_seconds: float = Field(gt=0, le=120)
    allowed_workflow_stages: list[WorkflowState]
    implementation_version: str = Field(min_length=1, max_length=40)


class ToolInvocation(StrictContract):
    tool_name: str
    arguments: dict[str, Any]
    workflow_id: str | None = None
    step_id: str | None = None
    workflow_stage: WorkflowState
    permission_scope: list[str] = Field(default_factory=list)
    run_context: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=160)


class ToolResult(StrictContract):
    tool_name: str
    status: ToolCallStatus
    output: dict[str, Any] | None = None
    error: NormalizedAgentError | None = None
    duration_ms: int = Field(default=0, ge=0)
    replayed: bool = False


class ProviderToolRequest(StrictContract):
    tool_name: str
    arguments: dict[str, Any]
    idempotency_key: str | None = None


class ProviderTurn(StrictContract):
    kind: Literal["output", "tool", "approval", "error"]
    output: dict[str, Any] | None = None
    tool_request: ProviderToolRequest | None = None
    approval: ApprovalRequest | None = None
    usage: UsageReport = Field(default_factory=UsageReport)
    error: NormalizedAgentError | None = None
