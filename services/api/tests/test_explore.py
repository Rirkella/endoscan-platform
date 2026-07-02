"""Explore routes — served from a committed fixture map; honesty invariants asserted.

Two clients:
  * ``client`` (the REAL committed repo): ER/AR have NO committed explore map yet, so
    ``GET /explore/ER/umap`` is an honest 404 — never fabricated points.
  * ``fixture_client`` (a tmp repo carrying ``models/FIX/explore/*`` from the small committed
    fixture): exercises the full serve + locate path WITHOUT the real non-git data.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from endoscan_api import create_app

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "explore"


@pytest.fixture(scope="module")
def fixture_client() -> TestClient:
    """A client rooted at a tmp repo with an empty registry + the committed FIX explore map."""
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="explore-repo-"))
    (root / "registry" / "models").mkdir(parents=True)
    # Empty endpoint registry -> create_app warms zero models (no model.pkl needed).
    (root / "registry" / "models" / "endpoints.json").write_text(
        json.dumps({"endpoints": []}), encoding="utf-8"
    )
    dest = root / "models" / "FIX" / "explore"
    dest.mkdir(parents=True)
    for name in ("umap.json", "manifest.json", "support.npy"):
        shutil.copy(FIXTURES / name, dest / name)
    return TestClient(create_app(repo_root=root))


def _fixture_signature() -> dict[str, float]:
    """A signature over the fixture map's exact feature set (from its manifest)."""
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    return {gene: 0.0 for gene in manifest["feature_names"]}


# --- serve the map -------------------------------------------------------------------


def test_umap_serves_points_counts_and_manifest(fixture_client: TestClient) -> None:
    r = fixture_client.get("/explore/FIX/umap")
    assert r.status_code == 200
    body = r.json()
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    assert body["context"] == "FIX"
    assert len(body["points"]) == manifest["n_compounds"]
    assert body["counts"]["n_total"] == manifest["n_compounds"]
    # Provenance is surfaced: exact UMAP params + seed + source hash.
    assert body["manifest"]["umap"]["random_state"] == manifest["umap"]["random_state"]
    assert body["manifest"]["source_sha256"] == manifest["source"]["sha256"]
    # Every point is a real training compound with coords (never fabricated).
    pt = body["points"][0]
    assert set(pt) == {"compound_id", "x", "y", "label"}


def test_missing_map_is_honest_404(client: TestClient) -> None:
    # The real committed repo has no explore map for ER yet -> "not yet computed", not points.
    r = client.get("/explore/ER/umap")
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "explore_map_unavailable"
    assert "not yet computed" in body["detail"]


# --- locate a signature --------------------------------------------------------------


def test_locate_returns_neighbors_distances_and_defined_domain_metric(
    fixture_client: TestClient,
) -> None:
    r = fixture_client.post(
        "/explore/locate", json={"context": "FIX", "signature": _fixture_signature()}
    )
    assert r.status_code == 200
    body = r.json()

    # Approximate placement — explicitly flagged, NOT a claimed exact projection.
    assert body["placement"] == "approximate_nearest_neighbor"
    assert set(body["approx_xy"]) == {"x", "y"}

    # Real nearest neighbours with distances (computed in the original gene space) + map coords.
    assert len(body["neighbors"]) >= 1
    n0 = body["neighbors"][0]
    assert set(n0) == {"compound_id", "distance", "x", "y", "label"}
    dists = [n["distance"] for n in body["neighbors"]]
    assert dists == sorted(dists)  # ascending == genuinely nearest-first

    # DEFINED domain metric: distance to the k-th neighbour vs the training reference.
    dom = body["domain"]
    assert dom["metric"] == "distance_to_kth_training_neighbor"
    assert dom["query_kth_distance"] >= 0.0
    assert 0.0 <= dom["percentile"] <= 1.0
    assert set(dom["training_reference_quantiles"]) >= {"min", "median", "max"}


def test_locate_asserts_no_in_domain_verdict_anywhere(fixture_client: TestClient) -> None:
    r = fixture_client.post(
        "/explore/locate", json={"context": "FIX", "signature": _fixture_signature()}
    )
    assert r.status_code == 200
    raw = r.text.lower()
    # The honesty landmine: no asserted in-/out-of-domain flag may exist in the response.
    assert "in_domain" not in raw
    assert "out_of_domain" not in raw
    assert "in-domain" not in raw
    assert "out-of-domain" not in raw


def test_locate_invalid_signature_is_422(fixture_client: TestClient) -> None:
    # Missing schema genes -> the authoritative validator rejects -> 422 (shared handler).
    r = fixture_client.post(
        "/explore/locate", json={"context": "FIX", "signature": {"GENE_A": 0.1}}
    )
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_signature"


def test_locate_missing_map_is_404(fixture_client: TestClient) -> None:
    r = fixture_client.post(
        "/explore/locate", json={"context": "NOPE", "signature": _fixture_signature()}
    )
    assert r.status_code == 404
    assert r.json()["error"] == "explore_map_unavailable"
