"""Condition selection (10uM/24h + fallback) and MCF7/A549 early fusion."""

from __future__ import annotations

import condition  # noqa: E402 — resolved via conftest sys.path
import pandas as pd


def test_prefers_10um_24h_then_falls_back_to_nearest() -> None:
    df = pd.DataFrame(
        [
            {
                "compound_key": "K1",
                "sig_id": "a",
                "cell_id": "MCF7",
                "pert_dose": 10.0,
                "pert_time": 24.0,
            },
            {
                "compound_key": "K1",
                "sig_id": "b",
                "cell_id": "MCF7",
                "pert_dose": 1.0,
                "pert_time": 6.0,
            },
            {
                "compound_key": "K1",
                "sig_id": "c",
                "cell_id": "A549",
                "pert_dose": 5.0,
                "pert_time": 24.0,
            },
        ]
    )
    chosen = condition.select_condition_signatures(df)
    got = dict(zip(chosen["cell_id"], chosen["sig_id"], strict=True))
    assert got["MCF7"] == "a"  # exact 10uM/24h preferred over 1uM/6h
    assert got["A549"] == "c"  # only option -> kept (compound not dropped)


def test_early_fuse_means_cell_lines_into_one_vector() -> None:
    selected = pd.DataFrame(
        [
            {"compound_key": "K1", "cell_id": "MCF7", "GENE_A": 2.0, "GENE_B": 4.0},
            {"compound_key": "K1", "cell_id": "A549", "GENE_A": 4.0, "GENE_B": 8.0},
        ]
    )
    fused = condition.early_fuse(selected, ["GENE_A", "GENE_B"])
    assert len(fused) == 1
    row = fused.iloc[0]
    assert row["GENE_A"] == 3.0
    assert row["GENE_B"] == 6.0
