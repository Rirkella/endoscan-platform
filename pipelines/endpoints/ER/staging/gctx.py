"""Slice a LINCS Level-5 ``.gctx`` to landmark genes x selected signatures.

``h5py`` is the **sole** reader — the same code path CI exercises (on a synthetic
``.gctx``) and the real Colab run uses. There is no cmapPy and no NumPy<2 pin: h5py,
pandas and pyarrow are all NumPy-2 compatible, so the operator runtime has no ABI
break.

GCTx v1.0 layout:
- ``/0/DATA/0/matrix`` : float32 2-D matrix
- ``/0/META/ROW/id``   : row ids (gene ids)
- ``/0/META/COL/id``   : column ids (sig_ids)

The matrix orientation (genes-major vs signatures-major) is **auto-detected** by
matching each matrix dimension to ``len(ROW/id)`` vs ``len(COL/id)`` — the real LINCS
dimensions differ (≈12k genes vs ≈474k signatures), so the match is unambiguous. The
slicer does a **partial HDF5 read** of only the requested signature slices, never the
full matrix, and returns a DataFrame in the requested row/col order.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import h5py
import numpy as np
import pandas as pd

_ROW_ID = "/0/META/ROW/id"
_COL_ID = "/0/META/COL/id"
_MATRIX = "/0/DATA/0/matrix"


def write_synthetic_gctx(path: Path, data_df: pd.DataFrame, *, genes_first: bool = False) -> None:
    """Write a tiny GCTx v1.0 file (``data_df`` rows = genes index, cols = sig_id columns).

    By default the matrix is stored signatures-major ``(n_cols, n_rows)`` like the real
    GEO file; ``genes_first=True`` stores it genes-major ``(n_rows, n_cols)`` so the
    orientation auto-detection can be tested against both layouts.
    """
    genes_by_sigs = data_df.to_numpy(dtype="float32")  # (genes, sigs)
    stored = genes_by_sigs if genes_first else genes_by_sigs.T
    with h5py.File(path, "w") as handle:
        handle.attrs["version"] = np.bytes_("GCTX1.0")
        data = handle.create_group("0").create_group("DATA").create_group("0")
        data.create_dataset("matrix", data=stored)
        meta = handle["0"].create_group("META")
        meta.create_group("ROW").create_dataset(
            "id", data=np.array([str(r) for r in data_df.index], dtype="S")
        )
        meta.create_group("COL").create_dataset(
            "id", data=np.array([str(c) for c in data_df.columns], dtype="S")
        )


def _decode(values) -> list[str]:
    return [v.decode() if isinstance(v, bytes) else str(v) for v in values]


def _resolve(requested: Sequence[str], available: list[str], kind: str) -> list[int]:
    """Map requested ids to integer positions; raise a clear error if any are absent."""
    pos = {value: i for i, value in enumerate(available)}  # first occurrence wins
    missing = [r for r in requested if r not in pos]
    if missing:
        raise KeyError(
            f"{len(missing)} requested {kind} id(s) absent from the gctx "
            f"(e.g. {missing[:5]}); refusing to slice to avoid silent mislabeling."
        )
    return [pos[r] for r in requested]


def slice_gctx_landmark(path: Path, row_ids: Sequence[str], col_ids: Sequence[str]) -> pd.DataFrame:
    """Return a ``(genes x signatures)`` DataFrame for the requested rows/cols.

    Auto-detects matrix orientation, validates that every requested id is present,
    reads ONLY the requested signature slices from HDF5 (never the full matrix), and
    returns the result in the requested ``row_ids`` x ``col_ids`` order.
    """
    row_ids = list(row_ids)
    col_ids = list(col_ids)
    with h5py.File(path, "r") as handle:
        all_genes = _decode(handle[_ROW_ID][:])
        all_sigs = _decode(handle[_COL_ID][:])
        dset = handle[_MATRIX]
        if dset.ndim != 2:
            raise ValueError(f"gctx matrix must be 2-D, got shape {dset.shape}")
        n_genes, n_sigs = len(all_genes), len(all_sigs)
        if n_genes == n_sigs:
            raise ValueError(
                "gctx ROW/id and COL/id have equal length; matrix orientation is ambiguous."
            )
        # Auto-detect which axis is genes by matching dimensions to the meta lengths.
        if dset.shape == (n_genes, n_sigs):
            genes_axis = 0
        elif dset.shape == (n_sigs, n_genes):
            genes_axis = 1
        else:
            raise ValueError(
                f"gctx matrix shape {dset.shape} matches neither (genes={n_genes}, "
                f"sigs={n_sigs}) nor its transpose; cannot determine orientation."
            )

        gene_idx = _resolve(row_ids, all_genes, "row/gene")
        sig_idx = _resolve(col_ids, all_sigs, "col/signature")

        # PARTIAL read: pull only the requested signatures (sorted+unique for h5py),
        # keeping all genes; then subselect/reorder genes and signatures in memory.
        sig_sorted = sorted(set(sig_idx))
        sig_rank = {orig: k for k, orig in enumerate(sig_sorted)}
        if genes_axis == 0:  # matrix[gene, sig] — select requested signature columns
            block = dset[:, sig_sorted]  # (n_genes, n_selected_sigs)
            block = block[gene_idx, :]  # genes -> requested order
        else:  # matrix[sig, gene] (real GCTx layout) — select requested signature rows
            block = dset[sig_sorted, :]  # (n_selected_sigs, n_genes)
            block = block[:, gene_idx].T  # -> (requested genes, selected sigs)
        block = block[:, [sig_rank[s] for s in sig_idx]]  # signatures -> requested order

    return pd.DataFrame(block, index=row_ids, columns=col_ids)
