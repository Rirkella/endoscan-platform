"""Orchestrate ER staging — fetch approved sources, build the staged files.

Run in the CLOUD (Colab "Run all"); NOT in CI, NOT on a laptop. Produces
``data/staged/er/{lincs.parquet, cerapp.csv, pubchem.csv}`` which the toolchain
then reads via ``StagedSourceAdapter``. The pure transforms it calls
(``condition``, ``gctx``, ``cerapp``, ``pubchem``) are unit-tested in CI; the
network fetch (``fetch``) and the real column wiring run only in the cloud.

This is a structured skeleton: seams marked ``CONFIRM`` need the real source
column names verified before the Phase-2b run (flagged in the dataset card).
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
    )  # (genes x sig_id)
    # Attach the 978-gene vector to each chosen signature, then early-fuse.
    expr = matrix.T  # (sig_id x genes)
    expr.columns = feature_names
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
        "fetches the approved locators, wires the CONFIRMed real columns, calls the "
        "build_* functions above, then runs the ER pipeline. Not runnable offline in CI."
    )


if __name__ == "__main__":
    main()
