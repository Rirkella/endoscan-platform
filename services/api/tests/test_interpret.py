"""POST /interpret/pathways route — honest states, method block, framing (fixture Reactome).

Enrichment is exercised against a SYNTHETIC fixture Reactome artifact (clearly marked
``is_fixture``), NOT real Reactome data — real Reactome is blocked in this environment. ``explain``
is monkeypatched so the toward-gene set is controlled and the outcome is deterministic; the real
enrichment math (``run_enrichment``) and the real ER feature schema (the 978-landmark universe) are
used unchanged.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from endoscan_api import create_app
from endoscan_api.routes import interpret

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_REACTOME = (
    REPO_ROOT / "services" / "api" / "tests" / "fixtures" / "reactome" / "reactome_pathways.json"
)

# The fixture pathways are built over these real ER landmark symbols (see the fixture file).
_LANDMARK6 = ["DDR1", "PAX8", "RPS5", "ABCF1", "SPAG7", "RHOA"]  # toward set that hits FIX:PA
_PA_GENES = ["ABCF1", "DDR1", "PAX8", "RPS5"]  # sorted overlap of toward∩PA∩universe


@pytest.fixture(scope="module")
def repo_with_reactome() -> Path:
    """A tmp repo carrying the registry + real feature schemas + the fixture Reactome artifact."""
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="pathways-repo-"))
    shutil.copytree(REPO_ROOT / "registry", root / "registry")
    for ep in ("ER", "AR"):
        (root / "models" / ep).mkdir(parents=True)
        shutil.copy(
            REPO_ROOT / "models" / ep / "feature_schema.json",
            root / "models" / ep / "feature_schema.json",
        )
    (root / "data" / "reactome").mkdir(parents=True)
    shutil.copy(FIXTURE_REACTOME, root / "data" / "reactome" / "reactome_pathways.json")
    return root


@pytest.fixture(scope="module")
def client(repo_with_reactome: Path) -> TestClient:
    return TestClient(create_app(repo_root=repo_with_reactome))


def _stub_explain(method: str, toward: list[str], away: list[str] | None = None):
    """A fake explain() returning a list[stub] with controlled toward/away contributors."""
    contribs = [SimpleNamespace(gene=g, shap_value=0.5, direction="toward") for g in toward]
    contribs += [SimpleNamespace(gene=g, shap_value=-0.5, direction="away") for g in (away or [])]
    stub = SimpleNamespace(endpoint_id="ER", method=method, top_contributors=contribs)
    return lambda *a, **k: [stub]


def test_ok_deterministic_enrichment(client, monkeypatch) -> None:
    monkeypatch.setattr(
        interpret, "explain", _stub_explain("tree_shap", _LANDMARK6, away=["APP", "CLTC"])
    )
    r = client.post("/interpret/pathways", json={"endpoint_id": "ER", "signature": {"DDR1": 0.1}})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["method"] == "tree_shap"
    assert len(body["pathways"]) == 1
    card = body["pathways"][0]
    assert card["pathway_id"] == "FIX:PA"
    assert card["evidence"] == "Medium"
    assert card["q_value"] == pytest.approx(4 * 120 / 38760, rel=1e-9)  # hand-computed
    assert card["genes_influencing_result"] == _PA_GENES  # the ACTUAL overlap, not full membership
    # Method block for Technical details.
    mb = body["method_block"]
    assert mb["pinned_top_n"] == 50 and mb["n_toward_genes"] == 6 and mb["n_input_genes"] == 6
    assert mb["universe_size"] == 20 and mb["min_pathway_overlap"] == 3 and mb["family_size"] == 4
    assert "Fisher" in mb["test"] and "Benjamini" in mb["correction"]
    assert mb["reactome"]["is_fixture"] is True  # provenance flows through


def test_no_causal_phrasing_anywhere_in_response(client, monkeypatch) -> None:
    monkeypatch.setattr(interpret, "explain", _stub_explain("tree_shap", _LANDMARK6))
    r = client.post("/interpret/pathways", json={"endpoint_id": "ER", "signature": {"DDR1": 0.1}})
    text = r.text.lower()
    for banned in ("activates", "causes", "perturbed", "affected gene"):
        assert banned not in text
    # The honest framing lives in the field name.
    assert "genes_influencing_result" in r.text


def test_too_few_toward_genes_is_honest_refusal(client, monkeypatch) -> None:
    monkeypatch.setattr(interpret, "explain", _stub_explain("tree_shap", ["DDR1", "PAX8", "RPS5"]))
    r = client.post("/interpret/pathways", json={"endpoint_id": "ER", "signature": {"DDR1": 0.1}})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "too_few_genes"
    assert "too few contributing genes" in body["reason"]
    assert body["pathways"] == []  # never tested on a handful of genes


def test_empty_result_is_a_real_result(client, monkeypatch) -> None:
    # 5 toward genes that are NOT in the fixture universe -> ran, but nothing clears q<0.10.
    outside = ["ZZZ1", "ZZZ2", "ZZZ3", "ZZZ4", "ZZZ5"]
    monkeypatch.setattr(interpret, "explain", _stub_explain("tree_shap", outside))
    r = client.post("/interpret/pathways", json={"endpoint_id": "ER", "signature": {"DDR1": 0.1}})
    body = r.json()
    assert body["status"] == "ok"
    assert body["pathways"] == []
    assert "no pathways met the evidence threshold" in body["reason"]


def test_explain_method_parity(client, monkeypatch) -> None:
    # The same toward set yields the same enrichment for tree_shap and linear_coefficient.
    results = {}
    for method in ("tree_shap", "linear_coefficient"):
        monkeypatch.setattr(interpret, "explain", _stub_explain(method, _LANDMARK6))
        r = client.post(
            "/interpret/pathways", json={"endpoint_id": "ER", "signature": {"DDR1": 0.1}}
        )
        body = r.json()
        assert body["method"] == method
        results[method] = [
            (p["pathway_id"], p["evidence"], round(p["q_value"], 9)) for p in body["pathways"]
        ]
    assert results["tree_shap"] == results["linear_coefficient"]


def test_missing_reactome_artifact_is_graceful(client_real: TestClient) -> None:
    # The real committed repo has NO data/reactome artifact -> honest "unavailable", zero pathways.
    r = client_real.post(
        "/interpret/pathways", json={"endpoint_id": "ER", "signature": {"DDR1": 0.1}}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unavailable"
    assert body["reason"] == "pathway data not available"
    assert body["pathways"] == []


@pytest.fixture(scope="module")
def client_real() -> TestClient:
    # Real committed repo root (no Reactome artifact committed) — for the unavailable path.
    return TestClient(create_app(repo_root=REPO_ROOT))
