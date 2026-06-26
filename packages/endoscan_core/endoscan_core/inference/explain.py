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
    shap_value: float
    direction: str  # "toward" (pushes toward ER-modulation) | "away"


class ExplanationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: str
    base_value: float
    n_features: int
    top_contributors: list[GeneAttribution]
    limitations: LimitationsBlock  # REQUIRED — no explanation without it


@runtime_checkable
class AttributionMethod(Protocol):
    """Compute per-feature signed attributions (positive class) for an aligned input.

    Returns ``(values, base_value)`` where ``values`` has shape ``(n_signatures,
    n_features)`` in the model's feature order and ``base_value`` is the explainer's
    expected value for the positive class. A future ``CoefficientAttribution`` (signed
    ``coef_ * feature`` for a linear endpoint) implements the same protocol.
    """

    def attribute(self, model: Any, X: np.ndarray) -> tuple[np.ndarray, float]: ...


class TreeSHAPAttribution:
    """Exact TreeSHAP attributions for a tree model inside a scaler+clf pipeline."""

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

    Returns one ``ExplanationResult`` for a single signature or a list for a batch.
    """
    root, entry, schema, metrics = _resolve(endpoint_id, repo_root)
    X, is_single = align_signature(signature, schema, allow_extra=allow_extra)
    model = load_endpoint_model(endpoint_id, repo_root=root)

    method = method or TreeSHAPAttribution()
    values, base = method.attribute(model, X)
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
                base_value=base,
                n_features=len(features),
                top_contributors=contributors,
                limitations=limitations,
            )
        )
    return results[0] if is_single else results
