"""CORS middleware: an explicit allow-list (default = local Vite origins), never a wildcard.

Additive middleware only — routes/behavior are unchanged (see the rest of the suite). These
assert the Access-Control-Allow-Origin behavior for allowed vs non-allowed origins, preflight,
and the ENDOSCAN_CORS_ORIGINS override.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from endoscan_api import create_app
from endoscan_api.app import DEFAULT_CORS_ORIGINS, _cors_origins

REPO_ROOT = Path(__file__).resolve().parents[3]
ALLOWED = "http://127.0.0.1:5174"  # in the default local Vite list
FOREIGN = "https://evil.example.com"


def _client(repo_root: Path = REPO_ROOT) -> TestClient:
    return TestClient(create_app(repo_root=repo_root))


def test_allowed_origin_gets_allow_origin_header() -> None:
    r = _client().get("/endpoints", headers={"Origin": ALLOWED})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == ALLOWED


def test_preflight_options_returns_cors_headers() -> None:
    r = _client().options(
        "/endpoints",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code in (200, 204)
    assert r.headers.get("access-control-allow-origin") == ALLOWED
    allow_methods = r.headers.get("access-control-allow-methods", "")
    assert "GET" in allow_methods and "POST" in allow_methods


def test_non_allowed_origin_is_not_reflected() -> None:
    # Allow-list, not wildcard: a foreign origin is not echoed into Allow-Origin.
    r = _client().get("/endpoints", headers={"Origin": FOREIGN})
    assert r.status_code == 200  # the request itself still succeeds server-side...
    assert r.headers.get("access-control-allow-origin") != FOREIGN
    assert r.headers.get("access-control-allow-origin") != "*"  # never a wildcard


def test_env_override_sets_exact_allow_list(monkeypatch) -> None:
    custom = "https://demo.endoscan.example"
    monkeypatch.setenv("ENDOSCAN_CORS_ORIGINS", f"  {custom} , , ")  # whitespace/empties dropped
    assert _cors_origins() == [custom]  # exactly the parsed env origins, no default appended

    client = _client()
    ok = client.get("/endpoints", headers={"Origin": custom})
    assert ok.headers.get("access-control-allow-origin") == custom
    # A previously-default origin is NOT allowed once the env allow-list is set.
    not_ok = client.get("/endpoints", headers={"Origin": ALLOWED})
    assert not_ok.headers.get("access-control-allow-origin") != ALLOWED


def test_default_origins_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("ENDOSCAN_CORS_ORIGINS", raising=False)
    assert _cors_origins() == DEFAULT_CORS_ORIGINS
    # No wildcard anywhere in the default.
    assert "*" not in _cors_origins()
