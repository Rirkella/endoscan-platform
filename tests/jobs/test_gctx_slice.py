"""endoscan_jobs gctx slicer: batched read == full read, orientation, bounded rdcc."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from endoscan_jobs.gctx import slice_gctx_landmark, write_synthetic_gctx

pytest.importorskip("h5py")


def _frame(n_genes: int = 6, n_sigs: int = 10) -> pd.DataFrame:
    genes = [f"g{i}" for i in range(n_genes)]
    sigs = [f"SIG_{i}" for i in range(n_sigs)]
    data = np.arange(n_genes * n_sigs, dtype=float).reshape(n_genes, n_sigs)
    return pd.DataFrame(data, index=genes, columns=sigs)


def test_batched_read_equals_full_read(tmp_path) -> None:
    # ★ The whole point: a small batch must yield byte-for-byte the SAME slice as one big
    # read — a memory-bounded implementation of the identical extraction.
    data = _frame(n_genes=6, n_sigs=10)
    path = tmp_path / "tiny.gctx"
    write_synthetic_gctx(path, data)  # signatures-major (real layout)

    landmark = ["g5", "g0", "g2"]  # shuffled gene order
    cols = ["SIG_7", "SIG_1", "SIG_4", "SIG_9", "SIG_0"]  # shuffled signature order
    expected = data.loc[landmark, cols]

    one_shot = slice_gctx_landmark(path, landmark, cols, batch_size=1000)
    batched = slice_gctx_landmark(path, landmark, cols, batch_size=2)  # forces 3 batches

    np.testing.assert_allclose(one_shot.to_numpy(), expected.to_numpy())
    np.testing.assert_allclose(batched.to_numpy(), expected.to_numpy())
    np.testing.assert_array_equal(batched.to_numpy(), one_shot.to_numpy())
    assert list(batched.index) == landmark and list(batched.columns) == cols


def test_batched_equals_full_for_genes_major_layout(tmp_path) -> None:
    data = _frame(n_genes=5, n_sigs=8)
    p = tmp_path / "gm.gctx"
    write_synthetic_gctx(p, data, genes_first=True)  # other orientation
    landmark = ["g0", "g3"]
    cols = ["SIG_6", "SIG_2", "SIG_0"]
    full = slice_gctx_landmark(p, landmark, cols, batch_size=1000)
    batched = slice_gctx_landmark(p, landmark, cols, batch_size=1)
    np.testing.assert_array_equal(full.to_numpy(), batched.to_numpy())
    np.testing.assert_allclose(full.to_numpy(), data.loc[landmark, cols].to_numpy())


def test_small_rdcc_does_not_change_result(tmp_path) -> None:
    # A tiny chunk-cache bound must not alter correctness — only memory behavior.
    data = _frame(n_genes=4, n_sigs=12)
    path = tmp_path / "r.gctx"
    write_synthetic_gctx(path, data)
    cols = ["SIG_11", "SIG_3"]
    big = slice_gctx_landmark(path, ["g0", "g2"], cols, rdcc_nbytes=64 * 1024 * 1024)
    small = slice_gctx_landmark(path, ["g0", "g2"], cols, rdcc_nbytes=1024)
    np.testing.assert_array_equal(big.to_numpy(), small.to_numpy())


def test_missing_id_raises_clear_error(tmp_path) -> None:
    data = _frame()
    path = tmp_path / "tiny.gctx"
    write_synthetic_gctx(path, data)
    with pytest.raises(KeyError, match="row/gene id"):
        slice_gctx_landmark(path, ["g0", "NOPE"], ["SIG_0"])
    with pytest.raises(KeyError, match="col/signature id"):
        slice_gctx_landmark(path, ["g0"], ["SIG_0", "MISSING_SIG"])
