"""Slice a LINCS Level-5 ``.gctx`` to landmark genes x selected signatures.

Two readers, same interface:
- **cmapPy** (authoritative) is used when importable. The real Phase-2 run does the
  slice inside the Colab notebook where cmapPy is installed with NumPy<2 (cmapPy's
  writer/parser uses the removed ``numpy.string_`` and is NOT NumPy-2 compatible).
- **h5py** fallback (NumPy-2 compatible) is what CI exercises against a tiny
  synthetic ``.gctx`` written by ``write_synthetic_gctx`` below.

GCTx v1.0 layout assumed by the h5py path (FLAGGED — validate against the real
GEO file in Phase 2; cmapPy is authoritative there):
- ``/0/DATA/0/matrix`` : float32, shape ``(n_cols, n_rows)`` (samples x genes)
- ``/0/META/ROW/id``   : row ids (gene ids)
- ``/0/META/COL/id``   : column ids (sig_ids)

The h5py fallback loads the full matrix, so it is meant for small/sliced files and
the synthetic fixture — NOT the 12 GB real matrix (use cmapPy there).
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


def write_synthetic_gctx(path: Path, data_df: pd.DataFrame) -> None:
    """Write a tiny GCTx v1.0 file (rows = genes index, cols = sig_id columns)."""
    matrix = data_df.to_numpy(dtype="float32").T  # store as (n_cols, n_rows)
    with h5py.File(path, "w") as handle:
        handle.attrs["version"] = np.bytes_("GCTX1.0")
        data = handle.create_group("0").create_group("DATA").create_group("0")
        data.create_dataset("matrix", data=matrix)
        meta = handle["0"].create_group("META")
        meta.create_group("ROW").create_dataset(
            "id", data=np.array([str(r) for r in data_df.index], dtype="S")
        )
        meta.create_group("COL").create_dataset(
            "id", data=np.array([str(c) for c in data_df.columns], dtype="S")
        )


def _decode(values) -> list[str]:
    return [v.decode() if isinstance(v, bytes) else str(v) for v in values]


#: The h5py fallback loads the FULL matrix into memory, so it is only safe for
#: small/sliced files and the synthetic fixture — never the ~12k x 473k real
#: matrix. Guard well below that (cells = n_rows * n_cols).
_MAX_H5PY_CELLS = 5_000_000


def _slice_h5py(path: Path, row_ids: Sequence[str], col_ids: Sequence[str]) -> pd.DataFrame:
    with h5py.File(path, "r") as handle:
        n_cols, n_rows = handle[_MATRIX].shape  # stored as (n_cols, n_rows)
        if n_rows * n_cols > _MAX_H5PY_CELLS:
            raise RuntimeError(
                f"gctx matrix is too large for the h5py fallback "
                f"({n_rows} x {n_cols}); use cmapPy (NumPy<2) for the real file. "
                "The h5py path is for the synthetic CI fixture only."
            )
        all_rows = _decode(handle[_ROW_ID][:])
        all_cols = _decode(handle[_COL_ID][:])
        matrix = handle[_MATRIX][:]  # (n_cols, n_rows)
    frame = pd.DataFrame(matrix.T, index=all_rows, columns=all_cols)  # (rows, cols)
    return frame.loc[list(row_ids), list(col_ids)]


def slice_gctx_landmark(path: Path, row_ids: Sequence[str], col_ids: Sequence[str]) -> pd.DataFrame:
    """Return a (genes x signatures) DataFrame for the requested rows/cols.

    Uses cmapPy (authoritative) when it is installed (the Colab run, NumPy<2). The
    h5py fallback runs ONLY when cmapPy is **absent** (``ImportError`` — CI /
    synthetic fixture). A cmapPy PARSE failure on the real file must surface
    loudly rather than silently fall through to the full-matrix h5py path.
    """
    try:
        from cmapPy.pandasGEXpress.parse_gctx import parse  # noqa: PLC0415
    except ImportError:
        return _slice_h5py(path, row_ids, col_ids)
    return parse(str(path), rid=list(row_ids), cid=list(col_ids)).data_df
