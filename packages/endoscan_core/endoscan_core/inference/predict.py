"""Load a registered endpoint and predict ER functional modulation from a signature.

Read-only: the endpoint is loaded via the M1 registry store and never fitted. The
input is a transcriptomic signature (validated + aligned to the endpoint's feature
schema); the output carries the probability, the thresholded call, and the endpoint's
limitations block — no bare score is ever returned.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..registry.schema import EndpointEntry
from ..registry.store import RegistryError, find_repo_root, get_endpoint, load_model
from .limitations import LimitationsBlock, build_limitations
from .schema_validation import align_signature, load_feature_schema


class ModelArtifactUnavailableError(RegistryError):
    """The endpoint's model binary is not present locally (e.g. a DVC pointer not pulled)."""


class PredictionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: str
    probability: float
    call: bool
    threshold: float
    standardized_input: bool
    limitations: LimitationsBlock  # REQUIRED — no result can exist without it


def _resolve(endpoint_id: str, repo_root: Path | None):
    root = repo_root or find_repo_root()
    entry = get_endpoint(endpoint_id, repo_root=root)
    schema = load_feature_schema(root / entry.feature_schema_path)
    metrics = json.loads((root / entry.metrics_path).read_text(encoding="utf-8"))
    return root, entry, schema, metrics


def load_endpoint_model(endpoint_id: str, repo_root: Path | None = None) -> Any:
    """Load the registered model, with a clear error when the binary is DVC-absent.

    The model is the TRUSTED, registered artifact (operator-produced, reviewed) — never
    a user-supplied pickle. If ``model.pkl`` is missing but a sibling ``model.pkl.dvc``
    pointer exists, raise an actionable ``ModelArtifactUnavailableError`` (run
    ``dvc pull``) rather than a bare FileNotFound crash.
    """
    root = repo_root or find_repo_root()
    entry = get_endpoint(endpoint_id, repo_root=root)
    model_file = root / entry.model_path
    if not model_file.is_file():
        pointer = model_file.with_name(model_file.name + ".dvc")
        if pointer.is_file():
            raise ModelArtifactUnavailableError(
                f"model artifact for {endpoint_id!r} is DVC-tracked and not present "
                f"locally ({entry.model_path}). Run `dvc pull {entry.model_path}` to "
                "fetch it, then retry."
            )
        raise ModelArtifactUnavailableError(
            f"model artifact for {endpoint_id!r} not found at {model_file}"
        )
    return load_model(endpoint_id, repo_root=root)


def _threshold(metrics: dict) -> float:
    # Source: metrics.json["threshold"]; default 0.5 when absent/null.
    value = metrics.get("threshold")
    return float(value) if value is not None else 0.5


def predict(
    endpoint_id: str,
    signature,
    *,
    repo_root: Path | None = None,
    allow_extra: bool = False,
) -> PredictionResult | list[PredictionResult]:
    """Predict P(ER functional modulation) for one signature (or a batch).

    Returns a single ``PredictionResult`` for a dict/Series input, or a list for a
    DataFrame batch. Every result carries the endpoint's limitations block.
    """
    root, entry, schema, metrics = _resolve(endpoint_id, repo_root)
    X, is_single = align_signature(signature, schema, allow_extra=allow_extra)
    model = load_endpoint_model(endpoint_id, repo_root=root)
    proba = model.predict_proba(X)[:, 1]
    threshold = _threshold(metrics)
    limitations = build_limitations(entry, metrics)

    results = [
        PredictionResult(
            endpoint_id=entry.endpoint_id,
            probability=float(p),
            call=bool(p >= threshold),
            threshold=threshold,
            standardized_input=bool(schema.standardized),
            limitations=limitations,
        )
        for p in proba
    ]
    return results[0] if is_single else results


def predict_batch(
    endpoint_id: str, signatures, *, repo_root: Path | None = None, allow_extra: bool = False
) -> list[PredictionResult]:
    """Batch form: always returns a list (DataFrame of gene columns, one row per signature)."""
    out = predict(endpoint_id, signatures, repo_root=repo_root, allow_extra=allow_extra)
    return out if isinstance(out, list) else [out]


def endpoint_entry(endpoint_id: str, repo_root: Path | None = None) -> EndpointEntry:
    """Convenience: the registered ``EndpointEntry`` (metadata only, no model load)."""
    return get_endpoint(endpoint_id, repo_root=repo_root or find_repo_root())
