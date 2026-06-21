"""Guard against a silent penalty collapse.

build_model passes ``l1_ratio`` with NO ``penalty=`` argument, which only selects
L1/elastic-net on scikit-learn >= 1.8 (pinned in the lock). On older scikit-learn
``l1_ratio`` is ignored unless ``penalty="elasticnet"``, so lasso/elastic-net would
silently become ridge (L2). This test fits the three LR variants on a fixture with
many redundant/noise features and fails loudly if that ever happens.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from endoscan_core.training.train_endpoint import build_model

NEAR_ZERO = 1e-8


def _coefs(name: str, X: pd.DataFrame, y: pd.Series) -> np.ndarray:
    model = build_model(name, seed=0)
    model.fit(X, y)
    return np.ravel(model.named_steps["clf"].coef_)


def _dataset() -> tuple[pd.DataFrame, pd.Series]:
    rng = np.random.default_rng(0)
    n = 300
    x1 = rng.normal(size=n)
    x2 = rng.normal(size=n)
    y = ((x1 + x2 + 0.25 * rng.normal(size=n)) > 0).astype(int)
    # 2 informative features + 18 pure-noise features that L1 should drive to zero.
    data = {"x1": x1, "x2": x2}
    for k in range(18):
        data[f"noise{k}"] = rng.normal(size=n)
    return pd.DataFrame(data), pd.Series(y)


def test_lasso_is_sparser_than_ridge_and_penalties_differ() -> None:
    X, y = _dataset()
    ridge = _coefs("ridge_logreg", X, y)
    lasso = _coefs("lasso_logreg", X, y)
    elastic = _coefs("elastic_net_logreg", X, y)

    ridge_zeros = int(np.sum(np.abs(ridge) < NEAR_ZERO))
    lasso_zeros = int(np.sum(np.abs(lasso) < NEAR_ZERO))

    # L1 must zero strictly more coefficients than L2 (which zeros ~none).
    assert lasso_zeros > ridge_zeros

    # The three penalties must yield genuinely different coefficient vectors.
    assert np.linalg.norm(ridge - lasso) > 1e-3
    assert np.linalg.norm(ridge - elastic) > 1e-3
    assert np.linalg.norm(lasso - elastic) > 1e-3
