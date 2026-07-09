"""Biological-pathway over-representation for an /explain result (API-side, standard stats).

For a prediction, this reports which Reactome pathways are over-represented among the genes
that drove THIS MODEL toward an active call. It is a hypothesis generator — NOT a mechanistic
claim, NOT a statement that the compound perturbed/activated anything.

Honest statistics by construction:
  * The universe is the model's OWN feature space — the 978 landmark genes (∩ genes Reactome
    knows) — never the genome. A genome background would inflate significance for pathways that
    merely contain many landmark genes.
  * Each pathway is tested only on its landmark-intersecting members; pathways with < 3 landmark
    genes are not tested at all.
  * Fisher exact, one-sided (over-representation) via the hypergeometric tail; Benjamini–Hochberg
    across the whole tested family for this request.

No science lives in ``endoscan_core`` for this — it is standard ORA math (scipy hypergeometric
tail + a local BH). The Reactome gene sets are a committed artifact (``reactome_store``); nothing
is fabricated: absent data yields an honest "unavailable" state upstream.
"""

from __future__ import annotations

from dataclasses import dataclass

from scipy.stats import hypergeom

#: Pinned attribution depth for pathway analysis. The pathways endpoint asks /explain for this
#: many top contributors REGARDLESS of any caller top_n — the science must not depend on a UI knob.
PINNED_TOP_N = 50
#: A pathway needs at least this many genes IN THE UNIVERSE to be testable (fewer is meaningless).
MIN_PATHWAY_OVERLAP = 3
#: Fewer than this many toward-signal genes → refuse to test (no q-values on a handful of genes).
MIN_TOWARD_GENES = 5
#: Nothing at or above this adjusted-p is shown at all (empty is an honest result).
Q_DISPLAY_MAX = 0.10


def benjamini_hochberg(pvalues: list[float]) -> list[float]:
    """BH-adjusted p-values (q), returned in the input order, monotonic and capped at 1.0.

    ``q_i = min_{j>=i}( p_(j) * m / j )`` over the ascending-p ranking (step-up), so it is the
    standard Benjamini–Hochberg FDR across the whole family passed in.
    """
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])  # ascending by p
    q = [0.0] * m
    running_min = 1.0
    for rank in range(m, 0, -1):  # largest p (rank m) down to smallest (rank 1)
        idx = order[rank - 1]
        running_min = min(running_min, pvalues[idx] * m / rank)
        q[idx] = min(running_min, 1.0)
    return q


def fisher_over_representation(overlap: int, universe: int, pathway: int, sample: int) -> float:
    """One-sided Fisher exact (over-representation) = hypergeometric P(X >= overlap).

    ``universe`` = N (landmark ∩ Reactome), ``pathway`` = K (pathway genes in the universe),
    ``sample`` = n (toward genes in the universe), ``overlap`` = k. ``hypergeom.sf(k-1, N, K, n)``.
    """
    return float(hypergeom.sf(overlap - 1, universe, pathway, sample))


def evidence_label(q_value: float, overlap: int) -> str | None:
    """Transparent label from the real adjusted p + overlap count. None => not shown.

    High:   q < 0.01 AND overlap >= 3
    Medium: q < 0.05
    Low:    q < 0.10
    (q >= 0.10 -> None: not displayed at all)
    """
    if q_value < 0.01 and overlap >= 3:
        return "High"
    if q_value < 0.05:
        return "Medium"
    if q_value < Q_DISPLAY_MAX:
        return "Low"
    return None


#: The evidence-label mapping, surfaced VERBATIM in Technical details so a reader can verify a
#: label against the q shown. Keep in sync with :func:`evidence_label`.
EVIDENCE_MAPPING = {
    "High": "q < 0.01 and overlap >= 3",
    "Medium": "q < 0.05",
    "Low": "q < 0.10",
    "not shown": "q >= 0.10",
}


@dataclass(frozen=True)
class Pathway:
    """A Reactome pathway's landmark-relevant gene membership (symbols)."""

    id: str
    name: str
    genes: frozenset[str]
    description: str | None = None


@dataclass(frozen=True)
class PathwayResult:
    pathway_id: str
    name: str
    description: str | None
    genes_influencing_result: list[str]  # the actual overlap (toward ∩ pathway ∩ universe)
    overlap_count: int
    pathway_size_in_universe: int
    input_size_in_universe: int
    p_value: float
    q_value: float
    evidence: str


@dataclass(frozen=True)
class EnrichmentOutcome:
    results: list[PathwayResult]
    universe_size: int
    family_size: int  # number of pathways actually tested (K >= MIN_PATHWAY_OVERLAP)
    n_input_genes: int  # toward genes intersected with the universe


def select_toward_genes(contributors) -> list[str]:
    """All toward-direction genes from the pinned top-N /explain contributors, in input order.

    ``contributors`` is ``ExplanationResult.top_contributors`` (already the top PINNED_TOP_N by
    |contribution|). Parity across methods: both tree_shap and linear_coefficient set
    ``direction == "toward"`` from the sign of a signed positive-class contribution.
    """
    return [c.gene for c in contributors if getattr(c, "direction", None) == "toward"]


def run_enrichment(
    toward_genes: list[str],
    landmark_genes: list[str],
    pathways: list[Pathway],
    *,
    min_overlap: int = MIN_PATHWAY_OVERLAP,
) -> EnrichmentOutcome:
    """ORA of ``toward_genes`` against ``pathways`` on the landmark ∩ Reactome universe.

    Universe = landmark genes that Reactome annotates (appear in >=1 pathway). Each pathway is
    restricted to its universe intersection; pathways with < ``min_overlap`` universe genes are
    not tested. Fisher one-sided per tested pathway, then BH across the tested family.
    """
    reactome_genes = frozenset().union(*[p.genes for p in pathways]) if pathways else frozenset()
    universe = frozenset(landmark_genes) & reactome_genes
    n_universe = len(universe)
    sample = frozenset(toward_genes) & universe
    n_sample = len(sample)

    tested: list[tuple[Pathway, frozenset[str], int, float]] = []
    for p in pathways:
        p_in_u = p.genes & universe
        if len(p_in_u) < min_overlap:
            continue
        overlap = sample & p_in_u
        pval = fisher_over_representation(len(overlap), n_universe, len(p_in_u), n_sample)
        tested.append((p, overlap, len(p_in_u), pval))

    qvals = benjamini_hochberg([t[3] for t in tested])
    results: list[PathwayResult] = []
    for (p, overlap, k_size, pval), q in zip(tested, qvals, strict=True):
        label = evidence_label(q, len(overlap))
        if label is None:
            continue  # q >= 0.10 -> not shown; NEVER relaxed to surface something
        results.append(
            PathwayResult(
                pathway_id=p.id,
                name=p.name,
                description=p.description,
                genes_influencing_result=sorted(overlap),
                overlap_count=len(overlap),
                pathway_size_in_universe=k_size,
                input_size_in_universe=n_sample,
                p_value=pval,
                q_value=q,
                evidence=label,
            )
        )
    results.sort(key=lambda r: (r.q_value, r.p_value, r.pathway_id))
    return EnrichmentOutcome(
        results=results,
        universe_size=n_universe,
        family_size=len(tested),
        n_input_genes=n_sample,
    )
