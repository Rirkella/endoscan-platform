"""Thin HTTP-only admin request shapes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from endoscan_workflows.contracts import ApprovalDecisionValue


class AdminRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateBuildBody(AdminRequest):
    endpoint_name: str = Field(min_length=3, max_length=120)
    endpoint_slug: str = Field(min_length=3, max_length=80)
    biological_goal: str = Field(min_length=10, max_length=4000)
    created_by: str = Field(default="local-admin", min_length=2, max_length=120)


class WorkflowCommandBody(AdminRequest):
    expected_version: int = Field(ge=0)
    actor: str = Field(default="local-admin", min_length=2, max_length=120)


class ApprovalDecisionBody(AdminRequest):
    decision: ApprovalDecisionValue
    reviewer_id: str = Field(default="local-admin", min_length=2, max_length=120)
    reviewer_comment: str = Field(default="", max_length=4000)
    selected_alternative_id: str | None = Field(default=None, max_length=160)
    expected_version: int = Field(ge=0)
    artifact_hashes: list[str] = Field(min_length=1, max_length=100)
