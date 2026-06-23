"""CERAPP archive extraction + tabular discovery on a synthetic zip (no network)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import fetch  # noqa: E402 — resolved via tests/staging/conftest.py sys.path


def _make_cerapp_zip(zip_path: Path) -> None:
    # A comma-delimited csv table, a TAB-delimited .txt table, and a prose README.
    csv_text = "CASRN,Activity\n50-00-0,active\n51-28-5,inactive\n"
    txt_text = "CASRN\tActivity\tConsensus\n50-00-0\tactive\t0.9\n"
    readme = "# CERAPP\nExperimental ER activity sets. This is not a table.\n"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("TrainingSet/cerapp_training.csv", csv_text)
        zf.writestr("TrainingSet/cerapp_training.txt", txt_text)
        zf.writestr("TrainingSet/README.md", readme)


def test_inspect_extracts_finds_tables_sniffs_delimiter_no_fallback(tmp_path, capsys) -> None:
    zip_path = tmp_path / "TrainingSet.zip"
    _make_cerapp_zip(zip_path)
    work = tmp_path / "cerapp_src"

    manifest = fetch.inspect_cerapp_archives([zip_path], work)

    # Both tables found; the README is NOT a table -> NOT in the manifest.
    names = sorted(Path(m["path"]).name for m in manifest)
    assert names == ["cerapp_training.csv", "cerapp_training.txt"]
    assert manifest, "manifest must be non-empty so the notebook does NOT fall back to 1d"
    assert all(m["readable"] for m in manifest)

    by_name = {Path(m["path"]).name: m for m in manifest}
    # Delimiter is SNIFFED, not assumed by extension: csv -> ',', tab-delimited .txt -> '\t'.
    assert by_name["cerapp_training.csv"]["columns"] == ["CASRN", "Activity"]
    assert by_name["cerapp_training.txt"]["columns"] == ["CASRN", "Activity", "Consensus"]
    assert by_name["cerapp_training.txt"]["rows"] == 1
    # The consensus column is flagged so the operator does not mistake it for labels.
    assert "consensus" in by_name["cerapp_training.txt"]["kind"].lower()

    out = capsys.readouterr().out
    assert "cerapp_training.csv" in out and "cerapp_training.txt" in out  # headers/counts printed
    assert "README" not in out  # readme is not even a tabular candidate


def test_extract_handles_nested_zip(tmp_path) -> None:
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("inner_table.csv", "a,b\n1,2\n")
    outer = tmp_path / "EvaluationSet.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.write(inner, arcname="nested/inner.zip")
    work = tmp_path / "cerapp_src"

    manifest = fetch.inspect_cerapp_archives([outer], work)
    assert [Path(m["path"]).name for m in manifest] == ["inner_table.csv"]
    assert manifest[0]["columns"] == ["a", "b"]
