"""Orchestrate ER staging — fetch approved sources, build the staged files.

Run in the CLOUD via the four-stop operator notebook (``colab_run_er_phase2.ipynb``);
NOT in CI, NOT on a laptop. Produces
``data/staged/er/{lincs.parquet, cerapp.csv, pubchem.csv}`` which the toolchain
then reads via ``StagedSourceAdapter``. The pure transforms it calls
(``condition``, ``gctx``, ``cerapp``, ``pubchem``) are unit-tested in CI; the
network fetch (``fetch``) and the real column wiring run only in the cloud.

All functions are parameterized by column names supplied at call time (the
operator pastes the reviewed column names into the notebook's CONFIG cells); there
is no hardcoded schema. The pure transforms are unit-tested on fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

# Allow `import gctx` etc. when run as a script / from the Colab notebook.
sys.path.insert(0, str(Path(__file__).parent))

import cerapp as cerapp_mod  # noqa: E402
import condition as condition_mod  # noqa: E402
import gctx as gctx_mod  # noqa: E402
import pubchem as pubchem_mod  # noqa: E402

# LINCS pert_info uses "-666" as its missing-value sentinel; PubChem/CERAPP may emit
# blanks or NaN. None of these are real InChIKeys and must never join.
_INCHIKEY_SENTINELS = {"", "-666", "NAN", "NONE", "NA", "NULL"}


def normalize_inchikey(value: object) -> str | None:
    """Normalize an InChIKey for like-for-like joining, or ``None`` if it is a sentinel.

    Strips surrounding whitespace and upper-cases (InChIKeys are canonically
    upper-case). Returns ``None`` for empty strings, NaN, and the LINCS ``-666``
    missing-value sentinel so those rows can never silently match. Does NOT truncate:
    a full 27-char InChIKey stays full and a 14-char block-1 prefix stays a prefix, so
    callers can compare like-for-like and refuse to match full against prefix.
    """
    if value is None:
        return None
    token = str(value).strip()
    if token.upper() in _INCHIKEY_SENTINELS:
        return None
    return token.upper()


def assemble_sig_meta(
    sig_info: pd.DataFrame,
    pert_to_inchikey: dict[str, str],
    labelled_inchikeys: set[str],
    *,
    sig_id_col: str,
    pert_id_col: str,
    cell_id_col: str,
    dose_col: str,
    time_col: str,
    cell_lines: tuple[str, ...] = ("MCF7", "A549"),
) -> pd.DataFrame:
    """Build the tidy ``sig_meta`` table the staging pipeline consumes.

    Joins ``sig_info`` -> compound (perturbagen -> InChIKey via ``pert_to_inchikey``)
    and keeps only signatures of the requested ``cell_lines`` whose compound has an
    experimental label (``labelled_inchikeys``). Parameterized ONLY by column names
    (nothing hardcoded). Returns columns:
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
    in_cells = renamed[renamed["cell_id"].isin(cell_lines)].copy()
    in_cells["compound_key"] = in_cells["perturbagen_id"].astype(str).map(pert_to_inchikey)
    kept = in_cells[
        in_cells["compound_key"].notna() & in_cells["compound_key"].isin(labelled_inchikeys)
    ]
    columns = ["compound_key", "sig_id", "cell_id", "pert_dose", "pert_time"]
    return kept[columns].reset_index(drop=True)


def select_landmark_genes(
    gene_info: pd.DataFrame,
    *,
    landmark_flag_col: str,
    gene_id_col: str,
    gene_symbol_col: str,
    landmark_value: object = 1,
) -> tuple[list[str], list[str]]:
    """Return ``(gene_ids, gene_symbols)`` for the landmark genes (≈978).

    A row is a landmark gene when ``gene_info[landmark_flag_col]`` equals
    ``landmark_value`` (compared as strings, so 1 / "1" / True all match).
    """
    wanted = {str(landmark_value), "1", "True", "true"}
    landmark = gene_info[gene_info[landmark_flag_col].astype(str).isin(wanted)]
    return (
        list(landmark[gene_id_col].astype(str)),
        list(landmark[gene_symbol_col].astype(str)),
    )


def build_lincs_parquet(
    sig_meta: pd.DataFrame,
    gctx_path: Path,
    landmark_gene_ids: list[str],
    feature_names: list[str],
    out_path: Path,
) -> Path:
    """Select condition signatures -> slice gctx -> early-fuse -> write Parquet.

    ``sig_meta`` must already be filtered to MCF7/A549 signatures of the ER-labelled
    compounds, with columns ``compound_key, sig_id, cell_id, pert_dose, pert_time``.
    """
    chosen = condition_mod.select_condition_signatures(sig_meta)
    matrix = gctx_mod.slice_gctx_landmark(
        gctx_path, row_ids=landmark_gene_ids, col_ids=list(chosen["sig_id"])
    )  # (genes x sig_id) — the h5py slicer returns rows in the requested landmark order,
    # but reindex defensively so the positional ``feature_names`` labels below can never
    # be misassigned to the wrong gene.
    matrix = matrix.reindex(index=list(landmark_gene_ids))
    expr = matrix.T  # (sig_id x genes), columns now in landmark order
    expr.columns = list(feature_names)
    merged = chosen.merge(expr, left_on="sig_id", right_index=True)
    fused = condition_mod.early_fuse(merged, feature_names)
    fused.to_parquet(out_path, index=False)
    return out_path


def build_cerapp_csv(experimental_rows: list[dict], activity_col: str, out_path: Path) -> Path:
    rows = cerapp_mod.parse_cerapp_experimental(experimental_rows, activity_col=activity_col)
    pd.DataFrame(rows).to_csv(out_path, index=False)
    return out_path


def build_pubchem_csv(mapping_rows: list[dict], out_path: Path) -> Path:
    rows = pubchem_mod.normalize_mapping(mapping_rows)
    pd.DataFrame(rows).to_csv(out_path, index=False)
    return out_path


def main() -> None:  # pragma: no cover - cloud entry point, not run in CI
    raise SystemExit(
        "Run the staging steps from the Colab notebook (colab_run_er_phase2.ipynb): it "
        "fetches the approved locators, wires the reviewed column names, calls the "
        "build_* functions above, then runs the ER pipeline. Not runnable offline in CI."
    )


if __name__ == "__main__":
    main()
