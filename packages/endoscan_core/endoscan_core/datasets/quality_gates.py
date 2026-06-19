"""The dataset quality gate.

Compares a `DatasetQualityReport` against thresholds from
``registry/data/quality_gates.yaml`` and returns ONLY a pass/fail verdict. It
never triggers training (PROJECT_RULES.md §3.2/§3.3) — it is a hard boundary that
later milestones consult before any gated training step.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..registry.store import find_repo_root
from .quality_report import DatasetQualityReport

#: Location of the thresholds, relative to the repository root.
GATES_RELPATH = Path("registry/data/quality_gates.yaml")


class GateThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_compounds_per_class: int
    min_overlap: int
    max_duplicate_signature_rate: float
    max_label_conflict_rate: float
    min_metadata_coverage: float
    require_compound_level_split_feasible: bool


class GateCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    observed: float | int | bool
    threshold: float | int | bool


class GateVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    checks: list[GateCheck] = Field(default_factory=list)
    summary: str


def load_gates(repo_root: Path | None = None) -> GateThresholds:
    """Load and validate gate thresholds from ``quality_gates.yaml``."""
    root = repo_root or find_repo_root()
    path = root / GATES_RELPATH
    if not path.is_file():
        raise FileNotFoundError(f"Quality gates not found at {path}")
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return GateThresholds.model_validate(raw["thresholds"])


def quality_gates(report: DatasetQualityReport, thresholds: GateThresholds) -> GateVerdict:
    """Evaluate ``report`` against ``thresholds`` and return a pass/fail verdict.

    This function only compares numbers; it performs no training, registration, or
    side effects.
    """
    # Compound-level (not row-level) counts; 0 if a class is missing, which fails.
    compound_counts = report.per_class_compound_counts
    min_class_count = min(compound_counts.values()) if compound_counts else 0
    both_classes_present = len(compound_counts) >= 2
    split_ok = report.split_feasible and not report.compound_level_leakage

    checks = [
        GateCheck(
            name="min_compounds_per_class",
            passed=both_classes_present and min_class_count >= thresholds.min_compounds_per_class,
            observed=min_class_count,
            threshold=thresholds.min_compounds_per_class,
        ),
        GateCheck(
            name="min_overlap",
            passed=report.n_overlap >= thresholds.min_overlap,
            observed=report.n_overlap,
            threshold=thresholds.min_overlap,
        ),
        GateCheck(
            name="max_duplicate_signature_rate",
            passed=report.duplicate_signature_rate <= thresholds.max_duplicate_signature_rate,
            observed=report.duplicate_signature_rate,
            threshold=thresholds.max_duplicate_signature_rate,
        ),
        GateCheck(
            name="max_label_conflict_rate",
            passed=report.label_conflict_rate <= thresholds.max_label_conflict_rate,
            observed=report.label_conflict_rate,
            threshold=thresholds.max_label_conflict_rate,
        ),
        GateCheck(
            name="min_metadata_coverage",
            passed=report.metadata_coverage >= thresholds.min_metadata_coverage,
            observed=report.metadata_coverage,
            threshold=thresholds.min_metadata_coverage,
        ),
    ]

    if thresholds.require_compound_level_split_feasible:
        checks.append(
            GateCheck(
                name="require_compound_level_split_feasible",
                passed=split_ok,
                observed=split_ok,
                threshold=True,
            )
        )

    passed = all(check.passed for check in checks)
    failed = [check.name for check in checks if not check.passed]
    summary = "PASS" if passed else f"FAIL: {', '.join(failed)}"
    return GateVerdict(passed=passed, checks=checks, summary=summary)
