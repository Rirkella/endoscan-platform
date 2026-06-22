"""Synthetic .gctx round-trip + h5py partial-read slicer (the only reader, NumPy-2 safe)."""

from __future__ import annotations

import gctx  # noqa: E402 — resolved via tests/staging/conftest.py sys.path
import numpy as np
import pandas as pd
import pytest


def _frame() -> pd.DataFrame:
    genes = [f"g{i}" for i in range(6)]
    sigs = [f"SIG_{i}" for i in range(4)]
    return pd.DataFrame(np.arange(24, dtype=float).reshape(6, 4), index=genes, columns=sigs)


def test_partial_read_correct_subset_and_shuffled_order(tmp_path) -> None:
    data = _frame()
    path = tmp_path / "tiny.gctx"
    gctx.write_synthetic_gctx(path, data)  # default: signatures-major (real layout)

    landmark = ["g5", "g0", "g2"]  # deliberately shuffled
    selected_cols = ["SIG_3", "SIG_1"]  # deliberately out of order
    out = gctx.slice_gctx_landmark(path, landmark, selected_cols)

    assert list(out.index) == landmark
    assert list(out.columns) == selected_cols
    np.testing.assert_allclose(out.to_numpy(), data.loc[landmark, selected_cols].to_numpy())


def test_orientation_autodetected_for_genes_major_layout(tmp_path) -> None:
    # rows (6 genes) != cols (4 sigs) makes orientation unambiguous; the slicer must
    # return identical results whether the matrix is stored genes-major or sigs-major.
    data = _frame()
    p_genes_first = tmp_path / "genes_first.gctx"
    p_sigs_first = tmp_path / "sigs_first.gctx"
    gctx.write_synthetic_gctx(p_genes_first, data, genes_first=True)
    gctx.write_synthetic_gctx(p_sigs_first, data, genes_first=False)

    landmark = ["g0", "g3"]
    cols = ["SIG_2", "SIG_0"]
    a = gctx.slice_gctx_landmark(p_genes_first, landmark, cols)
    b = gctx.slice_gctx_landmark(p_sigs_first, landmark, cols)

    np.testing.assert_allclose(a.to_numpy(), data.loc[landmark, cols].to_numpy())
    np.testing.assert_allclose(a.to_numpy(), b.to_numpy())


def test_missing_id_raises_clear_error(tmp_path) -> None:
    data = _frame()
    path = tmp_path / "tiny.gctx"
    gctx.write_synthetic_gctx(path, data)

    with pytest.raises(KeyError, match="row/gene id"):
        gctx.slice_gctx_landmark(path, ["g0", "NOPE"], ["SIG_0"])
    with pytest.raises(KeyError, match="col/signature id"):
        gctx.slice_gctx_landmark(path, ["g0"], ["SIG_0", "MISSING_SIG"])
