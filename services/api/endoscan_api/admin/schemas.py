"""Thin HTTP-only admin request shapes."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from endoscan_workflows.contracts import ApprovalDecisionValue, WorkflowKind
from endoscan_workflows.training_dataset import EndpointDiscoveryScope


class AdminRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateBuildBody(AdminRequest):
    endpoint_name: str = Field(min_length=3, max_length=120)
    endpoint_slug: str = Field(min_length=3, max_length=80)
    biological_goal: str = Field(min_length=10, max_length=4000)
    created_by: str = Field(default="local-admin", min_length=2, max_length=120)
    workflow_kind: WorkflowKind = WorkflowKind.LEGACY_SINGLE_SOURCE_DISCOVERY
    benchmark_mode: str = Field(default="none", min_length=1, max_length=120)


class WorkflowCommandBody(AdminRequest):
    expected_version: int = Field(ge=0)
    actor: str = Field(default="local-admin", min_length=2, max_length=120)


class DatasetSpecificationRetryBody(WorkflowCommandBody):
    endpoint_discovery_scope: EndpointDiscoveryScope | None = None


class SourceDiscoveryAuthorizationBody(WorkflowCommandBody):
    confirmation: Literal["authorize_reviewed_source_discovery"]


class DiscoveryRevisionBody(WorkflowCommandBody):
    reason: str = Field(min_length=3, max_length=4000)
    provider_policy_revision: dict[str, Any] = Field(default_factory=dict)
    modality_policy_revision: dict[str, Any] = Field(default_factory=dict)
    context_constraint_revision: dict[str, Any] = Field(default_factory=dict)


class StrategyApprovalBody(WorkflowCommandBody):
    strategy_proposal_id: str = Field(min_length=3, max_length=160)
    approved_modality_aggregation: dict[str, Any]
    approved_context_filters: dict[str, Any]
    approved_dose_time_rules: dict[str, Any]
    approved_label_policy: dict[str, Any]
    exclusion_rules: list[str] = Field(default_factory=list, max_length=200)
    required_extraction_fields: list[str] = Field(min_length=1, max_length=500)


class StrategySetRejectionBody(WorkflowCommandBody):
    reason: str = Field(min_length=3, max_length=4000)
    reason_category: str = Field(
        default="human_scientific_rejection", min_length=3, max_length=160
    )
    revision_objective: str | None = Field(default=None, min_length=3, max_length=4000)
    technical_proof_classification: Literal["TECHNICAL_PROOF_OF_JOINABILITY"] | None = None
    technical_proof_proposal_ids: list[str] = Field(default_factory=list, max_length=500)


class AssemblyRunBody(WorkflowCommandBody):
    offline_fixture_id: Literal["tr_receptor", "dna_damage"]


class DatasetReviewBody(WorkflowCommandBody):
    rationale: str = Field(min_length=3, max_length=4000)


class DatasetRevisionBody(WorkflowCommandBody):
    decision: Literal["rejected", "revision_requested"]
    rationale: str = Field(min_length=3, max_length=4000)


class ModelSelectionBody(WorkflowCommandBody):
    candidate_id: str = Field(min_length=3, max_length=160)
    decision_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    rationale: str = Field(min_length=3, max_length=4000)


class ModelReviewBody(WorkflowCommandBody):
    decision: Literal["reject_all_models", "request_new_benchmark"]
    rationale: str = Field(min_length=3, max_length=4000)


class GeoValidationProbeBody(AdminRequest):
    accessions: list[Annotated[str, Field(pattern=r"^GSE[1-9][0-9]{1,8}$")]] = Field(
        min_length=1, max_length=5
    )


class ApprovalDecisionBody(AdminRequest):
    decision: ApprovalDecisionValue
    reviewer_id: str = Field(default="local-admin", min_length=2, max_length=120)
    reviewer_comment: str = Field(default="", max_length=4000)
    selected_alternative_id: str | None = Field(default=None, max_length=160)
    expected_version: int = Field(ge=0)
    artifact_hashes: list[str] = Field(min_length=1, max_length=100)
    dataset_specification_policy: dict[str, Any] | None = None
