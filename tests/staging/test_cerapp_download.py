"""CERAPP downloader/resolver validation (no network — http + figshare are injected)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import fetch  # noqa: E402 — resolved via tests/staging/conftest.py sys.path
import pytest


def _write_valid_zip(dest: Path) -> None:
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("TrainingSet/cerapp_training.csv", "CASRN,Activity\n50-00-0,active\n")


def test_html_saved_as_zip_is_rejected_and_deleted(tmp_path) -> None:
    dest = tmp_path / "TrainingSet.zip"

    def fake_http(url, target):  # writes an HTML landing page, not a zip
        Path(target).write_bytes(b"<!DOCTYPE html><html><body>figshare</body></html>")
        return "https://figshare.example/articles/6062563", "text/html; charset=utf-8"

    with pytest.raises(ValueError, match="HTML"):
        fetch.download_cerapp_file("https://x/TrainingSet.zip", dest, http=fake_http)
    assert not dest.exists(), "the bad HTML file must be deleted, not left on disk"


def test_non_zip_payload_rejected_as_invalid_zip(tmp_path) -> None:
    dest = tmp_path / "EvaluationSet.zip"

    def fake_http(url, target):  # not HTML, but not a ZIP either
        Path(target).write_bytes(b"just,some,bytes\n1,2,3\n")
        return url, "application/octet-stream"

    with pytest.raises(ValueError, match="not a valid ZIP"):
        fetch.download_cerapp_file("https://x/EvaluationSet.zip", dest, http=fake_http)
    assert not dest.exists()


def test_stale_invalid_cached_zip_is_refetched(tmp_path) -> None:
    dest = tmp_path / "TrainingSet.zip"
    dest.write_bytes(b"<!DOCTYPE html><html>stale landing page from a prior run</html>")
    calls = {"n": 0}

    def fake_http(url, target):
        calls["n"] += 1
        _write_valid_zip(Path(target))
        return url, "application/zip"

    out = fetch.download_cerapp_file("https://x/TrainingSet.zip", dest, http=fake_http)
    assert calls["n"] == 1, "the invalid cached .zip must be re-downloaded"
    assert fetch.is_valid_zip(out)


def test_valid_cached_zip_is_not_refetched(tmp_path) -> None:
    dest = tmp_path / "TrainingSet.zip"
    _write_valid_zip(dest)

    def fail_http(url, target):  # must NOT be called for a valid cache
        raise AssertionError("should not re-download a valid cached zip")

    out = fetch.download_cerapp_file("https://x/TrainingSet.zip", dest, http=fail_http)
    assert fetch.is_valid_zip(out)


def test_pick_figshare_data_file_over_pdf_and_readme() -> None:
    files = [
        {"name": "CERAPP_manuscript.pdf", "download_url": "https://f/1"},
        {"name": "README.txt", "download_url": "https://f/2"},
        {"name": "TrainingSet.zip", "download_url": "https://f/3"},
    ]
    picked = fetch.pick_figshare_data_files(files)
    assert picked == [("TrainingSet.zip", "https://f/3")]


def test_resolver_uses_figshare_when_no_gaftp(tmp_path) -> None:
    def fake_figshare(article_id):
        return [
            {"name": "paper.pdf", "download_url": "https://f/pdf"},
            {"name": f"{article_id}.zip", "download_url": f"https://f/{article_id}"},
        ]

    def fake_http(url, target):
        _write_valid_zip(Path(target))
        return url, "application/zip"

    out = fetch.fetch_cerapp_experimental(
        tmp_path / "cerapp_src", ["6062563"], http=fake_http, figshare_lister=fake_figshare
    )
    assert [p.name for p in out] == ["6062563.zip"]
    assert all(fetch.is_valid_zip(p) for p in out)


def test_resolver_raises_clear_error_when_all_html(tmp_path) -> None:
    def fake_figshare(article_id):
        return [{"name": f"{article_id}.zip", "download_url": "https://f/x"}]

    def fake_http(url, target):  # every resolver hit returns HTML
        Path(target).write_bytes(b"<!DOCTYPE html><html></html>")
        return url, "text/html"

    with pytest.raises(RuntimeError, match="HTML/not-a-valid-ZIP or download failure"):
        fetch.fetch_cerapp_experimental(
            tmp_path / "cerapp_src", ["6062563"], http=fake_http, figshare_lister=fake_figshare
        )
