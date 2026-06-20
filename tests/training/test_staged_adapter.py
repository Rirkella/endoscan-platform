"""StagedSourceAdapter reads Parquet + CSV by source.id (offline)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from endoscan_core.datasets import SourceEntry, StagedSourceAdapter


def _source(source_id: str, source_type: str = "labels") -> SourceEntry:
    return SourceEntry(
        id=source_id,
        name=source_id,
        type=source_type,
        access_method="staged",
        provenance="test",
        version="0",
    )


def test_reads_csv(tmp_path: Path) -> None:
    pd.DataFrame({"casrn": ["1-2-3"], "hitcall": [1]}).to_csv(tmp_path / "toxcast.csv", index=False)
    rows = StagedSourceAdapter(tmp_path).read_records(_source("toxcast"))
    assert rows == [{"casrn": "1-2-3", "hitcall": 1}]


def test_reads_parquet_and_prefers_it(tmp_path: Path) -> None:
    pd.DataFrame({"sig_id": ["S1"], "GENE_A": [0.5]}).to_parquet(tmp_path / "lincs.parquet")
    # A stale CSV must be ignored in favour of the Parquet.
    pd.DataFrame({"sig_id": ["OLD"], "GENE_A": [9.9]}).to_csv(tmp_path / "lincs.csv", index=False)
    rows = StagedSourceAdapter(tmp_path).read_records(_source("lincs", "signatures"))
    assert rows == [{"sig_id": "S1", "GENE_A": 0.5}]


def test_missing_value_becomes_none(tmp_path: Path) -> None:
    (tmp_path / "toxcast.csv").write_text("casrn,ac50\n1-2-3,\n", encoding="utf-8")
    rows = StagedSourceAdapter(tmp_path).read_records(_source("toxcast"))
    assert rows[0]["ac50"] is None


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        StagedSourceAdapter(tmp_path).read_records(_source("nope"))
