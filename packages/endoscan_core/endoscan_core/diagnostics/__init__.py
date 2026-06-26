"""Framework-free coverage diagnostics (overlap + gate-floor verdict).

Pure functions over already-parsed inputs — NO rdkit, NO network, NO file I/O. The
impure adapters (CoMPARA fetch/parse, rdkit identity, LINCS metadata) live in the
``endoscan_jobs`` package and call into here. This keeps ``endoscan_core`` lean and the
coverage math unit-testable on synthetic data.
"""

from __future__ import annotations

from .coverage import (
    ContextResult,
    CoverageReport,
    GateFloors,
    build_coverage_report,
    context_overlap,
    gate_verdict,
)

__all__ = [
    "ContextResult",
    "CoverageReport",
    "GateFloors",
    "build_coverage_report",
    "context_overlap",
    "gate_verdict",
]
