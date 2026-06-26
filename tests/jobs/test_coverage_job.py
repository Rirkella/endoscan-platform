"""Coverage-job context selection: the VCaP+A549 preset, resolve_contexts, --contexts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from endoscan_jobs.cli import build_parser, main
from endoscan_jobs.jobs.coverage import DEFAULT_CONTEXTS, DEFAULT_PROBE_LINES, resolve_contexts

pytest.importorskip("rdkit")  # the coverage job derives InChIKeys

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "jobs"
ZIP = str(FIXTURES / "compara_data.zip")
LINCS = str(FIXTURES / "lincs")


# --- the VCaP+A549 preset --------------------------------------------------------------


def test_vcap_a549_is_a_default_preset() -> None:
    assert DEFAULT_CONTEXTS["VCaP+A549 (mixed)"] == ["VCAP", "A549"]
    # Its cells are covered by the probe lines (A549 already present).
    for cell in DEFAULT_CONTEXTS["VCaP+A549 (mixed)"]:
        assert cell in DEFAULT_PROBE_LINES


def test_redundant_vcap_lncap_preset_dropped() -> None:
    # LNCaP has zero LINCS trt_cp signatures, so VCaP+LNCaP was numerically VCaP alone.
    assert "VCaP+LNCaP (androgen)" not in DEFAULT_CONTEXTS
    # The kept presets: VCaP (androgen), VCaP+A549 (mixed), MCF7/A549 (ER-style), broad.
    assert set(DEFAULT_CONTEXTS) == {
        "VCaP (androgen)",
        "VCaP+A549 (mixed)",
        "MCF7/A549 (ER-style)",
        "broad (VCaP+MCF7+A549+PC3)",
    }


# --- resolve_contexts ------------------------------------------------------------------


def test_resolve_contexts_default_includes_vcap_a549() -> None:
    resolved = resolve_contexts(None)
    assert resolved["VCaP+A549 (mixed)"] == ["VCAP", "A549"]
    assert resolved == {k: list(v) for k, v in DEFAULT_CONTEXTS.items()}
    # Empty/whitespace spec also falls back to the defaults.
    assert resolve_contexts("   ") == {k: list(v) for k, v in DEFAULT_CONTEXTS.items()}


def test_resolve_contexts_explicit_pairs() -> None:
    resolved = resolve_contexts("VCaP+A549 (mixed):vcap,a549;solo:VCAP")
    # Cells are upper-cased; entry order preserved.
    assert resolved == {"VCaP+A549 (mixed)": ["VCAP", "A549"], "solo": ["VCAP"]}


def test_resolve_contexts_bare_preset_name() -> None:
    assert resolve_contexts("VCaP+A549 (mixed)") == {"VCaP+A549 (mixed)": ["VCAP", "A549"]}


def test_resolve_contexts_unknown_preset_raises() -> None:
    with pytest.raises(ValueError, match="unknown context preset"):
        resolve_contexts("not-a-preset")


def test_resolve_contexts_malformed_entry_raises() -> None:
    with pytest.raises(ValueError, match="invalid --contexts entry"):
        resolve_contexts("nameonly:")


# --- the --contexts CLI argument -------------------------------------------------------


def test_contexts_cli_argument_parses() -> None:
    args = build_parser().parse_args(
        ["run", "coverage", "AR", "--contexts", "VCaP+A549 (mixed):VCAP,A549"]
    )
    assert args.contexts == "VCaP+A549 (mixed):VCAP,A549"
    # Optional: absent -> None (job then uses the defaults).
    assert build_parser().parse_args(["run", "coverage", "AR"]).contexts is None


def test_contexts_cli_scopes_the_report(tmp_path: Path) -> None:
    # Running with only the VCaP+A549 preset yields exactly that one scored context.
    rc = main(
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
            "ctxrun",
            "--contexts",
            "VCaP+A549 (mixed)",
        ]
    )
    assert rc == 0  # the job RAN (failed_qc on the tiny fixture is still exit 0)
    report = json.loads((tmp_path / "reports" / "coverage" / "AR" / "ctxrun.json").read_text())
    assert [c["name"] for c in report["contexts"]] == ["VCaP+A549 (mixed)"]
