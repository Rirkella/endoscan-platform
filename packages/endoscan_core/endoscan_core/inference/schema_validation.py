"""Validate an input transcriptomic signature against an endpoint's feature schema.

This is the contract that prevents silently feeding a misaligned/garbage vector to a
model. A signature is a value over the endpoint's landmark gene SYMBOLS; validation
checks every schema gene is present, rejects extras (by default), aligns to the
schema's exact feature order, and rejects wrong dimensionality / NaN. No re-standardization
is done here — the model's own ``StandardScaler`` step handles that; the schema's
``standardized`` flag is recorded, not acted on.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pandas as pd

from ..features.landmark_features import FeatureSchema


class SignatureValidationError(ValueError):
    """Raised when an input signature does not satisfy the endpoint's feature schema."""


def load_feature_schema(path: Path | str) -> FeatureSchema:
    """Load a ``FeatureSchema`` from an endpoint's ``feature_schema.json``."""
    return FeatureSchema.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def _check_mapping_keys(
    values: Mapping[str, float], features: list[str], *, allow_extra: bool
) -> None:
    keys = set(values)
    wanted = set(features)
    missing = [g for g in features if g not in keys]
    extra = sorted(keys - wanted)
    if missing:
        raise SignatureValidationError(
            f"signature missing {len(missing)} of {len(features)} schema genes "
            f"(first few: {missing[:5]})"
        )
    if extra and not allow_extra:
        raise SignatureValidationError(
            f"signature has {len(extra)} gene(s) not in the schema "
            f"(first few: {extra[:5]}); pass allow_extra=True to drop them"
        )


def align_signature(
    signature: Mapping[str, float] | pd.Series | pd.DataFrame,
    schema: FeatureSchema,
    *,
    allow_extra: bool = False,
) -> tuple[np.ndarray, bool]:
    """Align ``signature`` to ``schema.features`` order; return ``(X_2d, is_single)``.

    Accepts a ``dict[str, float]`` / ``pd.Series`` (one signature) or a ``pd.DataFrame``
    with gene-symbol columns (a batch, one row per signature). Returns a 2-D float array
    of shape ``(n_signatures, n_features)`` in the schema's exact feature order, plus a
    flag for whether the caller passed a single signature.
    """
    features = list(schema.features)

    if isinstance(signature, pd.DataFrame):
        cols = set(signature.columns)
        missing = [g for g in features if g not in cols]
        extra = sorted(cols - set(features))
        if missing:
            raise SignatureValidationError(
                f"signature DataFrame missing {len(missing)} schema genes "
                f"(first few: {missing[:5]})"
            )
        if extra and not allow_extra:
            raise SignatureValidationError(
                f"signature DataFrame has {len(extra)} non-schema column(s) "
                f"(first few: {extra[:5]}); pass allow_extra=True to drop them"
            )
        frame = signature[features]
        is_single = False
    else:
        if isinstance(signature, pd.Series):
            mapping = signature.to_dict()
        elif isinstance(signature, Mapping):
            mapping = dict(signature)
        else:
            raise SignatureValidationError(
                "signature must be a dict[str, float], pandas Series (gene-indexed), or "
                f"DataFrame (gene columns); got {type(signature).__name__}"
            )
        _check_mapping_keys(mapping, features, allow_extra=allow_extra)
        frame = pd.DataFrame([{g: mapping[g] for g in features}], columns=features)
        is_single = True

    X = frame.to_numpy(dtype=float)
    if X.shape[1] != len(features):
        raise SignatureValidationError(
            f"aligned signature has {X.shape[1]} features, expected {len(features)}"
        )
    if not np.isfinite(X).all():
        raise SignatureValidationError(
            "signature contains NaN/inf values; all genes must be finite numbers"
        )
    return X, is_single


def validate_signature(
    signature: Mapping[str, float] | pd.Series | pd.DataFrame,
    schema: FeatureSchema,
    *,
    allow_extra: bool = False,
) -> np.ndarray:
    """Validate + align a single signature; return the 1-D aligned vector.

    Convenience wrapper over :func:`align_signature` for the single-signature case.
    Raises ``SignatureValidationError`` for a batch input.
    """
    X, is_single = align_signature(signature, schema, allow_extra=allow_extra)
    if not is_single:
        raise SignatureValidationError(
            "validate_signature expects ONE signature; use align_signature for a batch"
        )
    return X[0]
