"""Assemble the variant-forward-compatible ``EndpointDetail`` from EXISTING data.

Pure, thin, no science: reads the registry ``EndpointEntry`` (already loaded), the
endpoint's ``metrics.json`` and ``model_card.md`` from disk, and delegates the honest
caveats to ``endoscan_core.inference.build_limitations``. The registry is NOT modified
and card PROSE is NOT parsed — ``scope`` is the structured ``claim_scope`` field, served
verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

from endoscan_core.inference import build_limitations
from endoscan_core.registry import EndpointEntry

from .schemas import (
    ContextBlock,
    ContextVariant,
    EndpointDetail,
    MetricsSummary,
)


def _status_str(entry: EndpointEntry) -> str:
    return entry.status.value if hasattr(entry.status, "value") else str(entry.status)


def build_endpoint_detail(entry: EndpointEntry, repo_root: Path) -> EndpointDetail:
    """Compose the detail response (one context-variant today) from committed artifacts."""
    metrics = json.loads((repo_root / entry.metrics_path).read_text(encoding="utf-8"))
    model_card = (repo_root / entry.model_card_path).read_text(encoding="utf-8")

    limitations = build_limitations(entry, metrics)  # status + CI reasons + scope + disclaimer
    status = _status_str(entry)

    context = ContextBlock(
        cell_lines=None,  # no committed structured source today (see ContextBlock docstring)
        label_sources=list(entry.source_refs),
        scope=metrics.get("claim_scope"),  # structured field, verbatim (no prose parsing)
    )
    metrics_summary = MetricsSummary(
        auroc=metrics.get("auroc"),
        auprc=metrics.get("auprc"),
        balanced_accuracy=metrics.get("balanced_accuracy"),
        brier_score=metrics.get("brier_score"),
        uncertainty=metrics.get("uncertainty"),
        evidence=metrics.get("evidence"),
    )
    variant = ContextVariant(
        variant_id=entry.endpoint_id,  # == endpoint id today; e.g. "AR@VCaP" when a 2nd appears
        context=context,
        status=status,
        limitations=limitations,
        metrics_summary=metrics_summary,
        model_card_markdown=model_card,
    )

    return EndpointDetail(
        endpoint_id=entry.endpoint_id,
        biological_target=entry.biological_target,
        input_type=entry.input_type,
        version=entry.version,
        status=status,
        frozen=entry.frozen,
        source_refs=list(entry.source_refs),
        variants=[variant],  # LIST — additive when contexts multiply
    )
