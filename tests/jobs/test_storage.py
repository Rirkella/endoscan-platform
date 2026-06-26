"""LocalStorage backend: round-trip, namespacing, traversal rejection."""

from __future__ import annotations

from pathlib import Path

import pytest
from endoscan_jobs.storage import LocalStorage, StorageKeyError, normalize_key


def test_put_get_roundtrip(tmp_path: Path) -> None:
    s = LocalStorage(tmp_path)
    s.put_text("reports/coverage/AR/run1.json", '{"ok": true}')
    s.put_bytes("raw/AR/data.zip", b"PK\x03\x04zzz")
    assert s.get_text("reports/coverage/AR/run1.json") == '{"ok": true}'
    assert s.get_bytes("raw/AR/data.zip") == b"PK\x03\x04zzz"
    assert s.exists("raw/AR/data.zip") and not s.exists("raw/AR/missing.zip")


def test_list_prefix(tmp_path: Path) -> None:
    s = LocalStorage(tmp_path)
    s.put_text("reports/coverage/AR/a.json", "{}")
    s.put_text("reports/coverage/AR/b.md", "x")
    s.put_text("logs/run1/result.json", "{}")
    listed = s.list("reports/coverage/AR")
    assert listed == ["reports/coverage/AR/a.json", "reports/coverage/AR/b.md"]


def test_namespace_required() -> None:
    with pytest.raises(StorageKeyError):
        normalize_key("nope/AR/x")  # not a known namespace
    assert normalize_key("reports/AR/x") == "reports/AR/x"


def test_traversal_and_absolute_rejected(tmp_path: Path) -> None:
    s = LocalStorage(tmp_path)
    for bad in ("../etc/passwd", "/etc/passwd", "reports/../../escape", "reports/..", ""):
        with pytest.raises(StorageKeyError):
            s.put_text(bad, "x")
    # A harmless "." segment is collapsed, not an escape.
    assert normalize_key("reports/./x") == "reports/x"


def test_local_path_within_root(tmp_path: Path) -> None:
    s = LocalStorage(tmp_path)
    p = s.local_path("artifacts/AR/model.pkl")
    assert p is not None and str(p).startswith(str(tmp_path.resolve()))
