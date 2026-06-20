"""Transcriptomic landmark-feature schema (transcriptomics-only, no SMILES).

At M3 the features are the gene columns of the M2 candidate table's ``X``. This
module captures the gene-column order (the contract M4 inference will align an
incoming signature to) and records that standardization is applied. The
standardization itself lives inside the training `Pipeline` (`StandardScaler`),
so it is saved with the model and reapplied at inference time.
"""

from __future__ import annotations

import pandas as pd
from pydantic import BaseModel, ConfigDict


class FeatureSchema(BaseModel):
    """The ordered landmark-gene feature set for an endpoint model."""

    model_config = ConfigDict(extra="forbid")

    features: list[str]
    standardized: bool = True
    schema_version: str = "0.1"


def build_feature_schema(
    X: pd.DataFrame, *, standardized: bool = True, schema_version: str = "0.1"
) -> FeatureSchema:
    """Capture the gene-column order from a candidate table's ``X``."""
    return FeatureSchema(
        features=list(X.columns),
        standardized=standardized,
        schema_version=schema_version,
    )
