"""End-to-end CLI/runner on fixtures (offline): exit-code contract + artifacts written."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from endoscan_jobs.cli import main
from endoscan_jobs.runner import EXIT_JOB_ERROR, EXIT_OK, EXIT_USAGE, run_job
from endoscan_jobs.storage import LocalStorage

pytest.importorskip("rdkit")  # the coverage job derives InChIKeys

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "jobs"
ZIP = str(FIXTURES / "compara_data.zip")
LINCS = str(FIXTURES / "lincs")


def _run(tmp_path: Path) -> int:
    return main(
        [
            "run",
            "coverage",
            "AR",
            "--source-zip",
            ZIP,
            "--lincs-dir",
            LINCS,
            "--data-root",
            str(tmp_path),
            "--run-id",
            "testrun",
        ]
    )


def test_coverage_failed_qc_exits_zero(tmp_path: Path) -> None:
    # A tiny fixture cannot clear the 40/20 floors -> failed_qc, but the JOB RAN: exit 0.
    assert _run(tmp_path) == EXIT_OK
    result = json.loads((tmp_path / "logs" / "testrun" / "result.json").read_text())
    assert result["status"] == "ok" and result["exit_code"] == 0
    assert result["verdict"] == "failed_qc"  # valid data outcome, not an error
    # Report (json + md) + structured log were written through Storage.
    assert (tmp_path / "reports" / "coverage" / "AR" / "testrun.json").is_file()
    assert (tmp_path / "reports" / "coverage" / "AR" / "testrun.md").is_file()
    assert (tmp_path / "logs" / "testrun" / "events.jsonl").is_file()


def test_report_records_measured_source_and_overlap(tmp_path: Path) -> None:
    _run(tmp_path)
    report = json.loads((tmp_path / "reports" / "coverage" / "AR" / "testrun.json").read_text())
    assert report["source_call_column"] == "AR_Binding"  # measured, not predicted
    assert report["n_structures"] == 12
    # Overlap is computed per context; the broad context sees the most lines.
    broad = next(c for c in report["contexts"] if c["name"].startswith("broad"))
    assert broad["overlap"] >= 1 and broad["overlap"] >= broad["positives"]


def test_job_error_exits_two(tmp_path: Path) -> None:
    # A non-existent source zip -> the job raises -> exit 2 (NOT a failed_qc verdict).
    rc = main(
        [
            "run",
            "coverage",
            "AR",
            "--source-zip",
            str(tmp_path / "nope.zip"),
            "--lincs-dir",
            LINCS,
            "--data-root",
            str(tmp_path),
            "--run-id",
            "errrun",
        ]
    )
    assert rc == EXIT_JOB_ERROR
    result = json.loads((tmp_path / "logs" / "errrun" / "result.json").read_text())
    assert result["status"] == "error" and result["verdict"] is None


def test_unknown_job_is_usage_error(tmp_path: Path) -> None:
    assert main(["run", "frobnicate", "AR", "--data-root", str(tmp_path)]) == EXIT_USAGE


def test_run_job_returns_result_object(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    result = run_job(
        "coverage",
        "AR",
        storage=storage,
        run_id="obj",
        options={"source_zip": ZIP, "lincs_dir": LINCS},
    )
    assert result.exit_code == EXIT_OK and result.report_key.endswith("obj.json")
