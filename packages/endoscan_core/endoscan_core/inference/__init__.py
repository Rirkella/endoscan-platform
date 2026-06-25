"""Inference + explainability for registered endpoints (read-only; loads, never fits).

Validate a transcriptomic signature against the endpoint's feature schema, predict ER
functional modulation, and (with the ``explain`` extra) attribute the prediction to
landmark genes via TreeSHAP — every output carrying the endpoint's limitations block.
"""

from __future__ import annotations

from .explain import (
    AttributionMethod,
    ExplanationResult,
    GeneAttribution,
    TreeSHAPAttribution,
    explain,
)
from .limitations import LimitationsBlock, build_limitations, missed_criteria
from .predict import (
    ModelArtifactUnavailableError,
    PredictionResult,
    endpoint_entry,
    load_endpoint_model,
    predict,
    predict_batch,
)
from .schema_validation import (
    SignatureValidationError,
    align_signature,
    load_feature_schema,
    validate_signature,
)

__all__ = [
    "AttributionMethod",
    "ExplanationResult",
    "GeneAttribution",
    "LimitationsBlock",
    "ModelArtifactUnavailableError",
    "PredictionResult",
    "SignatureValidationError",
    "TreeSHAPAttribution",
    "align_signature",
    "build_limitations",
    "endpoint_entry",
    "explain",
    "load_endpoint_model",
    "load_feature_schema",
    "missed_criteria",
    "predict",
    "predict_batch",
    "validate_signature",
]
