"""Thin HTTP-only admin request shapes."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from endoscan_workflows.contracts import ApprovalDecisionValue, WorkflowKind


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
