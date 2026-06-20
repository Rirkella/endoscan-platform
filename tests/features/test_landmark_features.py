"""FeatureSchema captures gene-column order from the candidate table's X."""

from __future__ import annotations

import pandas as pd

from endoscan_core.features import build_feature_schema


def test_build_feature_schema_records_column_order() -> None:
    X = pd.DataFrame({"GENE_A": [0.1], "GENE_B": [0.2], "GENE_C": [0.3]})
    schema = build_feature_schema(X)
    assert schema.features == ["GENE_A", "GENE_B", "GENE_C"]
    assert schema.standardized is True
    assert schema.schema_version == "0.1"
