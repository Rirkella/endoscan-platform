"""train_endpoint: selects one of the two models, deterministically."""

from __future__ import annotations

from endoscan_core.datasets import CandidateTable
from endoscan_core.training import MODEL_NAMES, train_endpoint


def test_selects_a_model_and_refits(candidate_table: CandidateTable) -> None:
    groups = candidate_table.metadata["split_group"].tolist()
    result = train_endpoint(
        candidate_table.X, candidate_table.y, groups, model_names=list(MODEL_NAMES)
    )
    assert result.selected_model_name in MODEL_NAMES
    assert result.n_splits == 2
    assert len(result.candidates) == len(MODEL_NAMES)
    # The selected pipeline is refit on all rows and can predict.
    assert hasattr(result.fitted_model, "predict_proba")
    assert result.fitted_model.predict_proba(candidate_table.X).shape == (len(candidate_table.y), 2)


def test_training_is_deterministic(candidate_table: CandidateTable) -> None:
    groups = candidate_table.metadata["split_group"].tolist()
    r1 = train_endpoint(candidate_table.X, candidate_table.y, groups, model_names=list(MODEL_NAMES))
    r2 = train_endpoint(candidate_table.X, candidate_table.y, groups, model_names=list(MODEL_NAMES))
    assert r1.selected_model_name == r2.selected_model_name
    assert r1.metrics.auroc == r2.metrics.auroc
    assert r1.metrics.brier_score == r2.metrics.brier_score
