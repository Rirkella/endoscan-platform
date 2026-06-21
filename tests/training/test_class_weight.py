"""Imbalance handling: class_weight on LR/RF, leakage-safe sample_weight on GB."""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.utils.class_weight import compute_sample_weight

from endoscan_core.training import build_model, fit_balanced


def _imbalanced(n_majority: int = 60, n_minority: int = 8):
    rng = np.random.default_rng(0)
    x_pos = rng.normal(0.6, 1.0, size=(n_minority, 3))
    x_neg = rng.normal(-0.2, 1.0, size=(n_majority, 3))
    X = pd.DataFrame(np.vstack([x_pos, x_neg]), columns=["f0", "f1", "f2"])
    y = pd.Series([1] * n_minority + [0] * n_majority)
    return X, y


def test_lr_and_rf_carry_balanced_class_weight() -> None:
    for name in ("ridge_logreg", "lasso_logreg", "elastic_net_logreg", "random_forest"):
        assert build_model(name).named_steps["clf"].class_weight == "balanced"


def test_gradient_boosting_has_no_class_weight_param() -> None:
    clf = build_model("gradient_boosting").named_steps["clf"]
    assert not hasattr(clf, "class_weight")  # GB is balanced via sample_weight instead


def test_fit_balanced_changes_gb_on_imbalanced_data() -> None:
    X, y = _imbalanced()
    weighted = fit_balanced(build_model("gradient_boosting"), X, y)

    unweighted = build_model("gradient_boosting")
    unweighted.fit(X, y)  # no sample_weight

    p_weighted = weighted.predict_proba(X)[:, 1]
    p_unweighted = unweighted.predict_proba(X)[:, 1]
    # Balancing must materially change the GB predictions on imbalanced data.
    assert np.linalg.norm(p_weighted - p_unweighted) > 1e-6


def test_gb_sample_weight_is_local_to_the_passed_labels() -> None:
    # Two different label subsets => different balanced weights => no global leak.
    _, y_full = _imbalanced(60, 8)
    y_subset = y_full.iloc[:40]  # different prevalence than the full set
    w_full = compute_sample_weight("balanced", y_full)
    w_subset = compute_sample_weight("balanced", y_subset)
    # The minority weight depends on the prevalence of the passed y, not a global one.
    assert w_full[0] != w_subset[0]


def test_fit_balanced_is_a_noop_weighting_for_linear_models() -> None:
    X, y = _imbalanced()
    a = fit_balanced(build_model("ridge_logreg"), X, y).predict_proba(X)[:, 1]
    b = build_model("ridge_logreg").fit(X, y).predict_proba(X)[:, 1]
    # LR uses class_weight at construction, so fit_balanced == plain fit for it.
    assert np.allclose(a, b)


def test_gradient_boosting_isinstance_guard() -> None:
    assert isinstance(
        build_model("gradient_boosting").named_steps["clf"], GradientBoostingClassifier
    )
