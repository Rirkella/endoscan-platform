"""extract_signatures job e2e (offline): curated fused contract, cache-hit skip, exit codes."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from endoscan_jobs.cli import main
from endoscan_jobs.gctx import write_synthetic_gctx
from endoscan_jobs.runner import EXIT_JOB_ERROR, EXIT_OK

from endoscan_core.datasets import StagedSourceAdapter, signature_retriever
from endoscan_core.datasets.sources import SourceEntry

pytest.importorskip("rdkit")  # labels come from the CoMPARA SDF
pytest.importorskip("h5py")  # the gctx slice

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "jobs"
ZIP = str(FIXTURES / "compara_data.zip")
LINCS = str(FIXTURES / "lincs")

# trt_cp signatures present in the LINCS fixture (sig_info.txt).
_SIG_IDS = [f"SIG_{i:04d}" for i in range(1, 13)]
# Compounds that end up in the curated matrix for cells {VCAP, A549} under
# functional_modulation: bisphenol_A (VCAP+A549), nonylphenol, phenol, aniline, benzene.
_EXPECTED_COMPOUNDS = {"bisphenol_A", "nonylphenol", "phenol", "aniline", "benzene"}


def _expected_inchikeys() -> set[str]:
    pert = pd.read_csv(Path(LINCS) / "pert_info.txt", sep="\t")
    return set(pert[pert["pert_iname"].isin(_EXPECTED_COMPOUNDS)]["inchi_key"])


def _make_gctx_and_gene_info(tmp_path: Path) -> tuple[Path, Path, list[str]]:
    """A synthetic gctx over the fixture sig_ids + a gene_info with 3/4 landmark genes."""
    gene_ids = ["100", "200", "300", "400"]
    frame = pd.DataFrame(
        # deterministic, gene-distinct values so a mis-slice would be visible
        [[10 * g + s for s in range(len(_SIG_IDS))] for g in range(len(gene_ids))],
        index=gene_ids,
        columns=_SIG_IDS,
        dtype=float,
    )
    gctx_path = tmp_path / "synthetic_level5.gctx"
    write_synthetic_gctx(gctx_path, frame)  # signatures-major (real layout)

    gene_info = tmp_path / "gene_info.txt"
    gene_info.write_text(
        "pr_gene_id\tpr_gene_symbol\tpr_is_lm\n"
        "100\tGENE_A\t1\n200\tGENE_B\t1\n300\tGENE_C\t0\n400\tGENE_D\t1\n",
        encoding="utf-8",
    )
    return gctx_path, gene_info, ["GENE_A", "GENE_B", "GENE_D"]


def _run(tmp_path: Path, gctx: Path, gene_info: Path, *, run_id: str, cells: str = "VCAP,A549"):
    return main(
        [
            "run", "extract_signatures", "AR",
            "--source-zip", ZIP,
            "--lincs-dir", LINCS,
            "--gene-info", str(gene_info),
            "--gctx-path", str(gctx),
            "--cell-lines", cells,
            "--slice-batch", "2",  # force multiple batches in CI
            "--data-root", str(tmp_path),
            "--run-id", run_id,
        ]
    )  # fmt: skip


def test_curated_parquet_matches_fused_contract(tmp_path: Path) -> None:
    gctx, gene_info, features = _make_gctx_and_gene_info(tmp_path)
    assert _run(tmp_path, gctx, gene_info, run_id="x") == EXIT_OK

    curated = tmp_path / "curated" / "AR" / "signatures.parquet"
    assert curated.is_file()
    fused = pd.read_parquet(curated)
    # Fused compound-level matrix: compound_id + the 3 landmark gene symbols, no per-sig cols.
    assert list(fused.columns) == ["compound_id", *features]
    assert set(fused["compound_id"]) == _expected_inchikeys()
    assert len(fused) == len(_EXPECTED_COMPOUNDS)  # bisphenol_A's VCAP+A549 fused to ONE row

    # The signature_retriever fused path consumes it unchanged (no reader change).
    root = tmp_path / "curated" / "AR"
    entry = SourceEntry(
        id="signatures", name="curated", type="signatures",
        access_method="staged", provenance="test", version="0",
    )  # fmt: skip
    sig_set = signature_retriever([entry], StagedSourceAdapter(root))
    assert sig_set.granularity == "compound"
    assert sig_set.feature_names == features
    assert all(r.compound_id_type == "inchikey" for r in sig_set.records)
    assert {r.compound_id for r in sig_set.records} == _expected_inchikeys()


def test_manifest_cache_hit_skips_reslice(tmp_path: Path) -> None:
    gctx, gene_info, _ = _make_gctx_and_gene_info(tmp_path)
    assert _run(tmp_path, gctx, gene_info, run_id="first") == EXIT_OK
    curated = tmp_path / "curated" / "AR" / "signatures.parquet"
    manifest = json.loads((tmp_path / "curated" / "AR" / "slice_manifest.json").read_text())
    assert manifest["cache_key"]["cell_lines"] == ["A549", "VCAP"]
    mtime_before = curated.stat().st_mtime_ns

    # Second identical run: the manifest matches -> the slice is SKIPPED, parquet untouched.
    result = json.loads(_result_json(tmp_path, gctx, gene_info, run_id="second"))
    assert result["exit_code"] == EXIT_OK and result["verdict"] == "cached"
    assert curated.stat().st_mtime_ns == mtime_before  # not rewritten

    # A different context (different cache key) re-slices instead of reusing.
    third = json.loads(_result_json(tmp_path, gctx, gene_info, run_id="third", cells="VCAP"))
    assert third["verdict"] == "extracted"


def _result_json(tmp_path, gctx, gene_info, *, run_id, cells="VCAP,A549") -> str:
    _run(tmp_path, gctx, gene_info, run_id=run_id, cells=cells)
    return (tmp_path / "logs" / run_id / "result.json").read_text()


def test_empty_overlap_is_failed_qc_exit_zero(tmp_path: Path) -> None:
    # A cell line absent from the LINCS fixture -> 0 profiled signatures -> nothing to slice.
    gctx, gene_info, _ = _make_gctx_and_gene_info(tmp_path)
    assert _run(tmp_path, gctx, gene_info, run_id="empty", cells="HEPG2") == EXIT_OK
    result = json.loads((tmp_path / "logs" / "empty" / "result.json").read_text())
    assert result["status"] == "ok" and result["verdict"] == "failed_qc"
    assert not (tmp_path / "curated" / "AR" / "signatures.parquet").exists()


def test_missing_gctx_is_job_error_exit_two(tmp_path: Path) -> None:
    _gctx, gene_info, _ = _make_gctx_and_gene_info(tmp_path)
    rc = main(
        [
            "run", "extract_signatures", "AR",
            "--source-zip", ZIP, "--lincs-dir", LINCS, "--gene-info", str(gene_info),
            "--gctx-path", str(tmp_path / "does_not_exist.gctx"),
            "--cell-lines", "VCAP,A549",
            "--data-root", str(tmp_path), "--run-id", "nogctx",
        ]
    )  # fmt: skip
    assert rc == EXIT_JOB_ERROR
    result = json.loads((tmp_path / "logs" / "nogctx" / "result.json").read_text())
    assert result["status"] == "error" and result["verdict"] is None
