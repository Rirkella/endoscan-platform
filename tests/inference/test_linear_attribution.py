"""Coefficient-based linear attribution (M7 explain extension).

Proves LinearCoefficientAttribution is the EXACT additive decomposition of a linear model's
logit (not an approximation), and that select_attribution_method routes by model type. Uses
small synthetic pipelines — no registry, no committed artifacts, no network.
"""

from __future__ import annotations

import numpy as np
import pytest
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from endoscan_core.inference import (
    LinearCoefficientAttribution,
    UnsupportedModelForExplanationError,
    select_attribution_method,
)


def _linear_pipeline(seed: int = 0) -> tuple[Pipeline, np.ndarray]:
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(60, 6))
    y = (X[:, 0] + 0.5 * X[:, 1] - X[:, 2] > 0).astype(int)
    model = Pipeline([("scaler", StandardScaler()), ("clf", LogisticRegression())]).fit(X, y)
    return model, X


def test_linear_coefficient_reconciles_exactly_to_the_logit() -> None:
    # base_value (intercept) + Σ contributions == the model's decision_function (the logit)
    # over the SAME standardized features the model consumes — exact, not approximate.
    model, X = _linear_pipeline()
    x = X[:5]
    values, base = LinearCoefficientAttribution().attribute(model, x)

    x_scaled = model[:-1].transform(x)
    logit = model.named_steps["clf"].decision_function(x_scaled)
    reconstructed = base + values.sum(axis=1)
    assert np.allclose(reconstructed, logit, atol=1e-9)
    # base_value is the honest logit baseline: the model intercept.
    assert base == pytest.approx(float(model.named_steps["clf"].intercept_[0]))
    assert values.shape == (5, 6)


def test_linear_attribution_type_label() -> None:
    assert LinearCoefficientAttribution().attribution_type == "linear_coefficient"


def test_select_attribution_method_routes_by_model_type() -> None:
    lin, _ = _linear_pipeline()
    rng = np.random.default_rng(1)
    X = rng.normal(size=(60, 6))
    y = (X[:, 0] > 0).astype(int)
    tree = Pipeline(
        [
            ("scaler", StandardScaler()),
            ("clf", RandomForestClassifier(n_estimators=5, random_state=0)),
        ]
    ).fit(X, y)

    assert select_attribution_method(lin).attribution_type == "linear_coefficient"
    assert select_attribution_method(tree).attribution_type == "tree_shap"

    class _Unsupported:  # neither coef_ nor tree attributes
        pass

    with pytest.raises(UnsupportedModelForExplanationError):
        select_attribution_method(_Unsupported())
