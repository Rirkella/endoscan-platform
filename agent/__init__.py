"""EndoScan Builder Agent — sequences tested tools to a gate, then a guarded promotion.

The agent reimplements no science: it sequences the M2 dataset tools, records build
status, and enforces the gate + human-approval boundary before delegating train/register
to the M3 ``run_pipeline``. See :mod:`agent.builder_agent`.
"""

from __future__ import annotations

from .build_state import (
    ApprovalRequiredError,
    ApprovalToken,
    BuildError,
    BuildRecipe,
    BuildRecord,
    BuildState,
    BuildStateError,
    GateDivergenceError,
    GateFailedError,
    GateReport,
    IncompleteArtifactsError,
)
from .builder_agent import (
    approve,
    get_gate_report,
    get_status,
    promote,
    reject,
    start_build,
)

__all__ = [
    "ApprovalRequiredError",
    "ApprovalToken",
    "BuildError",
    "BuildRecipe",
    "BuildRecord",
    "BuildState",
    "BuildStateError",
    "GateDivergenceError",
    "GateFailedError",
    "GateReport",
    "IncompleteArtifactsError",
    "approve",
    "get_gate_report",
    "get_status",
    "promote",
    "reject",
    "start_build",
]
