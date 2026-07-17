"""Verified measured-signature catalogue routes and integrity boundaries."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from endoscan_api import create_app
from endoscan_api.catalogue_store import load_reference_identities


def test_search_by_name_surfaces_version_identity_and_measured_context(client: TestClient) -> None:
    response = client.get("/catalogue/v1/compounds", params={"query": "caffeic acid"})
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "1.0.0"
    assert body["catalogue_version"] == "2026-07-15"
    assert body["sources"]["identity"]["name"] == "PubChem PUG REST"
    assert len(body["results"]) == 1
    compound = body["results"][0]
    assert compound["pubchem_cid"] == 689043
    assert compound["compound_id"] == "QAIPRVGONGVQAS-DUXPYHPUSA-N"
    signature = compound["signatures"][0]
    assert signature["dataset"] == "LINCS L1000"
    assert signature["accession"] == "GSE92742"
    assert signature["n_genes"] == 978
    assert signature["dose"] is None
    assert signature["timepoint"] is None


def test_search_supports_pubchem_cid_and_full_inchikey(client: TestClient) -> None:
    by_cid = client.get("/catalogue/v1/compounds", params={"query": "42574"}).json()
    assert [item["preferred_name"] for item in by_cid["results"]] == ["Closantel"]

    by_key = client.get(
        "/catalogue/v1/compounds",
        params={"query": "SIYLLGKDQZGJHK-UHFFFAOYSA-N"},
    ).json()
    assert [item["pubchem_cid"] for item in by_key["results"]] == [2335]


def test_bpa_audit_is_identity_known_without_a_compatible_support_profile(
    client: TestClient,
) -> None:
    exact_key = "IISBACLAFKSPIT-UHFFFAOYSA-N"
    for query in ("Bisphenol A", "BPA", exact_key, "6623"):
        body = client.get("/catalogue/v1/compounds", params={"query": query}).json()
        hit = next(item for item in body["results"] if item["compound_id"] == exact_key)
        assert hit["preferred_name"] == "Bisphenol A"
        assert hit["pubchem_cid"] == 6623
        assert hit["availability_status"] == "Identity known — no compatible measured signature"
        assert hit["signatures"] == []
        assert hit["reference_contexts"] == []
        assert "CERAPP" in hit["availability_reason"]
        assert "LINCS" in hit["availability_reason"]
    assert client.get(f"/explore/ER/signatures/{exact_key}").status_code == 404


def test_search_never_substitutes_unmeasured_or_fabricated_results(client: TestClient) -> None:
    response = client.get("/catalogue/v1/compounds", params={"query": "not-a-real-record"})
    assert response.status_code == 200
    assert response.json()["results"] == []


def test_versioned_reference_identity_artifact_is_verified_and_high_coverage(
    repo_root: Path,
) -> None:
    artifact = load_reference_identities(repo_root)
    assert artifact.artifact_version == "2026-07-16"
    assert artifact.statistics == {
        "total_unique_inchikeys": 1023,
        "resolved": 1018,
        "unresolved": 5,
        "resolved_percentage": 99.51,
        "failed_lookup_categories": {
            "not_found": 5,
            "invalid_inchikey": 0,
            "transient_error": 0,
        },
        "pubchem_requests": 103,
    }
    assert len(artifact.identities) == 1023
    assert sum(item.resolution_status == "resolved" for item in artifact.identities) == 1018


def test_signature_detail_is_exact_committed_measured_payload(
    client: TestClient, repo_root: Path
) -> None:
    response = client.get("/catalogue/v1/signatures/lincs-gse92742-caffeic-acid-mcf7-a549")
    assert response.status_code == 200
    body = response.json()
    source = json.loads(
        (repo_root / "apps/web/src/demo-signatures/demo_low_low.json").read_text(encoding="utf-8")
    )
    assert body["signature"] == source["signature"]
    assert body["provenance"] == source["provenance"]
    assert "REAL measured" in body["provenance"]
    assert body["source_url"].endswith("acc=GSE92742")


def test_missing_signature_returns_safe_404_with_request_id(client: TestClient) -> None:
    response = client.get(
        "/catalogue/v1/signatures/unknown", headers={"X-Request-ID": "catalogue-test"}
    )
    assert response.status_code == 404
    assert response.json() == {
        "error": "catalogue_item_not_found",
        "detail": "The requested measured catalogue item was not found.",
        "endpoint_id": None,
        "request_id": "catalogue-test",
    }


def test_query_and_limit_constraints_are_enforced(client: TestClient) -> None:
    assert client.get("/catalogue/v1/compounds", params={"query": "x"}).status_code == 422
    assert client.get("/catalogue/v1/compounds", params={"query": " x"}).status_code == 422
    assert (
        client.get("/catalogue/v1/compounds", params={"query": "caffeic", "limit": 21}).status_code
        == 422
    )


def test_escaped_signature_path_returns_safe_503(tmp_path: Path, repo_root: Path) -> None:
    (tmp_path / "registry/models").mkdir(parents=True)
    (tmp_path / "registry/models/endpoints.json").write_text(
        json.dumps({"endpoints": []}), encoding="utf-8"
    )
    catalogue_dest = tmp_path / "data/catalogue/v1"
    catalogue_dest.mkdir(parents=True)
    doc = json.loads((repo_root / "data/catalogue/v1/catalogue.json").read_text(encoding="utf-8"))
    doc["compounds"][0]["signatures"][0]["signature_path"] = "data/catalogue/v1/catalogue.json"
    (catalogue_dest / "catalogue.json").write_text(json.dumps(doc), encoding="utf-8")
    signature_dest = tmp_path / "apps/web/src/demo-signatures"
    signature_dest.mkdir(parents=True)
    for source in (repo_root / "apps/web/src/demo-signatures").glob("*.json"):
        shutil.copy(source, signature_dest / source.name)

    response = TestClient(create_app(repo_root=tmp_path)).get(
        "/catalogue/v1/compounds", params={"query": "caffeic"}
    )
    assert response.status_code == 503
    assert response.json()["error"] == "catalogue_unavailable"
    assert "escaped" not in response.json()["detail"]
