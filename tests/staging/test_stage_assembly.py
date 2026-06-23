"""stage_er assembly helpers: sig_meta join/filter + landmark gene selection."""

from __future__ import annotations

import pandas as pd
import stage_er  # noqa: E402 — resolved via tests/staging/conftest.py sys.path


def test_assemble_sig_meta_joins_filters_and_renames() -> None:
    sig_info = pd.DataFrame(
        [
            # kept: MCF7, mapped to a labelled compound
            {"sid": "S1", "pert": "BRD-1", "cell": "MCF7", "dose": 10.0, "time": 24.0},
            # kept: A549, same labelled compound
            {"sid": "S2", "pert": "BRD-1", "cell": "A549", "dose": 10.0, "time": 24.0},
            # dropped: wrong cell line
            {"sid": "S3", "pert": "BRD-1", "cell": "PC3", "dose": 10.0, "time": 24.0},
            # dropped: perturbagen maps to an UNLABELLED compound
            {"sid": "S4", "pert": "BRD-2", "cell": "MCF7", "dose": 10.0, "time": 24.0},
            # dropped: perturbagen has no mapping
            {"sid": "S5", "pert": "BRD-9", "cell": "MCF7", "dose": 10.0, "time": 24.0},
        ]
    )
    pert_to_inchikey = {"BRD-1": "INCHI_A", "BRD-2": "INCHI_B"}
    labelled = {"INCHI_A"}  # only A is labelled

    out = stage_er.assemble_sig_meta(
        sig_info,
        pert_to_inchikey,
        labelled,
        sig_id_col="sid",
        pert_id_col="pert",
        cell_id_col="cell",
        dose_col="dose",
        time_col="time",
    )
    assert list(out.columns) == ["compound_key", "sig_id", "cell_id", "pert_dose", "pert_time"]
    assert set(out["sig_id"]) == {"S1", "S2"}  # only labelled INCHI_A in MCF7/A549
    assert set(out["compound_key"]) == {"INCHI_A"}
    assert set(out["cell_id"]) == {"MCF7", "A549"}


def test_build_lincs_parquet_labels_survive_shuffled_slice_order(tmp_path, monkeypatch) -> None:
    # Defensive: even if a slicer returned rows in a different order than requested,
    # build_lincs_parquet reindexes to landmark order so feature columns stay correct.
    landmark_ids = ["g0", "g1", "g2"]
    feature_names = ["GENE0", "GENE1", "GENE2"]
    sig_meta = pd.DataFrame(
        [
            {
                "compound_key": "C1",
                "sig_id": "S1",
                "cell_id": "MCF7",
                "pert_dose": 10.0,
                "pert_time": 24.0,
            },
            {
                "compound_key": "C1",
                "sig_id": "S2",
                "cell_id": "A549",
                "pert_dose": 10.0,
                "pert_time": 24.0,
            },
        ]
    )
    # Reader hands back genes in a DIFFERENT order than requested (g2, g0, g1).
    shuffled = pd.DataFrame(
        {"S1": [12.0, 10.0, 11.0], "S2": [22.0, 20.0, 21.0]},
        index=["g2", "g0", "g1"],
    )
    monkeypatch.setattr(
        stage_er.gctx_mod, "slice_gctx_landmark", lambda path, row_ids, col_ids: shuffled
    )
    out = stage_er.build_lincs_parquet(
        sig_meta, tmp_path / "ignored.gctx", landmark_ids, feature_names, tmp_path / "lincs.parquet"
    )
    fused = pd.read_parquet(out).set_index("compound_key").loc["C1"]
    # g0 -> GENE0 = mean(10, 20) = 15; g1 -> GENE1 = 16; g2 -> GENE2 = 17 regardless of order.
    assert fused["GENE0"] == 15.0
    assert fused["GENE1"] == 16.0
    assert fused["GENE2"] == 17.0


def test_normalize_inchikey_strips_uppercases_and_drops_sentinels() -> None:
    # Whitespace + case are normalized so like-for-like joins succeed.
    assert stage_er.normalize_inchikey("  abc-def-g  ") == "ABC-DEF-G"
    key = "ABCDEFGHIJKLMN-OPQRSTUVWX-Y"
    assert stage_er.normalize_inchikey(f"  {key.lower()}  ") == key
    # The LINCS -666 sentinel, blanks, and NaN-likes are NOT real keys -> None.
    for sentinel in ("-666", "", "   ", "nan", "None", "NA", "null", None, float("nan")):
        assert stage_er.normalize_inchikey(sentinel) is None
    # Full (27-char) vs prefix (14-char) are preserved, NOT truncated, so a caller can
    # refuse to match one against the other.
    full = "ABCDEFGHIJKLMN-OPQRSTUVWX-N"
    prefix = "ABCDEFGHIJKLMN"
    assert stage_er.normalize_inchikey(full) == full
    assert stage_er.normalize_inchikey(prefix) == prefix
    assert stage_er.normalize_inchikey(full) != stage_er.normalize_inchikey(prefix)


def test_select_landmark_genes_by_flag() -> None:
    gene_info = pd.DataFrame(
        [
            {"gid": "100", "sym": "GENE_A", "lm": 1},
            {"gid": "200", "sym": "GENE_B", "lm": 0},
            {"gid": "300", "sym": "GENE_C", "lm": 1},
        ]
    )
    gene_ids, gene_symbols = stage_er.select_landmark_genes(
        gene_info, landmark_flag_col="lm", gene_id_col="gid", gene_symbol_col="sym"
    )
    assert gene_ids == ["100", "300"]
    assert gene_symbols == ["GENE_A", "GENE_C"]
