"""Pure coverage math: per-cell-line LINCS overlap + the quality-gate-floor verdict.

A "coverage diagnostic" answers, for a candidate endpoint, how many labelled compounds
have a LINCS signature and the positive/negative split — versus the gate floors
(``min_overlap``, ``min_compounds_per_class``) — across candidate cell-line contexts.

Everything here is pure: inputs are a ``{InChIKey: 0|1}`` label map and, per cell line,
the set of InChIKeys profiled there. No rdkit, no network, no file I/O — the
``endoscan_jobs`` coverage job builds those inputs (CoMPARA parse + rdkit identity +
LINCS metadata) and calls these functions. This mirrors the gate the M5 agent enforces;
it never recomputes or weakens the gate — it only REPORTS a verdict against its floors.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

#: Default floors mirror ``registry/data/quality_gates.yaml`` (the agent reads them too).
DEFAULT_MIN_OVERLAP = 40
DEFAULT_MIN_COMPOUNDS_PER_CLASS = 20


class GateFloors(BaseModel):
    """The two gate floors a coverage verdict is judged against."""

    model_config = ConfigDict(extra="forbid")

    min_overlap: int = DEFAULT_MIN_OVERLAP
    min_compounds_per_class: int = DEFAULT_MIN_COMPOUNDS_PER_CLASS


class ContextResult(BaseModel):
    """Coverage for one cell-line context (a set of LINCS cell lines)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    cell_lines: list[str]
    overlap: int
    positives: int
    negatives: int
    prevalence: float
    verdict: str  # "PASS" | "failed_qc"
    reason: str


class CoverageReport(BaseModel):
    """The structured coverage diagnostic — the machine form of the operator summary."""

    model_config = ConfigDict(extra="forbid")

    target: str
    source_table: str | None = None
    source_call_column: str | None = None
    n_structures: int
    n_positives: int
    n_negatives: int
    n_conflicts: int
    floors: GateFloors
    line_signature_counts: dict[str, int] = Field(default_factory=dict)
    contexts: list[ContextResult] = Field(default_factory=list)
    recommended_context: str | None = None
    recommendation_note: str = ""

    @property
    def any_context_passes(self) -> bool:
        return any(c.verdict == "PASS" for c in self.contexts)


def gate_verdict(
    overlap: int, positives: int, negatives: int, floors: GateFloors
) -> tuple[str, str]:
    """Return ``("PASS"|"failed_qc", reason)`` — same floor logic the gate uses.

    Mirrors ``quality_gates``: a context PASSes only if ``overlap >= min_overlap`` AND
    ``min(pos, neg) >= min_compounds_per_class``. The reason names every unmet floor.
    """
    reasons: list[str] = []
    if overlap < floors.min_overlap:
        reasons.append(f"overlap {overlap}<{floors.min_overlap}")
    if min(positives, negatives) < floors.min_compounds_per_class:
        reasons.append(f"min-class {min(positives, negatives)}<{floors.min_compounds_per_class}")
    if reasons:
        return "failed_qc", "; ".join(reasons)
    return "PASS", "clears both floors"


def context_overlap(
    labels: Mapping[str, int], inchikeys_in_context: set[str]
) -> tuple[int, int, int]:
    """Return ``(overlap, positives, negatives)`` for one context's profiled InChIKeys."""
    overlap_keys = set(labels) & inchikeys_in_context
    positives = sum(1 for k in overlap_keys if labels[k] == 1)
    negatives = sum(1 for k in overlap_keys if labels[k] == 0)
    return len(overlap_keys), positives, negatives


def _recommend(contexts: list[ContextResult]) -> tuple[str | None, str]:
    """Prefer an androgen-relevant context that PASSes (best class balance); else the
    best-overlap passing context with a biological-context limitation; else flag the
    honest failed_qc expectation (a valid outcome — the gate rejects underpowered data).
    """
    if not contexts:
        return None, "no contexts evaluated"
    andro_pass = [c for c in contexts if "androgen" in c.name.lower() and c.verdict == "PASS"]
    passing = [c for c in contexts if c.verdict == "PASS"]
    if andro_pass:
        best = max(andro_pass, key=lambda c: min(c.positives, c.negatives))
        return best.name, "androgen-relevant context clears the gate — biologically apt."
    if passing:
        best = max(passing, key=lambda c: min(c.positives, c.negatives))
        return best.name, (
            "no androgen-relevant context clears the gate; using a broader/ER-style "
            "context — NOTE the biological-context limitation (not target-tissue specific)."
        )
    best = max(contexts, key=lambda c: (c.overlap, min(c.positives, c.negatives)))
    return best.name, (
        "NO context clears the gate at current floors -> expected outcome is failed_qc "
        "(a VALID result: the gate rejects underpowered data on a new target)."
    )


def build_coverage_report(
    *,
    target: str,
    labels: Mapping[str, int],
    n_conflicts: int,
    context_inchikeys: Mapping[str, set[str]],
    contexts: Mapping[str, list[str]],
    line_signature_counts: Mapping[str, int],
    floors: GateFloors | None = None,
    source_table: str | None = None,
    source_call_column: str | None = None,
) -> CoverageReport:
    """Assemble a :class:`CoverageReport` from labels + per-context profiled InChIKeys.

    ``context_inchikeys`` maps each context name to the union of InChIKeys profiled by
    its cell lines (the job computes this from LINCS metadata). ``contexts`` records the
    cell-line members for display. All math is pure and deterministic.
    """
    floors = floors or GateFloors()
    results: list[ContextResult] = []
    for name, lines in contexts.items():
        overlap, pos, neg = context_overlap(labels, context_inchikeys.get(name, set()))
        verdict, reason = gate_verdict(overlap, pos, neg, floors)
        results.append(
            ContextResult(
                name=name,
                cell_lines=list(lines),
                overlap=overlap,
                positives=pos,
                negatives=neg,
                prevalence=(pos / overlap) if overlap else 0.0,
                verdict=verdict,
                reason=reason,
            )
        )
    recommended, note = _recommend(results)
    return CoverageReport(
        target=target,
        source_table=source_table,
        source_call_column=source_call_column,
        n_structures=len(labels),
        n_positives=sum(1 for v in labels.values() if v == 1),
        n_negatives=sum(1 for v in labels.values() if v == 0),
        n_conflicts=n_conflicts,
        floors=floors,
        line_signature_counts=dict(line_signature_counts),
        contexts=results,
        recommended_context=recommended,
        recommendation_note=note,
    )
