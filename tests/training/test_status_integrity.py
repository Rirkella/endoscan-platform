"""Evidence-based, sample-aware validated_mvp integrity (ALL endpoints).

Covers the strengthened status rule end to end at the unit level (fast, no heavy build):
- per-fold + grouped-bootstrap-CI persistence ALONGSIDE the unchanged pooled estimate;
- the min-evidence gate (too few positives -> experimental even when point metrics clear);
- the CI rule (point >= floor but CI lower bound < floor -> experimental; symmetric ceiling);
- REACHABILITY (a well-powered, well-separated case still earns validated_mvp);
- determinism (seeded bootstrap -> reproducible CI);
- ER rebaselined record (the byte-identical model regeneration added CI/evidence; status
  stays experimental; a read-only re-derivation from its stored metrics returns experimental,
  no rewrite).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import numpy as np
import pandas as pd
import pytest

from endoscan_core.registry import EndpointStatus
from endoscan_core.training import (
    MIN_POS_PER_FOLD,
    MIN_POS_TOTAL,
    grouped_bootstrap_ci,
    nested_group_cv,
    unmet_validated_mvp_reasons,
)
from endoscan_core.training.evaluate_endpoint import ConfusionMatrix, EvalMetrics

REPO_ROOT = Path(__file__).resolve().parents[2]
FLOORS = {"auroc": 0.75, "auprc": 0.50, "balanced_accuracy": 0.65}
CEILINGS = {"brier_score": 0.20}


def _metrics(*, auroc=0.80, auprc=0.60, bal=0.70, brier=0.12) -> EvalMetrics:
    return EvalMetrics(
        auroc=auroc, auprc=auprc, balanced_accuracy=bal, f1=0.5, brier_score=brier,
        confusion_matrix=ConfusionMatrix(tn=40, fp=10, fn=10, tp=40), n_samples=100,
    )  # fmt: skip


def _ci(*, auroc=(0.77, 0.86), auprc=(0.55, 0.66), bal=(0.66, 0.74), brier=(0.10, 0.18)) -> dict:
    def b(lo_hi):
        return {"lo": lo_hi[0], "hi": lo_hi[1], "n_resamples": 2000}

    return {
        "auroc": b(auroc),
        "auprc": b(auprc),
        "balanced_accuracy": b(bal),
        "brier_score": b(brier),
    }


def _evidence(*, n_pos=60, min_per_fold=10) -> dict:
    return {"n_positives_total": n_pos, "min_positives_per_fold": min_per_fold, "n_outer_folds": 5}


def _point(m: EvalMetrics) -> dict:
    return {"auroc": m.auroc, "auprc": m.auprc, "balanced_accuracy": m.balanced_accuracy,
            "f1": m.f1, "brier_score": m.brier_score}  # fmt: skip


# --- reachability: validated_mvp is NOT a backdoor "never validate" --------------------


def test_well_powered_clearing_ci_earns_validated_mvp(er_run: ModuleType) -> None:
    m = _metrics()
    reasons = unmet_validated_mvp_reasons(
        _point(m), FLOORS, CEILINGS, uncertainty=_ci(), evidence=_evidence()
    )
    assert reasons == []  # every floor's CI lower bound clears; evidence sufficient
    assert (
        er_run._status_for(m, FLOORS, CEILINGS, uncertainty=_ci(), evidence=_evidence())
        is EndpointStatus.validated_mvp
    )


# --- min-evidence gate (sample-aware) --------------------------------------------------


def test_too_few_positives_blocks_validated_mvp_despite_point_metrics(er_run: ModuleType) -> None:
    m = _metrics()  # point metrics clear every floor, CI clears too...
    thin = _evidence(n_pos=MIN_POS_TOTAL - 1, min_per_fold=10)
    reasons = unmet_validated_mvp_reasons(
        _point(m), FLOORS, CEILINGS, uncertainty=_ci(), evidence=thin
    )
    assert any(f"< {MIN_POS_TOTAL} minimum" in r for r in reasons)
    assert (
        er_run._status_for(m, FLOORS, CEILINGS, uncertainty=_ci(), evidence=thin)
        is EndpointStatus.experimental
    )


def test_too_few_positives_per_fold_blocks_validated_mvp(er_run: ModuleType) -> None:
    m = _metrics()
    thin = _evidence(n_pos=60, min_per_fold=MIN_POS_PER_FOLD - 1)
    reasons = unmet_validated_mvp_reasons(
        _point(m), FLOORS, CEILINGS, uncertainty=_ci(), evidence=thin
    )
    assert any(
        f"min positives/fold {MIN_POS_PER_FOLD - 1} < {MIN_POS_PER_FOLD}" in r for r in reasons
    )


# --- CI rule (point clears, lower bound does not) --------------------------------------


def test_ci_lower_bound_below_floor_blocks_validated_mvp(er_run: ModuleType) -> None:
    m = _metrics(auroc=0.80)  # point AUROC 0.80 >= 0.75 floor...
    ci = _ci(auroc=(0.71, 0.88))  # ...but the 95% CI lower bound 0.71 < 0.75
    reasons = unmet_validated_mvp_reasons(
        _point(m), FLOORS, CEILINGS, uncertainty=ci, evidence=_evidence()
    )
    assert reasons == ["auroc 95% CI lower bound 0.710 < 0.75 floor"]
    assert (
        er_run._status_for(m, FLOORS, CEILINGS, uncertainty=ci, evidence=_evidence())
        is EndpointStatus.experimental
    )


def test_ci_upper_bound_above_ceiling_blocks_validated_mvp() -> None:
    m = _metrics(brier=0.18)  # point Brier 0.18 <= 0.20 ceiling...
    ci = _ci(brier=(0.15, 0.23))  # ...but the CI upper bound 0.23 > 0.20
    reasons = unmet_validated_mvp_reasons(
        _point(m), FLOORS, CEILINGS, uncertainty=ci, evidence=_evidence()
    )
    assert reasons == ["brier score 95% CI upper bound 0.230 > 0.20 ceiling"]


def test_absent_uncertainty_degrades_to_experimental() -> None:
    m = _metrics()
    reasons = unmet_validated_mvp_reasons(
        _point(m), FLOORS, CEILINGS, uncertainty=None, evidence=_evidence()
    )
    assert any("requires a bootstrap CI" in r for r in reasons)  # graceful degrade


# --- persistence + determinism (nested CV on a small synthetic, well-separated set) ----


def _synthetic(n_per_class: int = 40, n_genes: int = 6, seed: int = 0):
    rng = np.random.default_rng(seed)
    pos = rng.normal(1.0, 1.0, size=(n_per_class, n_genes))
    neg = rng.normal(-1.0, 1.0, size=(n_per_class, n_genes))
    X = pd.DataFrame(np.vstack([pos, neg]), columns=[f"g{i}" for i in range(n_genes)])
    y = pd.Series([1] * n_per_class + [0] * n_per_class)
    groups = list(range(2 * n_per_class))  # one compound per row (compound-level)
    return X, y, groups


def test_nested_cv_persists_per_fold_and_ci_without_changing_pooled() -> None:
    X, y, groups = _synthetic()
    models = ["ridge_logreg", "random_forest"]
    res = nested_group_cv(
        X, y, groups, models, outer_splits=5, inner_splits=3, seed=0, tolerance=0.02
    )

    # The pooled estimate is UNCHANGED in shape/meaning (still EvalMetrics over the OOF).
    assert isinstance(res.outer_metrics, EvalMetrics)
    assert res.outer_metrics.n_samples == len(y)
    # Added ALONGSIDE: per-fold metrics, a CI block with point+lo+hi per metric, evidence.
    assert len(res.per_fold_metrics) == res.outer_splits
    assert {"fold", "n_pos", "n_neg", "auroc"} <= set(res.per_fold_metrics[0])
    for name in ("auroc", "auprc", "balanced_accuracy", "brier_score"):
        assert {"point", "lo", "hi"} <= set(res.uncertainty[name])
    assert res.evidence["n_positives_total"] == 40 and res.evidence["n_outer_folds"] == 5


def test_bootstrap_ci_is_seed_reproducible() -> None:
    X, y, groups = _synthetic()
    oof = np.linspace(0, 1, len(y))  # arbitrary but fixed prediction vector
    a = grouped_bootstrap_ci(y, oof, groups, n_resamples=500, seed=7)
    b = grouped_bootstrap_ci(y, oof, groups, n_resamples=500, seed=7)
    assert a["auroc"]["lo"] == b["auroc"]["lo"] and a["auroc"]["hi"] == b["auroc"]["hi"]


def test_pooled_metrics_unchanged_vs_direct_evaluate() -> None:
    # The pooled OOF estimate is still exactly evaluate_endpoint over the pooled predictions —
    # the strengthening adds blocks, it does not alter the existing number.
    X, y, groups = _synthetic(seed=1)
    res = nested_group_cv(
        X, y, groups, ["ridge_logreg"], outer_splits=5, inner_splits=3, seed=1, tolerance=0.02
    )
    assert res.uncertainty["auroc"]["point"] == pytest.approx(res.outer_metrics.auroc)


# --- ER rebaselined record: CI/evidence present + read-only re-derivation is a no-op ----


def test_er_rebaselined_metrics_and_status_untouched(er_run: ModuleType) -> None:
    metrics_path = REPO_ROOT / "models" / "ER" / "metrics.json"
    raw = metrics_path.read_text(encoding="utf-8")
    metrics = json.loads(raw)
    # The rebaseline (byte-identical model regeneration) ADDED the CI + fold-evidence blocks
    # under the strengthened status logic; the point metrics are unchanged from the original.
    assert "uncertainty" in metrics and "evidence" in metrics
    # Registry status stays experimental (the CI lower bounds miss the floors, min-evidence
    # passes at 73 positives / 14 per fold).
    entries = json.loads((REPO_ROOT / "registry" / "models" / "endpoints.json").read_text())
    er = next(e for e in entries["endpoints"] if e["endpoint_id"] == "ER")
    assert er["status"] == "experimental"
    # Read-only re-derivation from ER's stored metrics (with CI/evidence) -> experimental.
    m = EvalMetrics(
        auroc=metrics["auroc"], auprc=metrics["auprc"],
        balanced_accuracy=metrics["balanced_accuracy"], f1=metrics["f1"],
        brier_score=metrics["brier_score"],
        confusion_matrix=ConfusionMatrix(**metrics["confusion_matrix"]),
        n_samples=metrics["n_samples"], threshold=metrics["threshold"],
    )  # fmt: skip
    status = er_run._status_for(
        m, metrics["validated_mvp_floors"], metrics["validated_mvp_ceilings"],
        uncertainty=metrics.get("uncertainty"), evidence=metrics.get("evidence"),
    )  # fmt: skip
    assert status is EndpointStatus.experimental
    # The re-derivation read the file; it must not have rewritten it.
    assert metrics_path.read_text(encoding="utf-8") == raw
