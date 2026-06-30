"""Evidence-based, sample-aware ``validated_mvp`` integrity logic (ALL endpoints).

The strongest status badge must be EVIDENCED, not asserted on a single pooled point
estimate. This module is the SINGLE SOURCE OF TRUTH for "does the evidence clear the
floors?" — used identically by the trainer (``run.py::_status_for`` / ``missed_floors``)
and the M4 inference layer (``inference.limitations::missed_criteria``), so the two
surfaces of the same claim can never diverge (the PR #24 agreement invariant, now
strengthened to the CI + min-evidence rule).

Two requirements, on statistical grounds and applied uniformly:

1. **Minimum evidence (sample-aware).** Below a sample floor a metric estimate is too
   noisy to support a "validated" claim regardless of the point value: metric sampling
   error scales ~1/sqrt(n_pos) and AUPRC is badly biased at small positive counts. The
   best attainable status is then ``experimental``. Thresholds: at least
   :data:`MIN_POS_TOTAL` positive test instances overall and :data:`MIN_POS_PER_FOLD`
   per outer fold. (Set on statistics, not on any endpoint's numbers.)

2. **Floors hold under uncertainty (evidence-based).** ``validated_mvp`` requires the
   **lower** bound of the bootstrap CI to clear each floor (and the **upper** bound to
   clear each ceiling), not merely the point estimate — so a hair-thin point pass with a
   CI that dips below the floor does NOT validate. Floor VALUES are unchanged; only the
   evidence required to clear them is strengthened. When no CI is recorded at all,
   ``validated_mvp`` is unattainable (graceful degrade to ``experimental``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
from sklearn.metrics import (
    average_precision_score,
    balanced_accuracy_score,
    brier_score_loss,
    roc_auc_score,
)

# --- named constants (deliberately NOT in PipelineConfig or quality_gates.yaml, so they
#     cannot be lowered per-build) --------------------------------------------------------
MIN_POS_TOTAL = 30  # minimum positive test instances for validated_mvp eligibility
MIN_POS_PER_FOLD = 5  # minimum positives in every outer fold's test set
BOOTSTRAP_RESAMPLES = 2000  # grouped bootstrap resamples over the pooled OOF
CI_LEVEL = 0.95  # two-sided confidence level for the percentile CI

#: Metrics carrying a bootstrap CI (the four the floors/ceilings are expressed over).
CI_METRICS = ("auroc", "auprc", "balanced_accuracy", "brier_score")

_BOOTSTRAP_NOTE = (
    "percentile bootstrap over compound-grouped resamples of the pooled out-of-fold "
    "predictions; at small positive counts the interval is wide and itself imprecise — a "
    "guard against lucky-split point estimates, not a precise interval."
)


def _label(name: str) -> str:
    return name.replace("_", " ")


def _metric_quartet(y_true: np.ndarray, y_prob: np.ndarray, threshold: float) -> dict[str, float]:
    pred = (y_prob >= threshold).astype(int)
    return {
        "auroc": float(roc_auc_score(y_true, y_prob)),
        "auprc": float(average_precision_score(y_true, y_prob)),
        "balanced_accuracy": float(balanced_accuracy_score(y_true, pred)),
        "brier_score": float(brier_score_loss(y_true, y_prob)),
    }


def fold_metrics(
    y_true: Sequence[int],
    oof: Sequence[float],
    fold_test_indices: Sequence[Sequence[int]],
    *,
    threshold: float = 0.5,
) -> list[dict]:
    """Per-outer-fold metrics from the pooled OOF, guarding single-class folds as null.

    Records ``n_pos``/``n_neg`` per fold always; AUROC/AUPRC/BA/Brier are ``None`` for a
    fold whose test set is single-class (the metrics are undefined there) — that nullity
    is itself evidence of thin fold support.
    """
    y = np.asarray(y_true).astype(int)
    p = np.asarray(oof, dtype=float)
    out: list[dict] = []
    for fold, test_idx in enumerate(fold_test_indices):
        idx = np.asarray(list(test_idx), dtype=int)
        yt, pt = y[idx], p[idx]
        n_pos, n_neg = int((yt == 1).sum()), int((yt == 0).sum())
        rec: dict = {
            "fold": fold,
            "n_test": int(len(idx)),
            "n_pos": n_pos,
            "n_neg": n_neg,
            "auroc": None,
            "auprc": None,
            "balanced_accuracy": None,
            "brier_score": None,
        }
        if n_pos > 0 and n_neg > 0:
            rec.update(_metric_quartet(yt, pt, threshold))
        out.append(rec)
    return out


def grouped_bootstrap_ci(
    y_true: Sequence[int],
    y_prob: Sequence[float],
    groups: Sequence[int],
    *,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
    ci_level: float = CI_LEVEL,
    seed: int = 0,
    threshold: float = 0.5,
) -> dict[str, dict]:
    """Percentile CI per metric from a COMPOUND-GROUPED bootstrap of the pooled OOF.

    Resamples whole compound groups with replacement (a compound's rows move together,
    respecting the leakage structure), recomputes each metric, and takes the
    ``ci_level`` percentile interval. Single-class resamples (no positives or no
    negatives) are discarded. Seeded -> reproducible.
    """
    y = np.asarray(y_true).astype(int)
    p = np.asarray(y_prob, dtype=float)
    g = np.asarray(list(groups))
    uniq = np.unique(g)
    idx_by_group = {grp: np.where(g == grp)[0] for grp in uniq}
    rng = np.random.default_rng(seed)

    collected: dict[str, list[float]] = {m: [] for m in CI_METRICS}
    n_effective = 0
    max_attempts = n_resamples * 5
    for _ in range(max_attempts):
        if n_effective >= n_resamples:
            break
        sampled = rng.choice(uniq, size=len(uniq), replace=True)
        rows = np.concatenate([idx_by_group[grp] for grp in sampled])
        yb, pb = y[rows], p[rows]
        if yb.min() == yb.max():  # single-class resample -> metrics undefined
            continue
        quartet = _metric_quartet(yb, pb, threshold)
        for m in CI_METRICS:
            collected[m].append(quartet[m])
        n_effective += 1

    alpha = (1.0 - ci_level) / 2.0
    out: dict[str, dict] = {}
    for m in CI_METRICS:
        vals = collected[m]
        if vals:
            out[m] = {
                "lo": float(np.quantile(vals, alpha)),
                "hi": float(np.quantile(vals, 1.0 - alpha)),
                "n_resamples": len(vals),
            }
        else:
            out[m] = {"lo": None, "hi": None, "n_resamples": 0}
    return out


def build_uncertainty_block(
    point_metrics: Mapping[str, float] | object,
    ci: dict[str, dict],
    *,
    n_resamples: int,
    ci_level: float,
    seed: int,
) -> dict:
    """Assemble the persisted ``uncertainty`` block: per-metric point + CI lo/hi + meta."""

    def _point(name: str) -> float:
        if isinstance(point_metrics, Mapping):
            return float(point_metrics[name])
        return float(getattr(point_metrics, name))

    block: dict = {
        "method": "grouped_bootstrap_oof",
        "ci_level": ci_level,
        "n_resamples_requested": n_resamples,
        "seed": seed,
        "note": _BOOTSTRAP_NOTE,
    }
    for name in CI_METRICS:
        c = ci.get(name, {})
        block[name] = {
            "point": _point(name),
            "lo": c.get("lo"),
            "hi": c.get("hi"),
            "n_resamples": c.get("n_resamples", 0),
        }
    return block


def evidence_summary(per_fold: Sequence[Mapping], y_true: Sequence[int]) -> dict:
    """Sample-support summary feeding the min-evidence gate."""
    y = np.asarray(y_true).astype(int)
    fold_pos = [int(f["n_pos"]) for f in per_fold] if per_fold else []
    return {
        "n_positives_total": int((y == 1).sum()),
        "n_negatives_total": int((y == 0).sum()),
        "min_positives_per_fold": int(min(fold_pos)) if fold_pos else 0,
        "n_outer_folds": int(len(per_fold)),
    }


def _require_point(point: Mapping[str, float], name: str, kind: str) -> float:
    """Return ``point[name]`` or RAISE — a floor/ceiling naming an absent metric is
    un-evaluable; silently skipping it would let one surface under-report a criterion the
    other enforces."""
    value = point.get(name)
    if value is None:
        raise ValueError(
            f"validated_mvp {kind} references metric '{name}', which has no value in "
            f"metrics (un-evaluable {kind}; emit the metric or drop the threshold)"
        )
    return float(value)


def unmet_validated_mvp_reasons(
    point: Mapping[str, float],
    floors: Mapping[str, float],
    ceilings: Mapping[str, float],
    *,
    uncertainty: Mapping[str, dict] | None,
    evidence: Mapping[str, object] | None,
) -> list[str]:
    """The reasons ``validated_mvp`` is NOT earned — empty list iff it IS earned.

    THE single source of truth shared by the trainer status decision and the M4
    inference limitations, so they can never diverge. Applies, in order: the
    min-evidence gate, the require-a-CI rule, then the CI-lower-bound floor check and
    CI-upper-bound ceiling check (same floor/ceiling VALUES, stronger evidence). The
    point value of every listed floor/ceiling metric must be present (else raises).
    """
    reasons: list[str] = []

    # (1) minimum-evidence gate — sample-aware, independent of point metrics.
    if evidence is None:
        reasons.append(
            "no sample-evidence summary (per-fold support not recorded); validated_mvp "
            "requires recorded fold support"
        )
    else:
        n_pos = evidence.get("n_positives_total")
        mpf = evidence.get("min_positives_per_fold")
        if n_pos is None or n_pos < MIN_POS_TOTAL:
            reasons.append(f"{n_pos} positives < {MIN_POS_TOTAL} minimum for validated_mvp")
        if mpf is None or mpf < MIN_POS_PER_FOLD:
            reasons.append(
                f"min positives/fold {mpf} < {MIN_POS_PER_FOLD} minimum for validated_mvp"
            )

    # (2) a CI is required to evidence the floors under uncertainty.
    have_ci = uncertainty is not None
    if not have_ci:
        reasons.append(
            "no CI evidence / no fold support — validation predates confidence-interval "
            "evidence; validated_mvp requires a bootstrap CI"
        )

    # (3) floors: the CI LOWER bound must clear the floor (point present is required).
    for name, thr in floors.items():
        value = _require_point(point, name, "floor")
        if have_ci:
            lo = (uncertainty.get(name) or {}).get("lo")
            if lo is None:
                reasons.append(f"{_label(name)} CI lower bound unavailable for the {thr:.2f} floor")
            elif lo < thr:
                reasons.append(f"{_label(name)} 95% CI lower bound {lo:.3f} < {thr:.2f} floor")
        elif value < thr:
            reasons.append(f"{_label(name)} {value:.3f} < {thr:.2f} floor")

    # (4) ceilings: the CI UPPER bound must clear the ceiling.
    for name, thr in ceilings.items():
        value = _require_point(point, name, "ceiling")
        if have_ci:
            hi = (uncertainty.get(name) or {}).get("hi")
            if hi is None:
                reasons.append(
                    f"{_label(name)} CI upper bound unavailable for the {thr:.2f} ceiling"
                )
            elif hi > thr:
                reasons.append(f"{_label(name)} 95% CI upper bound {hi:.3f} > {thr:.2f} ceiling")
        elif value > thr:
            reasons.append(f"{_label(name)} {value:.3f} > {thr:.2f} ceiling")

    return reasons
