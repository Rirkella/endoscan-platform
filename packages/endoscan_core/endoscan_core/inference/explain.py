"""Feature attributions for a registered endpoint's prediction.

TreeSHAP (exact for trees) attributes each landmark gene's signed contribution to the
predicted probability of ER functional modulation. The registered model is an sklearn
``Pipeline([StandardScaler, RandomForestClassifier])``; SHAP runs on the RF step over
the SCALED features (the RF's actual inputs), and because the per-feature scaler is
monotonic the per-gene sign/ranking is meaningful. Values map 1:1 back to gene SYMBOLS
via the endpoint's ``feature_schema.json`` (index -> symbol).

``shap`` is an OPTIONAL dependency (the ``explain`` extra / dev group); it is imported
LAZILY inside :class:`TreeSHAPAttribution`, so importing this module — or core — never
requires shap. The ``AttributionMethod`` protocol lets a future LINEAR endpoint plug in
coefficient-based attribution without changing the prediction/limitations paths; only
the TreeSHAP path is implemented here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np
from pydantic import BaseModel, ConfigDict

from .limitations import LimitationsBlock, build_limitations
from .predict import _resolve, load_endpoint_model
from .schema_validation import align_signature


class GeneAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gene: str
    #: Signed per-gene contribution to the positive class. NOTE: the field is named
    #: ``shap_value`` for historical reasons, but its MEANING depends on the attribution
    #: method (see ``ExplanationResult.method``): for ``tree_shap`` it is a TreeSHAP value;
    #: for ``linear_coefficient`` it is ``coef_i * standardized_x_i`` — a coefficient×value
    #: contribution, NOT a SHAP value.
    shap_value: float
    direction: str  # "toward" (pushes toward modulation) | "away"


class ExplanationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: str
    #: The attribution mechanism that produced ``top_contributors`` / ``shap_value``:
    #: ``"tree_shap"`` (TreeSHAP, tree models) or ``"linear_coefficient"`` (coef×value, linear
    #: models). Explicit so a consumer never mistakes a linear contribution for a SHAP value.
    method: str
    base_value: float
    n_features: int
    top_contributors: list[GeneAttribution]
    limitations: LimitationsBlock  # REQUIRED — no explanation without it


class UnsupportedModelForExplanationError(Exception):
    """No implemented attributor supports this model type (neither tree nor linear coef_)."""


@runtime_checkable
class AttributionMethod(Protocol):
    """Compute per-feature signed attributions (positive class) for an aligned input.

    Returns ``(values, base_value)`` where ``values`` has shape ``(n_signatures,
    n_features)`` in the model's feature order and ``base_value`` is the positive-class
    baseline (SHAP expected value for trees; the model intercept for linear models).
    ``attribution_type`` labels the mechanism ("tree_shap" | "linear_coefficient").
    """

    attribution_type: str

    def attribute(self, model: Any, X: np.ndarray) -> tuple[np.ndarray, float]: ...


def _final_estimator(model: Any) -> Any:
    """The last pipeline step (the classifier), or the model itself if it is bare."""
    if hasattr(model, "named_steps") and "clf" in model.named_steps:
        return model.named_steps["clf"]
    if hasattr(model, "steps"):
        return model.steps[-1][1]
    return model


def _preprocessor_transform(model: Any, X: np.ndarray) -> np.ndarray:
    """Apply all-but-last pipeline steps (e.g. StandardScaler); identity for a bare model."""
    if hasattr(model, "steps"):
        return np.asarray(model[:-1].transform(X))
    return np.asarray(X)


class TreeSHAPAttribution:
    """Exact TreeSHAP attributions for a tree model inside a scaler+clf pipeline."""

    attribution_type = "tree_shap"

    def attribute(self, model: Any, X: np.ndarray) -> tuple[np.ndarray, float]:
        import shap  # lazy: only needed when explaining; not a core dependency

        # Pipeline([... , ("clf", tree_model)]): scale with all-but-last, explain the clf.
        pre = model[:-1]
        clf = model.named_steps["clf"] if hasattr(model, "named_steps") else model.steps[-1][1]
        x_scaled = pre.transform(X)

        explainer = shap.TreeExplainer(clf)
        sv = explainer.shap_values(x_scaled)
        ev = explainer.expected_value

        # Normalize to the positive (class-1) attributions, shape (n, n_features).
        if isinstance(sv, list):  # legacy: list per class
            values = np.asarray(sv[1])
            base = float(np.asarray(ev).ravel()[1])
        else:
            sv = np.asarray(sv)
            if sv.ndim == 3:  # (n, features, classes)
                values = sv[:, :, 1]
                base = float(np.asarray(ev).ravel()[1])
            else:  # (n, features) single output
                values = sv
                ev_arr = np.asarray(ev).ravel()
                base = float(ev_arr[-1])
        return values, base


class LinearCoefficientAttribution:
    """Exact additive attribution for a LINEAR model inside a scaler+clf pipeline.

    Each gene's contribution is ``coef_i * x_scaled_i`` over the SAME standardized features
    the model consumes (the pipeline's StandardScaler output). This is the exact additive
    decomposition of the model's logit: ``logit = intercept + Σ coef_i·x_i``. It is NOT a
    SHAP value — the ``ExplanationResult.method`` label ("linear_coefficient") makes that
    explicit. Applies to ``coef_``-bearing linear models (LogisticRegression / ridge_logreg,
    RidgeClassifier, linear SVM). Multiclass ``coef_`` uses the positive class (row -1).
    """

    attribution_type = "linear_coefficient"

    def attribute(self, model: Any, X: np.ndarray) -> tuple[np.ndarray, float]:
        clf = _final_estimator(model)
        x_scaled = _preprocessor_transform(model, X)  # the vector the linear model consumes
        coef = np.asarray(clf.coef_)
        # Binary LogisticRegression: coef_ is (1, n_features); use the positive-class row.
        w = coef[-1] if coef.ndim == 2 else coef
        contributions = x_scaled * w  # broadcast -> (n_signatures, n_features)
        intercept = np.asarray(clf.intercept_).ravel()
        base = float(intercept[-1] if intercept.size else 0.0)  # logit baseline
        return contributions, base


def select_attribution_method(model: Any) -> AttributionMethod:
    """Pick an attributor by model type: linear (``coef_``) -> LinearCoefficient; tree -> TreeSHAP.

    Raises ``UnsupportedModelForExplanationError`` for a model that fits neither (the caller
    maps that to a clean 501). Linear is checked first: linear models never have tree
    attributes, and TreeSHAP cannot explain them.
    """
    clf = _final_estimator(model)
    if hasattr(clf, "coef_"):
        return LinearCoefficientAttribution()
    if hasattr(clf, "estimators_") or hasattr(clf, "tree_"):
        return TreeSHAPAttribution()
    raise UnsupportedModelForExplanationError(
        f"no implemented attributor for model type {type(clf).__name__!r} "
        "(supported: tree ensembles via TreeSHAP; linear coef_ models via LinearCoefficient)"
    )


def explain(
    endpoint_id: str,
    signature,
    *,
    top_n: int = 10,
    repo_root: Path | None = None,
    method: AttributionMethod | None = None,
    allow_extra: bool = False,
) -> ExplanationResult | list[ExplanationResult]:
    """Explain a prediction: top-N signed gene contributors + the limitations block.

    The attributor is auto-selected by model type (tree -> TreeSHAP, linear -> coefficient)
    unless one is passed explicitly. Returns one ``ExplanationResult`` for a single signature
    or a list for a batch. Read-only: loads the registered model, never fits it.
    """
    root, entry, schema, metrics = _resolve(endpoint_id, repo_root)
    X, is_single = align_signature(signature, schema, allow_extra=allow_extra)
    model = load_endpoint_model(endpoint_id, repo_root=root)

    method = method or select_attribution_method(model)
    values, base = method.attribute(model, X)
    method_label = getattr(method, "attribution_type", "unknown")
    limitations = build_limitations(entry, metrics)
    features = list(schema.features)

    results: list[ExplanationResult] = []
    for row in np.atleast_2d(values):
        order = np.argsort(np.abs(row))[::-1][:top_n]
        contributors = [
            GeneAttribution(
                gene=features[i],
                shap_value=float(row[i]),
                direction="toward" if row[i] >= 0 else "away",
            )
            for i in order
        ]
        results.append(
            ExplanationResult(
                endpoint_id=entry.endpoint_id,
                method=method_label,
                base_value=base,
                n_features=len(features),
                top_contributors=contributors,
                limitations=limitations,
            )
        )
    return results[0] if is_single else results
