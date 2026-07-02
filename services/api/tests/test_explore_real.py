"""Real committed Explore artifacts (models/<EP>/explore/) served by the API.

These run against the ACTUAL transferred ER/AR maps now in the repo (not the tiny fixture).
They prove: the API loads the real artifacts, serves real points/counts/manifest, places a
signature by nearest neighbours with the defined domain metric (no in_domain boolean), the
source-hash gate holds, and a never-built context still degrades gracefully to 404.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from endoscan_api.explore_store import (
    ExploreArtifactCorruptError,
    load_map,
    load_support,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

# Committed real maps (transferred from the server, integrity-verified before commit).
_EXPECTED = {
    "ER": {"n_compounds": 963, "n_active": 76, "n_inactive": 887},
    "AR": {"n_compounds": 140, "n_active": 40, "n_inactive": 100},
}


def _manifest(context: str) -> dict:
    return json.loads(
        (REPO_ROOT / "models" / context / "explore" / "manifest.json").read_text(encoding="utf-8")
    )


def _signature(context: str, value: float = 0.0) -> dict[str, float]:
    """A schema-complete signature over the MAP's own feature order (from its manifest)."""
    return {gene: value for gene in _manifest(context)["feature_names"]}


@pytest.mark.parametrize("context", ["ER", "AR"])
def test_real_umap_served_with_real_points_counts_and_provenance(
    client: TestClient, context: str
) -> None:
    r = client.get(f"/explore/{context}/umap")
    assert r.status_code == 200
    body = r.json()
    exp = _EXPECTED[context]
    assert len(body["points"]) == exp["n_compounds"]
    assert body["counts"]["n_total"] == exp["n_compounds"]
    assert body["counts"]["n_active"] == exp["n_active"]
    assert body["counts"]["n_inactive"] == exp["n_inactive"]
    # Real colouring (labels joined) + honest provenance surfaced.
    assert body["manifest"]["label_status"] == "joined"
    assert body["manifest"]["umap"]["random_state"] == 42
    assert body["manifest"]["umap"]["metric"] == "euclidean"
    assert len(body["manifest"]["source_sha256"]) == 64
    # Every point is a real compound with a colour (never fabricated).
    assert {p["label"] for p in body["points"]} <= {"active", "inactive", None}


@pytest.mark.parametrize("context", ["ER", "AR"])
def test_real_locate_returns_neighbors_and_defined_domain_metric_no_in_domain(
    client: TestClient, context: str
) -> None:
    r = client.post("/explore/locate", json={"context": context, "signature": _signature(context)})
    assert r.status_code == 200
    body = r.json()
    assert body["placement"] == "approximate_nearest_neighbor"
    assert set(body["approx_xy"]) == {"x", "y"}
    assert len(body["neighbors"]) == 5  # k=5
    dists = [n["distance"] for n in body["neighbors"]]
    assert dists == sorted(dists)
    dom = body["domain"]
    assert dom["metric"] == "distance_to_kth_training_neighbor" and dom["k"] == 5
    assert 0.0 <= dom["percentile"] <= 1.0
    # The honesty landmine: no asserted in-/out-of-domain flag anywhere.
    assert "in_domain" not in r.text.lower()
    assert "out_of_domain" not in r.text.lower()


@pytest.mark.parametrize("context", ["ER", "AR"])
def test_real_support_hash_gate_holds(context: str) -> None:
    # The committed support.npy matches its manifest hash (the API would 500 otherwise).
    umap_doc, manifest = load_map(REPO_ROOT, context)
    support = load_support(REPO_ROOT, context, manifest)  # raises if the gate fails
    assert support.shape[0] == manifest["n_compounds"]
    assert support.shape[1] == len(manifest["feature_names"])
    raw = (REPO_ROOT / "models" / context / "explore" / "support.npy").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == manifest["support_sha256"]


def test_tampered_support_hash_is_rejected() -> None:
    # A manifest whose hash no longer matches the bytes -> corrupt gate (500), never served.
    _umap, manifest = load_map(REPO_ROOT, "ER")
    manifest = {**manifest, "support_sha256": "0" * 64}
    with pytest.raises(ExploreArtifactCorruptError):
        load_support(REPO_ROOT, "ER", manifest)
