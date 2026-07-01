"""Phase 2 — /signatures/parse (upload parse+validate) and /analyze (fan-out) API tests.

All against the REAL committed repo (repo_root=REPO_ROOT); one authoritative validator
(align_signature) is reused server-side. Stateless — nothing is persisted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from endoscan_api import create_app

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_GENES: list[str] = json.loads(
    (REPO_ROOT / "models" / "ER" / "feature_schema.json").read_text()
)["features"]


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(repo_root=REPO_ROOT))


def full_json(value: float = 0.0) -> str:
    return json.dumps({g: value for g in SCHEMA_GENES})


def full_csv(value: float = 0.0) -> str:
    return "gene,value\n" + "\n".join(f"{g},{value}" for g in SCHEMA_GENES)


# --- parse: success paths ------------------------------------------------------------


def test_parse_valid_json_aligns(client) -> None:
    r = client.post("/signatures/parse", data={"format": "json", "content": full_json()})
    assert r.status_code == 200
    body = r.json()
    assert body["aligned"] is True
    assert body["preview"]["n_missing"] == 0 and body["preview"]["n_matched"] == 978
    assert set(body["signature"]) == set(SCHEMA_GENES)


def test_parse_valid_csv_file_aligns(client) -> None:
    r = client.post(
        "/signatures/parse",
        data={"format": "csv"},
        files={"file": ("sig.csv", full_csv(), "text/csv")},
    )
    assert r.status_code == 200
    assert r.json()["aligned"] is True


# --- parse: 422 (parsed but content invalid per align_signature) — verbatim ----------


def test_parse_missing_genes_422_verbatim(client) -> None:
    partial = json.dumps({g: 0.0 for g in SCHEMA_GENES[:-3]})
    r = client.post("/signatures/parse", data={"format": "json", "content": partial})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_signature"
    assert "missing 3 of 978 schema genes" in r.json()["detail"]  # align_signature message verbatim


def test_parse_extra_genes_422_then_allow_extra_200(client) -> None:
    obj = {g: 0.0 for g in SCHEMA_GENES}
    obj["NOT_A_GENE"] = 1.0
    reject = client.post("/signatures/parse", data={"format": "json", "content": json.dumps(obj)})
    assert reject.status_code == 422
    assert "not in the schema" in reject.json()["detail"]
    allow = client.post(
        "/signatures/parse",
        data={"format": "json", "content": json.dumps(obj), "allow_extra": "true"},
    )
    assert allow.status_code == 200
    assert allow.json()["aligned"] is True
    assert "NOT_A_GENE" in allow.json()["preview"]["extra_genes"]


def test_parse_non_finite_is_422_at_align(client) -> None:
    # A value that parses as a float but is non-finite (NaN) passes parsing and is rejected by
    # align_signature -> 422 (NOT 400).
    obj = {g: 0.0 for g in SCHEMA_GENES}
    obj[SCHEMA_GENES[0]] = float("nan")
    content = json.dumps(obj)  # Python json emits NaN, which json.loads accepts back
    r = client.post("/signatures/parse", data={"format": "json", "content": content})
    assert r.status_code == 422
    assert "NaN/inf" in r.json()["detail"]


# --- parse: 400 (unparseable / structural) -------------------------------------------


def test_parse_malformed_json_400(client) -> None:
    r = client.post("/signatures/parse", data={"format": "json", "content": "{not json"})
    assert r.status_code == 400 and r.json()["error"] == "malformed_upload"


def test_parse_csv_non_numeric_cell_400(client) -> None:
    r = client.post(
        "/signatures/parse", data={"format": "csv", "content": "gene,value\nA1BG,abc\n"}
    )
    assert r.status_code == 400
    assert "not numeric" in r.json()["detail"]


def test_parse_csv_duplicate_gene_400(client) -> None:
    r = client.post(
        "/signatures/parse", data={"format": "csv", "content": "gene,value\nA1BG,0.1\nA1BG,0.2\n"}
    )
    assert r.status_code == 400 and "duplicate gene" in r.json()["detail"]


def test_parse_empty_400(client) -> None:
    r = client.post("/signatures/parse", data={"format": "json", "content": "   "})
    assert r.status_code == 400


def test_parse_wrong_columns_400(client) -> None:
    r = client.post("/signatures/parse", data={"format": "csv", "content": "foo,bar\n1,2\n"})
    assert r.status_code == 400 and "gene column" in r.json()["detail"]


def test_parse_unknown_format_400(client) -> None:
    r = client.post("/signatures/parse", data={"format": "xlsx", "content": "x"})
    assert r.status_code == 400 and "unsupported format" in r.json()["detail"]


# --- parse: multi-column CSV ---------------------------------------------------------


def test_parse_multicolumn_csv_reports_samples(client) -> None:
    rows = "\n".join(f"{g},0.0,1.0" for g in SCHEMA_GENES)
    csv = "gene,ctrl_24h,dose_24h\n" + rows
    no_sample = client.post("/signatures/parse", data={"format": "csv", "content": csv})
    assert no_sample.status_code == 200
    body = no_sample.json()
    assert body["aligned"] is False and body["preview"]["needs_sample"] is True
    assert body["preview"]["samples"] == ["ctrl_24h", "dose_24h"]
    assert body["signature"] is None  # no fabricated signature

    chosen = client.post(
        "/signatures/parse", data={"format": "csv", "content": csv, "sample": "dose_24h"}
    )
    assert chosen.status_code == 200 and chosen.json()["aligned"] is True


# --- analyze: fan-out + per-endpoint isolation ---------------------------------------


def test_analyze_runs_across_all_endpoints(client) -> None:
    r = client.post("/analyze", json={"signature": {g: 0.0 for g in SCHEMA_GENES}})
    assert r.status_code == 200
    results = r.json()["results"]
    ids = {e["endpoint_id"] for e in results}
    assert {"ER", "AR"} <= ids
    assert all(e["ok"] and e["result"] is not None for e in results)


def test_analyze_per_endpoint_isolation(client, monkeypatch) -> None:
    # One endpoint erroring must not sink the others.
    from endoscan_api.routes import analyze as analyze_route
    from endoscan_core.inference import SignatureValidationError

    real_predict = analyze_route.predict

    def flaky(endpoint_id, signature, **kw):
        if endpoint_id == "AR":
            raise SignatureValidationError("synthetic AR failure")
        return real_predict(endpoint_id, signature, **kw)

    monkeypatch.setattr(analyze_route, "predict", flaky)
    r = client.post("/analyze", json={"signature": {g: 0.0 for g in SCHEMA_GENES}})
    assert r.status_code == 200
    by_id = {e["endpoint_id"]: e for e in r.json()["results"]}
    assert by_id["ER"]["ok"] is True and by_id["ER"]["result"] is not None
    assert by_id["AR"]["ok"] is False and by_id["AR"]["error"]["error"] == "invalid_signature"


def test_analyze_malformed_body_422(client) -> None:
    assert client.post("/analyze", json={}).status_code == 422  # missing 'signature'


# --- regression: existing routes unchanged -------------------------------------------


def test_existing_routes_unchanged(client) -> None:
    assert client.get("/endpoints").status_code == 200
    assert (
        client.post(
            "/predict", json={"endpoint_id": "AR", "signature": {g: 0.0 for g in SCHEMA_GENES}}
        ).status_code
        == 200
    )
