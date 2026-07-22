"""build_explore_map job — real-data-only, deterministic, honest labels (on a fixture matrix).

The heavy UMAP fit runs here on a small synthetic ``signatures.parquet`` (the real ER/AR
maps are built on the server over the real curated data and transferred separately). We
assert: points count == compounds, the manifest is complete + carries the DEFINED domain
metric, two seeded runs are byte-identical (determinism), absent signatures fail honestly,
and labels colour when they join / stay uncoloured (never fabricated) when they don't.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from endoscan_jobs.cli import main
from endoscan_jobs.runner import EXIT_JOB_ERROR, EXIT_OK

pytest.importorskip("umap")  # the seeded UMAP fit (jobs-only dependency)

GENES = ["GENE_A", "GENE_B", "GENE_C", "GENE_D"]
N = 12


def _write_signatures(root: Path, target: str) -> list[str]:
    """A deterministic 2-cluster fixture matrix so a real embedding is meaningful."""
    rng = np.random.RandomState(0)
    ids = [f"CID_{i:02d}" for i in range(N)]
    rows = [[ids[i], *[(0.0 if i < N // 2 else 5.0) + rng.rand() for _ in GENES]] for i in range(N)]
    df = pd.DataFrame(rows, columns=["compound_id", *GENES])
    dest = root / "curated" / target
    dest.mkdir(parents=True)
    df.to_parquet(dest / "signatures.parquet", index=False)
    return ids


def _run(root: Path, target: str, *, run_id: str, labels: Path | None = None) -> int:
    argv = [
        "run", "build_explore_map", target,
        "--data-root", str(root), "--run-id", run_id,
        "--umap-neighbors", "4", "--nn-k", "3",
    ]  # fmt: skip
    if labels is not None:
        argv += ["--labels-csv", str(labels)]
    return main(argv)


def _artifacts(root: Path, target: str) -> tuple[dict, dict, bytes]:
    base = root / "artifacts" / target / "explore"
    umap_doc = json.loads((base / "umap.json").read_text())
    manifest = json.loads((base / "manifest.json").read_text())
    support = (base / "support.npy").read_bytes()
    return umap_doc, manifest, support


def test_points_count_and_manifest_complete(tmp_path: Path) -> None:
    ids = _write_signatures(tmp_path, "FIX")
    assert _run(tmp_path, "FIX", run_id="one") == EXIT_OK

    umap_doc, manifest, _ = _artifacts(tmp_path, "FIX")
    assert len(umap_doc["points"]) == N  # one point per real compound
    assert umap_doc["counts"]["n_total"] == N
    assert [p["compound_id"] for p in umap_doc["points"]] == ids

    # Manifest carries full provenance + the DEFINED domain metric.
    assert manifest["feature_names"] == GENES
    assert manifest["compound_ids"] == ids
    assert manifest["umap"]["random_state"] == 42
    assert manifest["umap"]["n_neighbors"] == 4
    assert manifest["source"]["sha256"]  # source-data hash present
    assert manifest["support_sha256"]
    dm = manifest["domain_metric"]
    assert dm["metric"] == "distance_to_kth_training_neighbor" and dm["k"] == 3
    assert len(dm["training_kth_nn_distances"]) == N
    assert set(dm["quantiles"]) == {"min", "p25", "median", "p75", "p95", "max"}
    # No asserted domain verdict is baked into the artifact.
    assert "in_domain" not in json.dumps(manifest)


def test_two_seeded_runs_are_identical(tmp_path: Path) -> None:
    _write_signatures(tmp_path, "FIX")
    assert _run(tmp_path, "FIX", run_id="a") == EXIT_OK
    umap_a, man_a, support_a = _artifacts(tmp_path, "FIX")
    assert _run(tmp_path, "FIX", run_id="b") == EXIT_OK
    umap_b, man_b, support_b = _artifacts(tmp_path, "FIX")

    assert umap_a == umap_b  # identical embedding + counts
    assert support_a == support_b  # identical support bytes
    man_a.pop("built_at")  # the only non-deterministic field (a real timestamp)
    man_b.pop("built_at")
    assert man_a == man_b


def test_labels_colour_the_map_when_they_join(tmp_path: Path) -> None:
    ids = _write_signatures(tmp_path, "FIX")
    labels = tmp_path / "labels.csv"
    pd.DataFrame({"compound_id": ids, "label": [1 if i < N // 2 else 0 for i in range(N)]}).to_csv(
        labels, index=False
    )
    assert _run(tmp_path, "FIX", run_id="lab", labels=labels) == EXIT_OK

    umap_doc, manifest, _ = _artifacts(tmp_path, "FIX")
    assert manifest["labels"]["status"] == "joined"
    assert umap_doc["counts"]["n_active"] == N // 2
    assert umap_doc["counts"]["n_inactive"] == N // 2
    assert {p["label"] for p in umap_doc["points"]} == {"active", "inactive"}


def test_unjoinable_labels_ship_uncoloured_never_fabricated(tmp_path: Path) -> None:
    _write_signatures(tmp_path, "FIX")
    labels = tmp_path / "labels.csv"
    # Non-matching compound_ids -> 0 joined -> uncoloured (null labels), not fabricated.
    pd.DataFrame({"compound_id": ["OTHER_1", "OTHER_2"], "label": [1, 0]}).to_csv(
        labels, index=False
    )
    assert _run(tmp_path, "FIX", run_id="unjoin", labels=labels) == EXIT_OK

    umap_doc, manifest, _ = _artifacts(tmp_path, "FIX")
    assert manifest["labels"]["status"] == "unjoined"
    assert umap_doc["counts"]["n_unlabeled"] == N
    assert all(p["label"] is None for p in umap_doc["points"])


def test_missing_signatures_fails_honestly_no_synthetic_matrix(tmp_path: Path) -> None:
    # No curated signatures.parquet at all -> JobError (exit 2). The map is never invented.
    assert _run(tmp_path, "MISSING", run_id="none") == EXIT_JOB_ERROR
    result = json.loads((tmp_path / "logs" / "none" / "result.json").read_text())
    assert result["status"] == "error" and result["verdict"] is None
    assert not (tmp_path / "artifacts" / "MISSING" / "explore" / "umap.json").exists()
