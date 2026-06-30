"""Lifted fusion transforms: drift-guard vs the frozen ER staging path + cell-line param."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest
from endoscan_jobs.fusion import (
    assemble_sig_meta,
    early_fuse,
    select_condition_signatures,
    select_landmark_genes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGING_CONDITION = REPO_ROOT / "pipelines" / "endpoints" / "ER" / "staging" / "condition.py"


def _staging_condition():
    """Load the frozen ER staging condition module by file path (no sys.path pollution)."""
    spec = importlib.util.spec_from_file_location("er_staging_condition", STAGING_CONDITION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _sig_meta() -> pd.DataFrame:
    # Two compounds; one with a preferred (10 uM/24 h) and a worse condition per cell.
    return pd.DataFrame(
        {
            "compound_key": ["IK1", "IK1", "IK1", "IK2"],
            "sig_id": ["S_A", "S_B", "S_C", "S_D"],
            "cell_id": ["MCF7", "MCF7", "A549", "A549"],
            "pert_dose": ["10", "1", "10", "10"],
            "pert_time": ["24", "24", "24", "24"],
            "geneX": [1.0, 9.0, 3.0, 5.0],
            "geneY": [2.0, 8.0, 4.0, 6.0],
        }
    )


def test_condition_selection_matches_frozen_er_staging() -> None:
    staging = _staging_condition()
    sig_meta = _sig_meta()
    lifted = select_condition_signatures(sig_meta)
    frozen = staging.select_condition_signatures(sig_meta)
    pd.testing.assert_frame_equal(
        lifted.reset_index(drop=True), frozen.reset_index(drop=True), check_like=True
    )
    # IK1/MCF7 keeps the 10 uM signature S_A, not the 1 uM S_B.
    chosen = lifted.set_index(["compound_key", "cell_id"])["sig_id"]
    assert chosen[("IK1", "MCF7")] == "S_A"


def test_early_fuse_matches_frozen_er_staging() -> None:
    staging = _staging_condition()
    chosen = select_condition_signatures(_sig_meta())
    features = ["geneX", "geneY"]
    lifted = early_fuse(chosen, features)
    frozen = staging.early_fuse(chosen, features)
    pd.testing.assert_frame_equal(
        lifted.reset_index(drop=True), frozen.reset_index(drop=True), check_like=True
    )
    # IK1 fuses MCF7(S_A) + A549(S_C): mean(geneX)=mean(1,3)=2.0.
    row = lifted.set_index("compound_key").loc["IK1"]
    assert row["geneX"] == pytest.approx(2.0)


def test_select_landmark_genes_filters_and_names() -> None:
    gene_info = pd.DataFrame(
        {
            "pr_gene_id": ["100", "200", "300"],
            "pr_gene_symbol": ["AAA", "BBB", "CCC"],
            "pr_is_lm": [1, 0, 1],
        }
    )
    gene_ids, symbols = select_landmark_genes(
        gene_info,
        landmark_flag_col="pr_is_lm",
        gene_id_col="pr_gene_id",
        gene_symbol_col="pr_gene_symbol",
    )
    assert gene_ids == ["100", "300"] and symbols == ["AAA", "CCC"]  # non-landmark BBB dropped


def test_assemble_sig_meta_is_cell_line_parameterized() -> None:
    sig_info = pd.DataFrame(
        {
            "sig_id": ["s1", "s2", "s3"],
            "pert_id": ["P1", "P1", "P2"],
            "cell_id": ["VCAP", "A549", "MCF7"],
            "pert_idose": ["10", "10", "10"],
            "pert_itime": ["24", "24", "24"],
        }
    )
    pert_to_ik = {"P1": "IK1", "P2": "IK2"}
    labelled = {"IK1", "IK2"}

    vcap_only = assemble_sig_meta(sig_info, pert_to_ik, labelled, cell_lines=["VCAP"])
    assert set(vcap_only["sig_id"]) == {"s1"}  # only the VCAP signature

    mixed = assemble_sig_meta(sig_info, pert_to_ik, labelled, cell_lines=["VCAP", "A549"])
    assert set(mixed["sig_id"]) == {"s1", "s2"}  # the SAME compound across two contexts

    # An unlabelled compound contributes nothing even if its cell line is requested.
    none_labelled = assemble_sig_meta(sig_info, pert_to_ik, set(), cell_lines=["VCAP", "A549"])
    assert none_labelled.empty
