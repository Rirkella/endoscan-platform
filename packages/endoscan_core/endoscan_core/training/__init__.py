"""Endpoint training, model selection, and compound-level evaluation."""

from .evaluate_endpoint import ConfusionMatrix, EvalMetrics, evaluate_endpoint
from .model_selection import (
    DEFAULT_TOLERANCE,
    ModelScore,
    render_selection_markdown,
    score_candidates,
    select_model,
    selection_report,
)
from .nested_cv import (
    HoldoutResult,
    NestedCVResult,
    NestedFoldResult,
    holdout_group_eval,
    nested_group_cv,
)
from .splits import grouped_cv_splits, n_splits_for_groups
from .train_endpoint import (
    MODEL_NAMES,
    SEED,
    CandidateModelResult,
    TrainResult,
    build_model,
    fit_balanced,
    model_tiers,
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
    "fit_balanced",
    "model_tiers",
    "train_endpoint",
    "ModelScore",
    "score_candidates",
    "select_model",
    "selection_report",
    "render_selection_markdown",
    "DEFAULT_TOLERANCE",
    "NestedCVResult",
    "NestedFoldResult",
    "HoldoutResult",
    "nested_group_cv",
    "holdout_group_eval",
]
