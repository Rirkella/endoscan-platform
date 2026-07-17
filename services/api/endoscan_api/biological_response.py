"""Endpoint-independent pathway analysis for a complete signed transcriptomic signature.

The method is deliberately distinct from endpoint attribution ORA.  It ranks every supplied
Reactome-annotated gene by its signed differential value and performs a two-sided competitive
Wilcoxon/Mann-Whitney rank-sum test for each Reactome gene set.  The standardized rank-sum z
statistic retains direction; Benjamini-Hochberg controls FDR across the tested pathway family.
No control condition is inferred and raw expression is never treated as differential response.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.stats import norm, rankdata

from .pathways import Pathway, benjamini_hochberg

MIN_GENE_SET_SIZE = 5
MAX_GENE_SET_SIZE = 500
MAX_PATHWAYS_PER_DIRECTION = 20
SUPPORTED_DIFFERENTIAL_TYPES = {
    "differential_zscore",
    "log2_fold_change",
    "ranked_statistic",
}


@dataclass(frozen=True)
class RankedPathwayResult:
    pathway_id: str
    name: str
    direction: str
    enrichment_statistic: float
    p_value: float
    q_value: float
    leading_edge_genes: list[str]
    pathway_size_in_universe: int
    statistically_supported: bool


@dataclass(frozen=True)
class RankedEnrichmentOutcome:
    increased: list[RankedPathwayResult]
    decreased: list[RankedPathwayResult]
    universe: list[str]
    pathways_tested: int


def _tie_correction(ranks: np.ndarray) -> float:
    _, counts = np.unique(ranks, return_counts=True)
    n = len(ranks)
    if n < 2:
        return 1.0
    return 1.0 - float(np.sum(counts**3 - counts)) / float(n**3 - n)


def run_preranked_enrichment(
    signature: dict[str, float],
    pathways: list[Pathway],
    *,
    min_size: int = MIN_GENE_SET_SIZE,
    max_size: int = MAX_GENE_SET_SIZE,
) -> RankedEnrichmentOutcome:
    """Run deterministic competitive rank enrichment over the full available signed signature."""
    finite = {gene: float(value) for gene, value in signature.items() if math.isfinite(value)}
    reactome_genes = (
        frozenset().union(*(pathway.genes for pathway in pathways)) if pathways else set()
    )
    universe = sorted(set(finite) & set(reactome_genes))
    if len(universe) < min_size * 2:
        return RankedEnrichmentOutcome([], [], universe, 0)

    values = np.asarray([finite[gene] for gene in universe], dtype=float)
    # rankdata assigns high ranks to high signed values, with deterministic average ties.
    ranks = rankdata(values, method="average")
    correction = _tie_correction(ranks)
    index = {gene: i for i, gene in enumerate(universe)}
    n_total = len(universe)
    tested: list[tuple[Pathway, list[str], float, float]] = []

    for pathway in pathways:
        members = sorted(pathway.genes & set(universe))
        m = len(members)
        if m < min_size or m > max_size or m >= n_total:
            continue
        member_ranks = np.asarray([ranks[index[gene]] for gene in members], dtype=float)
        complement_size = n_total - m
        u_stat = float(member_ranks.sum() - (m * (m + 1) / 2.0))
        expected = m * complement_size / 2.0
        variance = m * complement_size * (n_total + 1) * correction / 12.0
        if variance <= 0:
            continue
        z_score = (u_stat - expected) / math.sqrt(variance)
        p_value = float(2.0 * norm.sf(abs(z_score)))
        tested.append((pathway, members, z_score, p_value))

    q_values = benjamini_hochberg([item[3] for item in tested])
    results: list[RankedPathwayResult] = []
    for (pathway, members, z_score, p_value), q_value in zip(tested, q_values, strict=True):
        direction = "increased" if z_score >= 0 else "decreased"
        ordered = sorted(
            members,
            key=lambda gene: finite[gene],
            reverse=direction == "increased",
        )
        signed = [
            gene
            for gene in ordered
            if (finite[gene] > 0 if direction == "increased" else finite[gene] < 0)
        ]
        leading = (signed or ordered)[:10]
        results.append(
            RankedPathwayResult(
                pathway_id=pathway.id,
                name=pathway.name,
                direction=direction,
                enrichment_statistic=float(z_score),
                p_value=p_value,
                q_value=float(q_value),
                leading_edge_genes=leading,
                pathway_size_in_universe=len(members),
                statistically_supported=q_value < 0.05,
            )
        )

    def sort_key(item: RankedPathwayResult) -> tuple[float, float, str]:
        return (item.q_value, -abs(item.enrichment_statistic), item.pathway_id)

    increased = sorted((item for item in results if item.direction == "increased"), key=sort_key)
    decreased = sorted((item for item in results if item.direction == "decreased"), key=sort_key)
    return RankedEnrichmentOutcome(
        increased=increased[:MAX_PATHWAYS_PER_DIRECTION],
        decreased=decreased[:MAX_PATHWAYS_PER_DIRECTION],
        universe=universe,
        pathways_tested=len(tested),
    )
