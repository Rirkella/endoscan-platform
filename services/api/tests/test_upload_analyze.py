"""Endpoint-aware upload parsing and isolated analysis integration tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from endoscan_api import create_app

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_GENES: list[str] = json.loads(
    (REPO_ROOT / "models" / "ER" / "feature_schema.json").read_text(encoding="utf-8")
)["features"]


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(repo_root=REPO_ROOT))


def full_json(value: float = 0.0) -> str:
    return json.dumps({gene: value for gene in SCHEMA_GENES})


def full_delimited(delimiter: str, value: float = 0.0) -> str:
    return f"gene{delimiter}value\n" + "\n".join(
        f"{gene}{delimiter}{value}" for gene in SCHEMA_GENES
    )


@pytest.mark.parametrize(
    ("fmt", "content"),
    [
        ("json", full_json()),
        ("csv", full_delimited(",")),
        ("tsv", full_delimited("\t")),
    ],
)
def test_parse_supported_formats_checks_every_endpoint(client, fmt, content) -> None:
    response = client.post("/signatures/parse", data={"format": fmt, "content": content})
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["preview"]["n_detected"] == len(SCHEMA_GENES)
    assert {item["endpoint_id"] for item in body["compatibility"]} >= {"ER", "AR"}
    assert set(body["compatible_endpoint_ids"]) >= {"ER", "AR"}
    assert set(body["signature"]) == set(SCHEMA_GENES)


def test_parse_file_uses_same_contract(client) -> None:
    response = client.post(
        "/signatures/parse",
        data={"format": "csv"},
        files={"file": ("signature.csv", full_delimited(","), "text/csv")},
    )
    assert response.status_code == 200
    assert response.json()["compatible_endpoint_ids"]


def test_all_named_example_files_round_trip_through_multipart(client) -> None:
    examples = REPO_ROOT / "apps" / "web" / "public" / "examples"
    manifest = json.loads((examples / "manifest.json").read_text(encoding="utf-8"))
    catalogue = json.loads(
        (REPO_ROOT / "data/catalogue/v1/catalogue.json").read_text(encoding="utf-8")
    )
    names = {item["name"] for item in manifest["examples"]}
    assert {"Caffeic Acid", "Closantel", "Benzethonium"} <= names
    assert len(manifest["examples"]) == 4
    for metadata, compound in zip(manifest["examples"], catalogue["compounds"], strict=True):
        path = examples / metadata["filename"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata["file_sha256"]
        response = client.post(
            "/signatures/parse",
            data={"format": metadata["format"]},
            files={"file": (metadata["filename"], path.read_bytes(), "text/plain")},
        )
        assert response.status_code == 200
        body = response.json()
        source_path = REPO_ROOT / compound["signatures"][0]["signature_path"]
        source = json.loads(source_path.read_text(encoding="utf-8"))
        assert (
            hashlib.sha256(source_path.read_bytes()).hexdigest()
            == metadata["source_signature_sha256"]
        )
        assert body["signature"] == source["signature"]
        assert set(body["compatible_endpoint_ids"]) >= {"ER", "AR"}
        assert metadata["name"] == compound["preferred_name"]


def test_caffeic_acid_real_multipart_stack_explains_every_declared_endpoint(client) -> None:
    path = REPO_ROOT / "apps/web/public/examples/lincs-caffeic-acid-mcf7-a549.csv"
    parsed = client.post(
        "/signatures/parse",
        data={"format": "csv"},
        files={"file": (path.name, path.read_bytes(), "text/csv")},
    )
    assert parsed.status_code == 200
    signature = parsed.json()["signature"]
    health = client.get("/health").json()["explanation_capabilities"]
    for endpoint_id in ("ER", "AR"):
        assert health[endpoint_id]["available"] is True
        payload = {"endpoint_id": endpoint_id, "signature": signature}
        predicted = client.post("/predict", json=payload)
        explained = client.post("/explain", json=payload)
        assert predicted.status_code == 200
        assert explained.status_code == 200
        assert explained.json()["top_contributors"]


def test_parse_incompatible_signature_is_200_with_per_endpoint_reasons(client) -> None:
    partial = json.dumps({gene: 0.0 for gene in SCHEMA_GENES[:-3]})
    response = client.post("/signatures/parse", data={"format": "json", "content": partial})
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert body["compatible_endpoint_ids"] == []
    assert all(not item["compatible"] for item in body["compatibility"])
    assert all(item["n_missing"] == 3 for item in body["compatibility"])


def test_parse_extra_genes_can_be_assessed_with_allow_extra(client) -> None:
    signature = {gene: 0.0 for gene in SCHEMA_GENES}
    signature["NOT_A_GENE"] = 1.0
    rejected = client.post(
        "/signatures/parse", data={"format": "json", "content": json.dumps(signature)}
    ).json()
    assert rejected["ready"] is False
    accepted = client.post(
        "/signatures/parse",
        data={"format": "json", "content": json.dumps(signature), "allow_extra": "true"},
    ).json()
    assert accepted["ready"] is True
    assert all(item["n_extra"] == 1 for item in accepted["compatibility"])


def test_parse_ten_heterogeneous_endpoint_schemas(client, monkeypatch) -> None:
    from endoscan_api.routes import signatures as route
    from endoscan_core.inference import SignatureValidationError

    entries = [
        SimpleNamespace(
            endpoint_id=f"E{i}", biological_target=f"Target {i}", feature_schema_path=f"E{i}.json"
        )
        for i in range(10)
    ]

    def fake_schema(path):
        index = int(path.stem[1:])
        return SimpleNamespace(features=("A", "B") if index % 2 == 0 else ("A", "C"))

    def fake_align(mapping, schema, *, allow_extra=False):
        missing = set(schema.features) - set(mapping)
        if missing:
            raise SignatureValidationError(f"missing {len(missing)} genes")
        return [[mapping[gene] for gene in schema.features]], True

    monkeypatch.setattr(route, "list_endpoints", lambda **_: entries)
    monkeypatch.setattr(route, "load_feature_schema", fake_schema)
    monkeypatch.setattr(route, "align_signature", fake_align)
    response = client.post(
        "/signatures/parse", data={"format": "json", "content": '{"A": 1, "B": 2}'}
    )
    body = response.json()
    assert len(body["compatibility"]) == 10
    assert body["compatible_endpoint_ids"] == [f"E{i}" for i in range(0, 10, 2)]


@pytest.mark.parametrize(
    "data",
    [
        {"format": "json", "content": "{not json"},
        {"format": "csv", "content": "gene,value\nA1BG,abc\n"},
        {"format": "csv", "content": "gene,value\nA1BG,0.1\nA1BG,0.2\n"},
        {"format": "xlsx", "content": "x"},
    ],
)
def test_parse_malformed_inputs_are_safe_400_with_request_id(client, data) -> None:
    response = client.post("/signatures/parse", data=data)
    assert response.status_code == 400
    assert response.json()["error"] == "malformed_upload"
    assert response.json()["request_id"]


def test_parse_multicolumn_requires_explicit_sample(client) -> None:
    rows = "\n".join(f"{gene},0.0,1.0" for gene in SCHEMA_GENES)
    content = "gene,ctrl_24h,dose_24h\n" + rows
    pending = client.post(
        "/signatures/parse", data={"format": "csv", "content": content}
    ).json()
    assert pending["ready"] is False
    assert pending["preview"]["needs_sample"] is True
    assert pending["compatibility"] == [] and pending["signature"] is None
    chosen = client.post(
        "/signatures/parse",
        data={"format": "csv", "content": content, "sample": "dose_24h"},
    )
    assert chosen.status_code == 200 and chosen.json()["ready"] is True


def test_request_body_limit_returns_413_with_request_id(client) -> None:
    response = client.post(
        "/analyze",
        content=b"x" * (6 * 1024 * 1024 + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"
    assert response.json()["request_id"]


def test_gene_count_limit_is_enforced_before_endpoint_work(client) -> None:
    content = json.dumps({f"GENE_{index}": 0.0 for index in range(10_001)})
    response = client.post(
        "/signatures/parse", data={"format": "json", "content": content}
    )
    assert response.status_code == 400
    assert "10000 gene limit" in response.json()["detail"]


def test_endpoint_selection_count_limit_is_422(client) -> None:
    response = client.post(
        "/analyze",
        json={"signature": {"A": 0.0}, "endpoint_ids": [f"E{i}" for i in range(101)]},
    )
    assert response.status_code == 422
    assert response.json()["request_id"]


def test_analyze_runs_only_selected_endpoints(client) -> None:
    response = client.post(
        "/analyze",
        json={"signature": {gene: 0.0 for gene in SCHEMA_GENES}, "endpoint_ids": ["AR"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert [item["endpoint_id"] for item in body["results"]] == ["AR"]
    assert body["results"][0]["model_version"]
    assert body["results"][0]["source_refs"]
    assert body["summary"] == {"requested": 1, "succeeded": 1, "failed": 0, "status": "ok"}


def test_analyze_per_endpoint_isolation_and_partial_summary(client, monkeypatch) -> None:
    from endoscan_api.routes import analyze as route
    from endoscan_core.inference import SignatureValidationError

    real_predict = route.predict

    def flaky(endpoint_id, signature, **kwargs):
        if endpoint_id == "AR":
            raise SignatureValidationError("synthetic AR failure")
        return real_predict(endpoint_id, signature, **kwargs)

    monkeypatch.setattr(route, "predict", flaky)
    response = client.post(
        "/analyze", json={"signature": {gene: 0.0 for gene in SCHEMA_GENES}}
    )
    body = response.json()
    by_id = {item["endpoint_id"]: item for item in body["results"]}
    assert by_id["ER"]["ok"] is True
    assert by_id["AR"]["ok"] is False
    assert by_id["AR"]["error"]["request_id"]
    assert body["summary"]["status"] == "partial"


def test_analyze_all_failed_has_dedicated_summary(client, monkeypatch) -> None:
    from endoscan_api.routes import analyze as route
    from endoscan_core.inference import SignatureValidationError

    monkeypatch.setattr(
        route,
        "predict",
        lambda *args, **kwargs: (_ for _ in ()).throw(SignatureValidationError("failure")),
    )
    body = client.post(
        "/analyze",
        json={"signature": {gene: 0.0 for gene in SCHEMA_GENES}, "endpoint_ids": ["ER", "AR"]},
    ).json()
    assert body["summary"] == {
        "requested": 2,
        "succeeded": 0,
        "failed": 2,
        "status": "all_failed",
    }


def test_validation_error_is_safe_and_has_request_id(client) -> None:
    response = client.post("/analyze", json={})
    assert response.status_code == 422
    assert response.json()["detail"] == "The request body does not match the API contract."
    assert response.json()["request_id"]
