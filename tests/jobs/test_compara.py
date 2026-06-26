"""CoMPARA archive parser: zip extraction + measured-AR-call selection (excl. predicted)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pandas as pd
import pytest
from endoscan_jobs.compara import (
    NoMeasuredCallError,
    extract_tables,
    select_measured_ar_call,
    to_binary,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ZIP = REPO_ROOT / "tests" / "fixtures" / "jobs" / "compara_data.zip"


def test_extract_tables_reads_inner_csv() -> None:
    tables = extract_tables(FIXTURE_ZIP)
    assert len(tables) == 1
    name, df = tables[0]
    assert name.endswith(".csv")
    assert "AR_Binding" in df.columns and "InChI" in df.columns


def test_selects_measured_binding_not_predicted() -> None:
    tables = extract_tables(FIXTURE_ZIP)
    selection, df = select_measured_ar_call(tables)
    assert selection.call_col == "AR_Binding"  # measured, preferred (binding)
    assert (
        "pred" not in selection.call_col.lower() and "consensus" not in selection.call_col.lower()
    )
    assert selection.struct_inchi_col == "InChI"
    assert set(selection.value_distribution) <= {"active", "inactive"}


def test_predicted_only_table_is_rejected() -> None:
    df = pd.DataFrame({"InChI": ["InChI=1S/CH4/h1H4"], "AR_Binding_consensus_pred": ["Active"]})
    with pytest.raises(NoMeasuredCallError):
        select_measured_ar_call([("preds.csv", df)])


def test_predicted_file_name_is_skipped_during_extraction() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("AR_consensus_predictions.csv", "InChI,AR_Binding\nInChI=1S/CH4/h1H4,Active\n")
        zf.writestr("measured.csv", "InChI,AR_Binding\nInChI=1S/CH4/h1H4,Active\n")
    tables = extract_tables(buf.getvalue())
    names = [n for n, _ in tables]
    assert names == ["measured.csv"]  # the consensus/predicted file was skipped by name


def test_to_binary_mapping() -> None:
    assert to_binary("Active") == 1 and to_binary("inactive") == 0
    assert to_binary("Inconclusive") is None and to_binary("") is None
