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
