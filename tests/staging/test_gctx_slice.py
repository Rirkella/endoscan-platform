"""Synthetic .gctx round-trip + landmark/sig slice (h5py path, NumPy-2 safe)."""

from __future__ import annotations

import gctx  # noqa: E402 — resolved via tests/staging/conftest.py sys.path
import numpy as np
import pandas as pd


def test_write_then_slice_landmark_and_signatures(tmp_path) -> None:
    genes = [f"g{i}" for i in range(6)]
    sigs = [f"SIG_{i}" for i in range(4)]
    data = pd.DataFrame(np.arange(24, dtype=float).reshape(6, 4), index=genes, columns=sigs)
    path = tmp_path / "tiny.gctx"
    gctx.write_synthetic_gctx(path, data)

    landmark = ["g0", "g2", "g5"]
    selected_cols = ["SIG_3", "SIG_1"]  # deliberately out of order
    out = gctx.slice_gctx_landmark(path, landmark, selected_cols)

    assert list(out.index) == landmark
    assert list(out.columns) == selected_cols
    np.testing.assert_allclose(out.to_numpy(), data.loc[landmark, selected_cols].to_numpy())
