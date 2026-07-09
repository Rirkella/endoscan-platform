"""Enrichment math + honesty invariants (pure functions; no HTTP, no real model).

The exact Fisher p and BH q on a contrived case are hand-computed and asserted here — this proves
the statistics, not merely that the code runs. Also covers the universe restriction, the min-overlap
exclusion, BH across the tested family, evidence-label boundaries, the empty result, and the
toward-gene selection parity across attribution methods.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from endoscan_api.pathways import (
    MIN_PATHWAY_OVERLAP,
    Pathway,
    benjamini_hochberg,
    evidence_label,
    fisher_over_representation,
    run_enrichment,
    select_toward_genes,
)

# Contrived, hand-computable case: universe N=20, sample n=6 (G1..G6).
_U20 = [f"G{i}" for i in range(1, 21)]
_TOWARD6 = ["G1", "G2", "G3", "G4", "G5", "G6"]


def test_fisher_exact_values_hand_computed() -> None:
    # P(X>=4 | N=20,K=4,n=6) = C(4,4)C(16,2)/C(20,6) = 120/38760.
    assert fisher_over_representation(4, 20, 4, 6) == pytest.approx(120 / 38760, rel=1e-12)
    # P(X>=2 | N=20,K=3,n=6) = 1 - [C(17,6)+3C(17,5)]/C(20,6) = 7820/38760.
    assert fisher_over_representation(2, 20, 3, 6) == pytest.approx(7820 / 38760, rel=1e-12)
    # k=0 over-representation is always p=1.0.
    assert fisher_over_representation(0, 20, 3, 6) == 1.0


def test_benjamini_hochberg_exact_values() -> None:
    # Family of 3 raw p; BH step-up (monotone from the top). m=3.
    q = benjamini_hochberg([120 / 38760, 7820 / 38760, 1.0])
    assert q[0] == pytest.approx(3 * 120 / 38760, rel=1e-12)  # 0.00928792...
    assert q[1] == pytest.approx(1.5 * 7820 / 38760, rel=1e-12)  # 0.30263157...
    assert q[2] == pytest.approx(1.0, rel=1e-12)
    # BH is applied ACROSS the family (q depends on m and rank, not per-pathway).
    assert q[0] > 120 / 38760  # adjusted upward vs the raw p


def test_run_enrichment_universe_restriction_min_overlap_and_exact_q() -> None:
    # PE is a landmark "background" pathway that fills the universe to all 20 landmark genes,
    # so the universe = 20 (hand-computable). Z* genes are NOT landmark and must be dropped.
    background = ["G8", "G9", "G13", "G14", "G15", "G16", "G17", "G18", "G19", "G20"]
    pathways = [
        Pathway("PA", "PA", frozenset(["G1", "G2", "G3", "G4"])),  # K=4, overlap 4
        Pathway("PB", "PB", frozenset(["G5", "G6", "G7"])),  # K=3, overlap 2
        Pathway("PC", "PC", frozenset(["G10", "G11", "G12"])),  # K=3, overlap 0
        Pathway("PD", "PD", frozenset(["G1", "G2"])),  # K=2 -> EXCLUDED
        Pathway("PE", "PE", frozenset(background)),  # K=10, overlap 0 (fills the universe)
        # Non-landmark genes (Z*) must not count toward K: only the landmark intersection does.
        Pathway(
            "PF", "PF", frozenset(["G1", "G2", "Z1", "Z2", "Z3"])
        ),  # K in universe = 2 -> EXCLUDED
    ]
    out = run_enrichment(_TOWARD6, _U20, pathways)
    assert out.universe_size == 20  # all 20 landmark genes are Reactome-known; Z* dropped
    assert out.family_size == 4  # PA, PB, PC, PE tested; PD and PF excluded (K<3 in universe)
    ids = {r.pathway_id for r in out.results}
    assert "PD" not in ids and "PF" not in ids
    # Only PA clears q<0.10. Exact end-to-end value: p = 120/38760; q = 4*p across the family.
    pa = next(r for r in out.results if r.pathway_id == "PA")
    assert pa.p_value == pytest.approx(120 / 38760, rel=1e-12)
    assert pa.q_value == pytest.approx(4 * 120 / 38760, rel=1e-12)  # 0.012384 -> Medium
    assert pa.evidence == "Medium"
    assert pa.genes_influencing_result == ["G1", "G2", "G3", "G4"]  # the ACTUAL overlap
    assert pa.overlap_count == 4 and pa.pathway_size_in_universe == 4


def test_run_enrichment_empty_result_is_a_real_result() -> None:
    # A single weak pathway (overlap 1 of a 3-gene set) never clears q<0.10 -> empty, NOT relaxed.
    pathways = [Pathway("PB", "PB", frozenset(["G5", "G6", "G7"]))]  # overlap 1 with toward {G5}
    out = run_enrichment(["G5"], _U20, pathways)
    assert out.family_size == 1
    assert out.results == []  # nothing fabricated to fill the gap


def test_evidence_label_boundaries() -> None:
    assert evidence_label(0.0099, 3) == "High"  # q<0.01 AND overlap>=3
    assert evidence_label(0.0099, 2) == "Medium"  # overlap<3 -> not High, but q<0.05
    assert evidence_label(0.0101, 3) == "Medium"  # q>=0.01 -> not High
    assert evidence_label(0.049, 2) == "Medium"
    assert evidence_label(0.05, 2) == "Low"  # q<0.05 is strict -> Low
    assert evidence_label(0.099, 5) == "Low"
    assert evidence_label(0.10, 9) is None  # q>=0.10 -> not shown
    assert evidence_label(0.5, 9) is None


def test_min_pathway_overlap_constant_is_three() -> None:
    assert MIN_PATHWAY_OVERLAP == 3


def test_select_toward_genes_parity_across_methods() -> None:
    # direction is derived identically for tree_shap and linear_coefficient (sign of a signed
    # contribution), so the toward-gene rule is method-agnostic.
    contribs = [
        SimpleNamespace(gene="A", shap_value=0.9, direction="toward"),
        SimpleNamespace(gene="B", shap_value=-0.8, direction="away"),
        SimpleNamespace(gene="C", shap_value=0.2, direction="toward"),
    ]
    assert select_toward_genes(contribs) == ["A", "C"]  # away dropped, order preserved
    # Same selection regardless of which method produced the (gene, direction) pairs.
    assert select_toward_genes(list(reversed(contribs))) == ["C", "A"]
