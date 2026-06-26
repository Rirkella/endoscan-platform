"""Builder-Agent state model: lifecycle, the recipe, the fingerprinted gate report,
the approval token, the build record, and the errors the chokepoint raises.

The agent reimplements NO science. This module holds only data shapes + the legal
state-transition table; all dataset/gate/train logic lives in tested ``endoscan_core``
tools and the M3 pipeline runner (``pipelines/endpoints/ER/run.py``), which the agent
sequences but never edits.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict


class BuildState(str, Enum):
    """Build lifecycle. ``rejected``/``failed_qc``/``registered`` are terminal; there is
    NO transition from a failed gate or a rejection to ``registered``."""

    created = "created"
    building = "building"
    dataset_ready = "dataset_ready"
    gated_pass = "gated_pass"
    gated_fail = "gated_fail"
    awaiting_approval = "awaiting_approval"
    approved = "approved"
    rejected = "rejected"
    registered = "registered"
    failed_qc = "failed_qc"


#: The only legal transitions. A FAILED gate routes to failed_qc (terminal); a PASS
#: routes through awaiting_approval -> approved -> registered. No edge bridges a
#: failure/rejection to registration.
_LEGAL_TRANSITIONS: dict[BuildState, set[BuildState]] = {
    BuildState.created: {BuildState.building},
    BuildState.building: {BuildState.dataset_ready, BuildState.gated_fail},
    BuildState.dataset_ready: {BuildState.gated_pass, BuildState.gated_fail},
    BuildState.gated_pass: {BuildState.awaiting_approval},
    BuildState.gated_fail: {BuildState.failed_qc},
    BuildState.awaiting_approval: {BuildState.approved, BuildState.rejected},
    BuildState.approved: {BuildState.registered},
    BuildState.rejected: set(),
    BuildState.failed_qc: set(),
    BuildState.registered: set(),
}


class BuildStateError(RuntimeError):
    """Raised on an illegal state transition or a build acted on in the wrong state."""


class ApprovalRequiredError(RuntimeError):
    """Raised when promotion is attempted without a valid, issued approval token."""


class GateFailedError(RuntimeError):
    """Raised when promotion is attempted on a build whose gate verdict is FAIL.

    Approval is necessary but NOT sufficient: a present token never authorizes training
    past a failed gate (PROJECT_RULES.md §3.3)."""


class GateDivergenceError(RuntimeError):
    """Raised when the inner run_pipeline declines to register a gated_pass build —
    i.e. its recomputed verdict disagreed with the recorded outer verdict. Surfaced as
    an error (registry untouched) rather than silently hidden."""


class IncompleteArtifactsError(RuntimeError):
    """Raised when a registered entry is missing a required artifact or its limitations
    block is not derivable."""


class BuildError(RuntimeError):
    """Raised for a malformed recipe or an un-runnable build configuration."""


def assert_transition(current: BuildState, target: BuildState) -> None:
    """Raise ``BuildStateError`` unless ``current -> target`` is a legal transition."""
    if target not in _LEGAL_TRANSITIONS.get(current, set()):
        raise BuildStateError(f"illegal build transition {current.value!r} -> {target.value!r}")


class BuildRecipe(BaseModel):
    """A build request. Carries NO thresholds or floors — those come ONLY from the
    referenced pipeline config's ``gate.thresholds_path`` (a reviewed file). ``extra``
    is forbidden, so a recipe cannot smuggle in a (lowered) threshold knob."""

    model_config = ConfigDict(extra="forbid")

    name: str
    pipeline_runner_path: str  # repo-relative path to the M3 runner (run_pipeline)
    pipeline_config_path: str  # repo-relative path to the reviewed PipelineConfig yaml


class GateReport(BaseModel):
    """The authoritative pass/fail record — the single source of truth for the gate.

    ``fingerprint()`` is computed over the verdict CONTENT, so any tampering with this
    file changes the fingerprint and invalidates an approval token bound to it."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    summary: str
    checks: list[dict]  # serialized GateCheck records (name/passed/observed/threshold)
    dataset_card_path: str | None = None

    def fingerprint(self) -> str:
        payload = {"passed": self.passed, "summary": self.summary, "checks": self.checks}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalToken(BaseModel):
    """Issued only by ``approve()``; binds an approver to a SPECIFIC passed gate report
    via ``gate_fingerprint``. ``value`` is an opaque minted secret."""

    model_config = ConfigDict(extra="forbid")

    build_id: str
    gate_fingerprint: str
    approver: str
    value: str
    issued_at: datetime
    note: str = ""


class BuildRecord(BaseModel):
    """Persisted build status. ``gate_passed`` here is DISPLAY-ONLY and is never trusted
    by ``promote()`` — promotion re-derives pass/fail from the fingerprinted GateReport,
    so there is no second, independently-mutable copy of the pass/fail bit."""

    model_config = ConfigDict(extra="forbid")

    build_id: str
    name: str
    state: BuildState
    endpoint_id: str
    target: str
    recipe: BuildRecipe
    gate_passed: bool | None = None  # display only; NOT authoritative
    created_at: datetime
    updated_at: datetime
