"""Pure LINCS condition-selection + early fusion for the extraction job.

Mirrors the frozen ER staging transforms (``pipelines/endpoints/ER/staging/{condition,
stage_er}.py``) so the server extraction job is self-contained and CI-tested, producing the
SAME fused 978-gene-per-compound matrix and SAME ``compound_id`` contract. These are pure
pandas functions (no network, no SMILES, no h5py); the frozen ER path is left untouched and
``tests/jobs/test_fusion.py`` pins identical behavior against it.

Mapping (FLAGGED for human confirmation; documented in the dataset card):
- **Condition rule:** prefer 10 uM / 24 h; otherwise the nearest available dose/time
  (by |log10 dose - log10 10| then |time - 24|), deterministic tie-break by ``sig_id``.
- **Early fusion:** mean-aggregate each compound's selected cell-line landmark profiles into
  ONE 978-vector per compound; ``group = compound`` (InChIKey).
- **Cell-line set is parameterized** (NOT hardcoded to MCF7/A549) so the same endpoint can be
  extracted on different contexts (e.g. VCaP vs VCaP+A549) for the platform comparison.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pandas as pd

PREFERRED_DOSE_UM = 10.0
PREFERRED_TIME_H = 24.0
#: Recorded in the slice manifest so a fusion-rule change invalidates a cached slice.
FUSION_PARAMS = {
    "preferred_dose_um": PREFERRED_DOSE_UM,
    "preferred_time_h": PREFERRED_TIME_H,
    "condition_rule": "nearest_to_10uM_24h_tiebreak_sig_id",
    "fusion": "mean_over_cell_lines",
}


def select_landmark_genes(
    gene_info: pd.DataFrame,
    *,
    landmark_flag_col: str,
    gene_id_col: str,
    gene_symbol_col: str,
    landmark_value: object = 1,
) -> tuple[list[str], list[str]]:
    """Return ``(gene_ids, gene_symbols)`` for the landmark genes (≈978).

    A row is a landmark gene when ``gene_info[landmark_flag_col]`` equals ``landmark_value``
    (compared as strings, so 1 / "1" / True all match).
    """
    wanted = {str(landmark_value), "1", "True", "true"}
    landmark = gene_info[gene_info[landmark_flag_col].astype(str).isin(wanted)]
    return (
        list(landmark[gene_id_col].astype(str)),
        list(landmark[gene_symbol_col].astype(str)),
    )


def assemble_sig_meta(
    sig_info: pd.DataFrame,
    pert_to_inchikey: dict[str, str],
    labelled_inchikeys: set[str],
    *,
    cell_lines: Sequence[str],
    sig_id_col: str = "sig_id",
    pert_id_col: str = "pert_id",
    cell_id_col: str = "cell_id",
    dose_col: str = "pert_idose",
    time_col: str = "pert_itime",
) -> pd.DataFrame:
    """Build the tidy ``sig_meta`` table the extraction consumes (the overlap sig_id list).

    Joins ``sig_info`` -> compound (perturbagen -> InChIKey via ``pert_to_inchikey``) and
    keeps only signatures of the requested ``cell_lines`` whose compound has an experimental
    label (``labelled_inchikeys``). Returns columns:
    ``compound_key, sig_id, cell_id, pert_dose, pert_time``.
    """
    renamed = sig_info.rename(
        columns={
            sig_id_col: "sig_id",
            pert_id_col: "perturbagen_id",
            cell_id_col: "cell_id",
            dose_col: "pert_dose",
            time_col: "pert_time",
        }
    )
    cells = {str(c).upper() for c in cell_lines}
    renamed["cell_id"] = renamed["cell_id"].astype(str).str.upper()
    in_cells = renamed[renamed["cell_id"].isin(cells)].copy()
    in_cells["compound_key"] = in_cells["perturbagen_id"].astype(str).map(pert_to_inchikey)
    kept = in_cells[
        in_cells["compound_key"].notna() & in_cells["compound_key"].isin(labelled_inchikeys)
    ]
    columns = ["compound_key", "sig_id", "cell_id", "pert_dose", "pert_time"]
    return kept[columns].reset_index(drop=True)


def _distance(dose: object, time: object) -> tuple[float, float]:
    """Distance from the preferred (10 uM, 24 h) condition; never crashes.

    ``sig_info`` delivers ``pert_dose``/``pert_time`` as STRINGS (and sometimes blanks or
    junk), so coerce numerically first: a non-numeric/non-positive dose or a non-numeric time
    ranks LAST (large distance) instead of raising.
    """
    dose_val = pd.to_numeric(dose, errors="coerce")
    time_val = pd.to_numeric(time, errors="coerce")
    dose_ok = pd.notna(dose_val) and dose_val > 0
    dose_term = abs(math.log10(dose_val) - math.log10(PREFERRED_DOSE_UM)) if dose_ok else 1e9
    time_term = abs(float(time_val) - PREFERRED_TIME_H) if pd.notna(time_val) else 1e9
    return (dose_term, time_term)


def select_condition_signatures(sig_meta: pd.DataFrame) -> pd.DataFrame:
    """Pick ONE signature per (compound_key, cell_id) by the condition rule.

    ``sig_meta`` columns required: ``compound_key, sig_id, cell_id, pert_dose, pert_time``
    (+ any feature columns, carried through). Returns the chosen rows.
    """
    ranked = sig_meta.copy()
    distances = ranked.apply(lambda r: _distance(r["pert_dose"], r["pert_time"]), axis=1)
    ranked["_d_dose"] = [d[0] for d in distances]
    ranked["_d_time"] = [d[1] for d in distances]
    ranked = ranked.sort_values(["compound_key", "cell_id", "_d_dose", "_d_time", "sig_id"])
    chosen = ranked.groupby(["compound_key", "cell_id"], as_index=False).first()
    return chosen.drop(columns=["_d_dose", "_d_time"])


def early_fuse(selected: pd.DataFrame, feature_cols: Sequence[str]) -> pd.DataFrame:
    """Mean cell-line landmark profiles -> one vector per compound (group=compound)."""
    return selected.groupby("compound_key", as_index=False)[list(feature_cols)].mean()
