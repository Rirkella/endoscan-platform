"""Assemble the honest limitations block attached to EVERY inference output.

Every field is read from STRUCTURED artifacts — never by parsing model-card prose, so
a future card rewording cannot change inference behaviour:

- ``status``        <- the registry ``EndpointEntry.status``;
- ``prevalence``    <- ``metrics.json`` confusion matrix (pos = fn+tp) over ``n_samples``;
- ``missed_criteria`` <- recomputed from ``metrics.json`` metric VALUES vs the run's
  ``validated_mvp_floors``/``validated_mvp_ceilings`` (also in ``metrics.json``), using
  the SAME comparison the trainer's status decision makes (floor ``>=``, ceiling ``<=``);
- ``claim_scope``   <- the structured ``metrics.json["claim_scope"]`` field;
- ``floors_provenance`` <- the optional ``metrics.json["floors_provenance"]`` field — a
  note explaining a PARTIAL floor record (some floors recovered, others not persisted)
  so an incomplete set never reads as "all floors met" and no threshold is fabricated.

If a legacy ``metrics.json`` predates these structured fields, the block degrades
honestly: status + prevalence still resolve; ``floors_recorded`` is ``False`` and
``missed_criteria`` is left empty (NOT fabricated); ``claim_scope`` falls back to a
conservative generic scope.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..registry.schema import EndpointEntry

GENERIC_CLAIM_SCOPE = (
    "the configured endpoint only; NOT a pan-endocrine, regulatory, clinical, or "
    "diagnostic claim"
)
DISCLAIMER = (
    "Pre-screening / prioritization only; metrics are the honest compound-level "
    "cross-validated estimate, NOT regulatory-grade validation, and must be confirmed "
    "experimentally."
)


class LimitationsBlock(BaseModel):
    """Structured caveats attached to every prediction/explanation output."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: str
    status: str
    is_experimental: bool
    missed_criteria: list[str]
    floors_recorded: bool
    floors_provenance: str | None = None
    positives: int | None
    n_total: int | None
    prevalence: float | None
    claim_scope: str
    disclaimer: str = DISCLAIMER


def _require_metric(metrics: dict, name: str, kind: str) -> float:
    """Fetch ``metrics[name]`` or RAISE — a floor/ceiling naming an absent metric is
    un-evaluable, and silently skipping it would let inference under-report a criterion
    the trainer's ``_status_for`` enforces (it reads the metric off the EvalMetrics
    object and would compare it). A small/partial floor record is fine; this only fires
    when a listed key has NO value in ``metrics``."""
    value = metrics.get(name)
    if value is None:
        raise ValueError(
            f"validated_mvp {kind} references metric '{name}', which has no value in "
            f"metrics (un-evaluable {kind}; emit the metric into metrics.json or drop "
            f"the threshold)"
        )
    return value


def missed_criteria(
    metrics: dict, floors: dict[str, float], ceilings: dict[str, float]
) -> list[str]:
    """Floors/ceilings the recorded metrics FAIL, value-vs-threshold (floor >=, ceiling <=).

    Mirrors the trainer's ``_status_for`` exactly: same metric keys, same operators
    (floor ``<``, ceiling ``>``). Every floor/ceiling key MUST resolve to a value in
    ``metrics``; a key whose metric is ABSENT raises (see :func:`_require_metric`) rather
    than being silently skipped. This does NOT fire for a small/partial floor record —
    e.g. ``{"balanced_accuracy": 0.60}`` evaluates normally as long as that one key is
    present in ``metrics``.
    """
    out: list[str] = []
    for name, thr in floors.items():
        value = _require_metric(metrics, name, "floor")
        if value < thr:
            out.append(f"{name.replace('_', ' ')} {value:.3f} < {thr:.2f} floor")
    for name, thr in ceilings.items():
        value = _require_metric(metrics, name, "ceiling")
        if value > thr:
            out.append(f"{name.replace('_', ' ')} {value:.3f} > {thr:.2f} ceiling")
    return out


def build_limitations(entry: EndpointEntry, metrics: dict) -> LimitationsBlock:
    """Assemble the limitations block from the registry entry + metrics.json (structured)."""
    status = entry.status.value if hasattr(entry.status, "value") else str(entry.status)

    floors = metrics.get("validated_mvp_floors")
    ceilings = metrics.get("validated_mvp_ceilings") or {}
    floors_recorded = isinstance(floors, dict)
    missed = missed_criteria(metrics, floors, ceilings) if floors_recorded else []
    # A PARTIAL floor record (e.g. only balanced_accuracy recovered, the rest not
    # persisted) is honest, not fabricated: missed_criteria evaluates the keys that ARE
    # present, and floors_provenance documents WHY the record is partial so the block
    # never implies "all floors met" from an incomplete set.
    floors_provenance = metrics.get("floors_provenance")

    cm = metrics.get("confusion_matrix") or {}
    positives = (int(cm["fn"]) + int(cm["tp"])) if {"fn", "tp"} <= set(cm) else None
    n_total = metrics.get("n_samples")
    prevalence = (positives / n_total) if (positives is not None and n_total) else None

    claim_scope = metrics.get("claim_scope") or GENERIC_CLAIM_SCOPE

    return LimitationsBlock(
        endpoint_id=entry.endpoint_id,
        status=status,
        is_experimental=(status == "experimental"),
        missed_criteria=missed,
        floors_recorded=floors_recorded,
        floors_provenance=floors_provenance,
        positives=positives,
        n_total=n_total,
        prevalence=prevalence,
        claim_scope=claim_scope,
    )
