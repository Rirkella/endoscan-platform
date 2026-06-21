"""Model scorecard and simplicity-aware selection.

Scores every candidate model on a compound-level grouped-CV scorecard (AUROC,
AUPRC, balanced accuracy, calibration/Brier, fold-stability, and measured
train/inference cost) plus static interpretability/deployment-simplicity tiers.

Selection rule: among candidates within a configured tolerance of the best on the
primary metrics (AUROC and AUPRC), prefer the simplest / most interpretable model
(by static tiers, then name for determinism). Measured cost is **reported** for
human review but is NOT used in the automated tie-break (it is wall-time and
therefore non-deterministic); the final pick is human-approved.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from time import perf_counter

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from .evaluate_endpoint import evaluate_endpoint
from .splits import grouped_cv_splits, n_splits_for_groups
from .train_endpoint import build_model, fit_balanced, model_tiers

# Primary metrics used to define "near-best" candidates.
PRIMARY_METRICS = ("auroc", "auprc")
DEFAULT_TOLERANCE = 0.02


@dataclass
class ModelScore:
    name: str
    auroc: float
    auprc: float
    balanced_accuracy: float
    brier_score: float
    auroc_fold_std: float  # stability across compound-level folds
    train_seconds: float
    infer_seconds: float
    interpretability_tier: int
    simplicity_tier: int


def _score_one(
    name: str,
    X: pd.DataFrame,
    y: pd.Series,
    folds: list[tuple[np.ndarray, np.ndarray]],
    seed: int,
) -> ModelScore:
    oof = np.full(len(y), np.nan)
    fold_aurocs: list[float] = []
    train_seconds = 0.0
    infer_seconds = 0.0

    for train_idx, test_idx in folds:
        model = build_model(name, seed=seed)
        start = perf_counter()
        # Imbalance handled per-fit from the fold's TRAINING labels (no leakage).
        fit_balanced(model, X.iloc[train_idx], y.iloc[train_idx])
        train_seconds += perf_counter() - start

        start = perf_counter()
        proba = model.predict_proba(X.iloc[test_idx])[:, 1]
        infer_seconds += perf_counter() - start
        oof[test_idx] = proba

        y_fold = y.iloc[test_idx]
        if len(set(y_fold)) == 2:  # AUROC needs both classes in the fold
            fold_aurocs.append(float(roc_auc_score(y_fold, proba)))

    metrics = evaluate_endpoint(y, oof)
    interp, simplicity = model_tiers(name)
    return ModelScore(
        name=name,
        auroc=metrics.auroc,
        auprc=metrics.auprc,
        balanced_accuracy=metrics.balanced_accuracy,
        brier_score=metrics.brier_score,
        auroc_fold_std=float(np.std(fold_aurocs)) if len(fold_aurocs) > 1 else 0.0,
        train_seconds=train_seconds,
        infer_seconds=infer_seconds,
        interpretability_tier=interp,
        simplicity_tier=simplicity,
    )


def score_candidates(
    X: pd.DataFrame,
    y: pd.Series,
    groups: Sequence[int],
    model_names: Sequence[str],
    n_splits: int,
    seed: int,
) -> list[ModelScore]:
    """Compute a grouped-CV scorecard for every candidate model."""
    folds = grouped_cv_splits(list(groups), n_splits_for_groups(groups, n_splits))
    return [_score_one(name, X, y, folds, seed) for name in model_names]


def select_model(
    scores: Sequence[ModelScore],
    tolerance: float = DEFAULT_TOLERANCE,
) -> str:
    """Pick the simplest model within ``tolerance`` of the best on primary metrics.

    A candidate is eligible if it is within ``tolerance`` of the best observed
    AUROC AND the best observed AUPRC. Among the eligible, the winner is the one
    with the lowest (interpretability_tier, simplicity_tier, name).
    """
    if not scores:
        raise ValueError("No candidate scores to select from")
    best_auroc = max(s.auroc for s in scores)
    best_auprc = max(s.auprc for s in scores)
    eligible = [
        s for s in scores if s.auroc >= best_auroc - tolerance and s.auprc >= best_auprc - tolerance
    ]
    chosen = min(
        eligible,
        key=lambda s: (s.interpretability_tier, s.simplicity_tier, s.name),
    )
    return chosen.name


def selection_report(
    scores: Sequence[ModelScore],
    selected: str,
    tolerance: float,
    *,
    extra: dict | None = None,
) -> dict:
    """A JSON-serializable report ranking candidates with the selection rationale."""
    ranked = sorted(scores, key=lambda s: (-s.auroc, -s.auprc, s.name))
    report = {
        "selected_model": selected,
        "tolerance": tolerance,
        "primary_metrics": list(PRIMARY_METRICS),
        "rule": (
            "Among candidates within `tolerance` of the best AUROC and AUPRC, "
            "prefer the lowest (interpretability_tier, simplicity_tier). Measured "
            "cost is reported but not used in the automated tie-break. Final pick "
            "is human-approved."
        ),
        "candidates": [asdict(s) for s in ranked],
    }
    if extra:
        report.update(extra)
    return report


def render_selection_markdown(report: dict) -> str:
    """Render the selection report as a small Markdown table."""
    lines = [
        "# Model Selection Report",
        "",
        f"- **Selected model:** {report['selected_model']}",
        f"- **Tolerance (primary metrics):** {report['tolerance']}",
        f"- **Rule:** {report['rule']}",
        "",
        "| model | AUROC | AUPRC | bal_acc | Brier | AUROC_fold_std "
        "| interp_tier | simplicity_tier |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in report["candidates"]:
        lines.append(
            f"| {c['name']} | {c['auroc']:.3f} | {c['auprc']:.3f} | "
            f"{c['balanced_accuracy']:.3f} | {c['brier_score']:.3f} | "
            f"{c['auroc_fold_std']:.3f} | {c['interpretability_tier']} | "
            f"{c['simplicity_tier']} |"
        )
    if "nested_per_fold_selected" in report:
        lines += ["", f"Nested per-fold selections: {report['nested_per_fold_selected']}"]
    lines.append("")
    return "\n".join(lines)
