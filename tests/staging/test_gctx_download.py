"""gctx download helpers: filename dim-parse, HDF5 validity, cache reuse/refetch."""

from __future__ import annotations

import gzip
from pathlib import Path

import fetch  # noqa: E402 — resolved via tests/staging/conftest.py sys.path
import gctx  # noqa: E402 — resolved via tests/staging/conftest.py sys.path
import numpy as np
import pandas as pd
import pytest


def _make_gctx(path: Path) -> None:
    data = pd.DataFrame(
        np.arange(6, dtype=float).reshape(3, 2),
        index=["g0", "g1", "g2"],
        columns=["SIG_0", "SIG_1"],
    )
    gctx.write_synthetic_gctx(path, data)


def test_parse_gctx_dims_from_real_filename() -> None:
    name = "GSE92742_Broad_LINCS_Level5_COMPZ.MODZ_n473647x12328.gctx.gz"
    assert fetch.parse_gctx_dims(name) == (473647, 12328)  # (n_signatures, n_genes)
    assert fetch.parse_gctx_dims("not_a_gctx_filename.txt.gz") is None


def test_is_valid_gctx(tmp_path) -> None:
    good = tmp_path / "ok.gctx"
    _make_gctx(good)
    assert fetch.is_valid_gctx(good) is True

    html = tmp_path / "bad.gctx"
    html.write_bytes(b"<!DOCTYPE html><html></html>")
    assert fetch.is_valid_gctx(html) is False
    assert fetch.is_valid_gctx(tmp_path / "missing.gctx") is False


def test_download_gctx_reuses_valid_cache(tmp_path) -> None:
    gctx_path = tmp_path / "level5_modz.gctx"
    _make_gctx(gctx_path)

    def fail_http(url, target):  # must NOT re-download a valid cached gctx
        raise AssertionError("should not download when a valid .gctx is cached")

    out = fetch.download_gctx("https://x/level5.gctx.gz", gctx_path, http=fail_http)
    assert out == gctx_path and fetch.is_valid_gctx(out)


def test_download_gctx_refetches_invalid_cache(tmp_path) -> None:
    gctx_path = tmp_path / "level5_modz.gctx"
    gctx_path.write_bytes(b"<!DOCTYPE html><html>stale/truncated</html>")  # invalid cache

    # Build a real .gctx and gzip it; the injected http writes that .gz to the target.
    real = tmp_path / "real.gctx"
    _make_gctx(real)
    gz_blob = gzip.compress(real.read_bytes())

    def transport_double(url, target):
        Path(target).write_bytes(gz_blob)
        return url, "application/gzip"

    out = fetch.download_gctx(
        "https://x/level5.gctx.gz",
        gctx_path,
        gz_path=tmp_path / "dl.gctx.gz",
        http=transport_double,
    )
    assert fetch.is_valid_gctx(out)


def test_download_gctx_rejects_html_payload(tmp_path) -> None:
    gctx_path = tmp_path / "level5_modz.gctx"

    def transport_double(url, target):  # server returns an HTML page, gzipped or not
        Path(target).write_bytes(b"<!DOCTYPE html><html></html>")
        return url, "text/html"

    with pytest.raises(ValueError, match="HTML"):
        fetch.download_gctx(
            "https://x/level5.gctx.gz",
            gctx_path,
            gz_path=tmp_path / "dl.gctx.gz",
            http=transport_double,
        )
    assert not gctx_path.exists()
