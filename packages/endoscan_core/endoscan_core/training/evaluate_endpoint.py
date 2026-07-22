"""Evaluation metrics for endpoint models.

Computed from pooled out-of-fold predictions (the honest cross-validated
estimate). Calibration is reported as the Brier score — a proper scoring rule —
with no separate calibrated-model fit in the current training path.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict
from sklearn.metrics import (
    average_precision_score,
    balanced_accuracy_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    roc_auc_score,
)


class ConfusionMatrix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tn: int
    fp: int
    fn: int
    tp: int


class EvalMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auroc: float
    auprc: float
    balanced_accuracy: float
    f1: float
    brier_score: float
    confusion_matrix: ConfusionMatrix
    n_samples: int
    threshold: float = 0.5


def evaluate_endpoint(
    y_true: Sequence[int], y_prob: Sequence[float], threshold: float = 0.5
) -> EvalMetrics:
    """Compute classification + calibration metrics from probabilities."""
    y_true_arr = np.asarray(y_true).astype(int)
    y_prob_arr = np.asarray(y_prob, dtype=float)
    y_pred = (y_prob_arr >= threshold).astype(int)

    tn, fp, fn, tp = confusion_matrix(y_true_arr, y_pred, labels=[0, 1]).ravel()
    return EvalMetrics(
        auroc=float(roc_auc_score(y_true_arr, y_prob_arr)),
        auprc=float(average_precision_score(y_true_arr, y_prob_arr)),
        balanced_accuracy=float(balanced_accuracy_score(y_true_arr, y_pred)),
        f1=float(f1_score(y_true_arr, y_pred, zero_division=0)),
        brier_score=float(brier_score_loss(y_true_arr, y_prob_arr)),
        confusion_matrix=ConfusionMatrix(tn=int(tn), fp=int(fp), fn=int(fn), tp=int(tp)),
        n_samples=int(len(y_true_arr)),
        threshold=threshold,
    )
