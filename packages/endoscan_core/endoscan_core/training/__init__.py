"""Endpoint training and evaluation (compound-level CV, two model types)."""

from .evaluate_endpoint import ConfusionMatrix, EvalMetrics, evaluate_endpoint
from .splits import grouped_cv_splits, n_splits_for_groups
from .train_endpoint import (
    MODEL_NAMES,
    SEED,
    CandidateModelResult,
    TrainResult,
    build_model,
    train_endpoint,
)

__all__ = [
    "ConfusionMatrix",
    "EvalMetrics",
    "evaluate_endpoint",
    "grouped_cv_splits",
    "n_splits_for_groups",
    "MODEL_NAMES",
    "SEED",
    "CandidateModelResult",
    "TrainResult",
    "build_model",
    "train_endpoint",
]
