"""Memory-bounded LINCS Level-5 ``.gctx`` slicer (the server build path's big-file read).

Lifted into ``endoscan_jobs`` from ``pipelines/endpoints/ER/staging/gctx.py`` (the frozen
ER Colab path) so the server extraction job is self-contained and CI-tested. ``h5py`` is the
**sole** reader — h5py/big-file I/O belongs in jobs, not in the lean ``endoscan_core``.

Difference from the staging slicer: the requested signatures are read in **batches**
(default 256 columns) through an HDF5 hyperslab, with a **bounded chunk cache**
(``rdcc_nbytes``) so a pathological on-disk chunking can never amplify the working set to
the full ~23 GB matrix. The batched read returns byte-for-byte the SAME result as a single
full-region read — it is a memory-bounded implementation of the same slice.

GCTx v1.0 layout:
- ``/0/DATA/0/matrix`` : float32 2-D matrix
- ``/0/META/ROW/id``   : row ids (gene ids)
- ``/0/META/COL/id``   : column ids (sig_ids)

The matrix orientation (genes-major vs signatures-major) is **auto-detected** by matching
each matrix dimension to ``len(ROW/id)`` vs ``len(COL/id)`` — the real LINCS dimensions
differ (≈12k genes vs ≈474k signatures), so the match is unambiguous.
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

#: Default signature batch (columns per hyperslab read) and chunk-cache bound. 256 sigs ×
#: ~12k genes × 4 bytes ≈ 12 MB per batch; the 32 MB cache caps chunk-read amplification.
DEFAULT_SIG_BATCH = 256
DEFAULT_RDCC_NBYTES = 32 * 1024 * 1024


def write_synthetic_gctx(path: Path, data_df: pd.DataFrame, *, genes_first: bool = False) -> None:
    """Write a tiny GCTx v1.0 file (``data_df`` rows = genes index, cols = sig_id columns).

    By default the matrix is stored signatures-major ``(n_cols, n_rows)`` like the real GEO
    file; ``genes_first=True`` stores it genes-major so the orientation auto-detection can be
    tested against both layouts.
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


def slice_gctx_landmark(
    path: Path,
    row_ids: Sequence[str],
    col_ids: Sequence[str],
    *,
    batch_size: int = DEFAULT_SIG_BATCH,
    rdcc_nbytes: int = DEFAULT_RDCC_NBYTES,
) -> pd.DataFrame:
    """Return a ``(genes x signatures)`` DataFrame for the requested rows/cols.

    Auto-detects matrix orientation, validates that every requested id is present, reads
    ONLY the requested signatures (in batches of ``batch_size``, never the full matrix) with
    a bounded chunk cache, and returns the result in the requested ``row_ids`` x ``col_ids``
    order. The batched read is identical to a single full-region read.
    """
    row_ids = list(row_ids)
    col_ids = list(col_ids)
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    with h5py.File(path, "r", rdcc_nbytes=rdcc_nbytes) as handle:
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

        # PARTIAL, BATCHED read: pull the requested signatures in sorted+unique batches
        # (h5py fancy indexing needs increasing indices), keeping all genes per batch, then
        # subselect/reorder genes and signatures in memory.
        sig_sorted = sorted(set(sig_idx))
        sig_rank = {orig: k for k, orig in enumerate(sig_sorted)}
        batches: list[np.ndarray] = []
        for start in range(0, len(sig_sorted), batch_size):
            cols = sig_sorted[start : start + batch_size]
            if genes_axis == 0:  # matrix[gene, sig] — select requested signature columns
                block = dset[:, cols]  # (n_genes, batch)
                block = block[gene_idx, :]  # genes -> requested order
            else:  # matrix[sig, gene] (real GCTx layout) — select requested signature rows
                block = dset[cols, :]  # (batch, n_genes)
                block = block[:, gene_idx].T  # -> (requested genes, batch)
            batches.append(block)
        if batches:
            full = np.concatenate(batches, axis=1)  # (genes, n_unique_sigs) in sorted order
        else:
            full = np.empty((len(gene_idx), 0), dtype="float32")
        full = full[:, [sig_rank[s] for s in sig_idx]]  # signatures -> requested order

    return pd.DataFrame(full, index=row_ids, columns=col_ids)
