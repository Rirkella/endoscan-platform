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


# --- real GSE92742 schema: dose/time column collision (the server-found bug) -----------


def _real_schema_sig_info() -> pd.DataFrame:
    """sig_info with the REAL 12-column shape: numeric pert_dose/pert_time AND string
    pert_idose/pert_itime present together (the exact coexistence that collided)."""
    return pd.DataFrame(
        {
            "sig_id": ["s1", "s2"],
            "pert_id": ["P1", "P1"],
            "pert_iname": ["c", "c"],
            "pert_type": ["trt_cp", "trt_cp"],
            "cell_id": ["VCAP", "VCAP"],
            "pert_dose": [10.0, 0.1],  # NUMERIC (real preferred column)
            "pert_dose_unit": ["µM", "µM"],
            "pert_idose": ["10 µM", "0.1 µM"],  # STRING quantity (the colliding column)
            "pert_time": [24.0, 24.0],  # NUMERIC
            "pert_time_unit": ["h", "h"],
            "pert_itime": ["24 h", "24 h"],  # STRING quantity
            "distil_id": ["d1", "d2"],
        }
    )


def test_real_schema_no_duplicate_columns_and_numeric() -> None:
    # ★ Regression: the real schema must NOT produce a duplicate pert_dose/pert_time column,
    # and select_condition_signatures must run without "truth value of a Series is ambiguous".
    sig_meta = assemble_sig_meta(
        _real_schema_sig_info(), {"P1": "IK1"}, {"IK1"}, cell_lines=["VCAP"]
    )
    assert list(sig_meta.columns).count("pert_dose") == 1  # single, unambiguous
    assert list(sig_meta.columns).count("pert_time") == 1
    assert pd.api.types.is_numeric_dtype(sig_meta["pert_dose"])  # numeric, not the "10 µM" string
    assert set(sig_meta["pert_dose"]) == {10.0, 0.1}
    chosen = select_condition_signatures(sig_meta)  # must NOT raise
    assert chosen.loc[chosen["compound_key"] == "IK1", "sig_id"].tolist() == ["s1"]  # 10 µM wins


def test_condition_preference_is_operative_on_numeric_dose() -> None:
    # With the OLD code the string column NaN'd out and the 10 µM preference was inoperative
    # (selection fell to the sig_id tie-break). On numeric dose the preference actually works:
    # s1 (10 µM) is preferred over s2 (0.1 µM) even though "s2" would NOT sort first anyway —
    # so flip the ids to prove dose drives it, not the tie-break.
    df = _real_schema_sig_info()
    df["sig_id"] = ["z_far", "a_near"]  # a_near is 0.1 µM; if dose were ignored it would win
    df["pert_dose"] = [10.0, 0.1]
    sig_meta = assemble_sig_meta(df, {"P1": "IK1"}, {"IK1"}, cell_lines=["VCAP"])
    chosen = select_condition_signatures(sig_meta)
    assert chosen["sig_id"].tolist() == ["z_far"]  # 10 µM chosen despite the worse-sorting id


def test_string_quantity_fallback_when_no_numeric_column() -> None:
    # A file with ONLY the string quantity column (no numeric pert_dose) still parses to a
    # numeric preference (leading number of "10 µM"/"1 µM").
    df = pd.DataFrame(
        {
            "sig_id": ["near", "far"],
            "pert_id": ["P1", "P1"],
            "cell_id": ["VCAP", "VCAP"],
            "pert_idose": ["10 µM", "1 µM"],
            "pert_itime": ["24 h", "24 h"],
        }
    )
    sig_meta = assemble_sig_meta(df, {"P1": "IK1"}, {"IK1"}, cell_lines=["VCAP"])
    assert pd.api.types.is_numeric_dtype(sig_meta["pert_dose"])
    assert set(sig_meta["pert_dose"]) == {10.0, 1.0}
    assert select_condition_signatures(sig_meta)["sig_id"].tolist() == ["near"]  # 10 µM wins


def test_assemble_feeds_the_same_numeric_values_frozen_er_used() -> None:
    # Drift confirmation: ER staging fed the NUMERIC pert_dose/pert_time column into the
    # condition rule (operator chose the numeric column). Our auto-detect yields exactly those
    # numeric values, so AR is built the same way ER was. Combined with the select/early_fuse
    # drift-guards above, the corrected path is IN alignment with ER, not divergent.
    raw = _real_schema_sig_info()
    sig_meta = assemble_sig_meta(raw, {"P1": "IK1"}, {"IK1"}, cell_lines=["VCAP"])
    by_sig = sig_meta.set_index("sig_id")
    assert by_sig.loc["s1", "pert_dose"] == 10.0 and by_sig.loc["s2", "pert_dose"] == 0.1
    assert by_sig.loc["s1", "pert_time"] == 24.0
    # The frozen ER condition module, given the SAME numeric sig_meta, selects identically.
    staging = _staging_condition()
    pd.testing.assert_frame_equal(
        select_condition_signatures(sig_meta).reset_index(drop=True),
        staging.select_condition_signatures(sig_meta).reset_index(drop=True),
        check_like=True,
    )
