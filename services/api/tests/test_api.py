"""TestClient integration tests for the thin serving API (no network, no dvc).

Runs against the REAL committed repo (see conftest.REPO_ROOT): real registry entries +
committed model.pkl binaries. These assert the honest contract — status/CI reasons/scope
surfaced, the variant-forward-compatible detail LIST shape, real-model predictions, and
clean error mapping.
"""

from __future__ import annotations

import pytest

from endoscan_api.schemas import EndpointDetail

# AR's four CI-demotion reasons and ER's three — the honest evidence-based rule output.
AR_CI_REASONS = {
    "auroc 95% CI lower bound 0.678 < 0.75 floor",
    "auprc 95% CI lower bound 0.420 < 0.50 floor",
    "balanced accuracy 95% CI lower bound 0.642 < 0.65 floor",
    "brier score 95% CI upper bound 0.243 > 0.20 ceiling",
}
ER_CI_REASONS = {
    "auroc 95% CI lower bound 0.675 < 0.75 floor",
    "auprc 95% CI lower bound 0.194 < 0.50 floor",
    "balanced accuracy 95% CI lower bound 0.537 < 0.65 floor",
}


# 1 -----------------------------------------------------------------------------------
def test_list_endpoints_returns_er_and_ar_experimental(client) -> None:
    r = client.get("/endpoints")
    assert r.status_code == 200
    by_id = {e["endpoint_id"]: e for e in r.json()}
    assert {"ER", "AR"} <= set(by_id)
    for ep in ("ER", "AR"):
        assert by_id[ep]["status"] == "experimental"  # honest status in the LIST view
        assert by_id[ep]["input_type"] == "transcriptomics"
    assert by_id["ER"]["frozen"] is True and by_id["AR"]["frozen"] is False


# 2 -----------------------------------------------------------------------------------
def test_detail_ar_variants_list_shape_and_honesty(client) -> None:
    r = client.get("/endpoints/AR")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "experimental"
    assert isinstance(body["variants"], list) and len(body["variants"]) == 1  # LIST, len 1 today
    v = body["variants"][0]
    assert v["context"]["cell_lines"] is None  # no committed structured source (Option a)
    assert "VCaP" in v["context"]["scope"] and "binding EXCLUDED" in v["context"]["scope"]
    lim = v["limitations"]
    assert lim["is_experimental"] is True
    assert AR_CI_REASONS <= set(lim["missed_criteria"])  # the four CI reasons, surfaced
    assert lim["disclaimer"]  # honesty: disclaimer present
    assert v["metrics_summary"]["uncertainty"] and v["metrics_summary"]["evidence"]


# 3 -----------------------------------------------------------------------------------
def test_detail_er_variants_list_shape_and_reasons(client) -> None:
    body = client.get("/endpoints/ER").json()
    assert len(body["variants"]) == 1
    v = body["variants"][0]
    assert v["context"]["cell_lines"] is None
    assert v["context"]["scope"]  # ER claim_scope served verbatim
    assert ER_CI_REASONS <= set(v["limitations"]["missed_criteria"])
    assert body["frozen"] is True


# 4 -----------------------------------------------------------------------------------
def test_detail_shape_is_forward_compatible_with_multiple_variants(client) -> None:
    # A SECOND context-variant is additive: the same schema validates with len==2, so
    # adding e.g. AR@organoid later is not a breaking contract change.
    body = client.get("/endpoints/AR").json()
    second = dict(body["variants"][0])
    second["variant_id"] = "AR@ORGANOID"
    body["variants"] = [body["variants"][0], second]
    detail = EndpointDetail.model_validate(body)  # no error -> shape supports >1 variant
    assert len(detail.variants) == 2
    assert [v.variant_id for v in detail.variants] == ["AR", "AR@ORGANOID"]


# 5 -----------------------------------------------------------------------------------
def test_unknown_endpoint_returns_404(client) -> None:
    r = client.get("/endpoints/NOPE")
    assert r.status_code == 404
    assert r.json()["error"] == "endpoint_not_found"


# 6 -----------------------------------------------------------------------------------
def test_predict_returns_real_prediction_from_committed_model(
    client, full_signature, repo_root
) -> None:
    r = client.post("/predict", json={"endpoint_id": "AR", "signature": full_signature("AR")})
    assert r.status_code == 200
    body = r.json()
    assert 0.0 <= body["score"] <= 1.0
    assert isinstance(body["call"], bool)
    assert body["threshold"] is not None
    assert body["limitations"]["is_experimental"] is True  # honesty rides on every prediction
    assert AR_CI_REASONS <= set(body["limitations"]["missed_criteria"])
    # It served the REAL committed binary (not a fixture): ~1.5MB ER / ~40KB AR exist in git.
    assert (repo_root / "models" / "AR" / "model.pkl").stat().st_size > 10_000


# 7 -----------------------------------------------------------------------------------
def test_predict_gene_mismatch_returns_422(client, full_signature) -> None:
    sig = full_signature("AR")
    sig.pop(next(iter(sig)))  # drop one required gene
    sig["NOT_A_GENE"] = 1.0  # and add an unknown one
    r = client.post("/predict", json={"endpoint_id": "AR", "signature": sig})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_signature"
    assert "schema genes" in r.json()["detail"] or "not in the schema" in r.json()["detail"]


# 8 -----------------------------------------------------------------------------------
def test_malformed_body_returns_422(client) -> None:
    # Missing 'signature' entirely -> pydantic request validation (FastAPI default 422).
    assert client.post("/predict", json={"endpoint_id": "AR"}).status_code == 422
    # Non-numeric signature value -> 422 too.
    bad = client.post("/predict", json={"endpoint_id": "AR", "signature": {"A1BG": "x"}})
    assert bad.status_code == 422


# 9 -----------------------------------------------------------------------------------
def test_explain_tree_endpoint_uses_tree_shap(client, full_signature) -> None:
    # ER is random_forest -> TreeSHAP. (/explain is a clean 503 if shap is absent.)
    pytest.importorskip("shap")
    r = client.post(
        "/explain", json={"endpoint_id": "ER", "signature": full_signature("ER"), "top_n": 5}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["method"] == "tree_shap"  # unchanged behavior for the tree endpoint
    assert body["n_features"] == 978
    assert 1 <= len(body["top_contributors"]) <= 5
    assert all(c["direction"] in {"toward", "away"} for c in body["top_contributors"])
    assert body["limitations"]["is_experimental"] is True


# 9b ----------------------------------------------------------------------------------
def test_explain_linear_endpoint_uses_coefficient_attribution(client, full_signature) -> None:
    # AR is ridge_logreg -> coefficient attribution (no shap needed). Returns 200 now.
    r = client.post(
        "/explain", json={"endpoint_id": "AR", "signature": full_signature("AR"), "top_n": 5}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["method"] == "linear_coefficient"  # labeled honestly, NOT tree_shap
    assert body["n_features"] == 978
    assert 1 <= len(body["top_contributors"]) <= 5
    assert all(c["direction"] in {"toward", "away"} for c in body["top_contributors"])
    # The explanation still carries AR's honest limitations (experimental + the CI reasons).
    assert body["limitations"]["is_experimental"] is True
    assert AR_CI_REASONS <= set(body["limitations"]["missed_criteria"])


# 9c ----------------------------------------------------------------------------------
def test_explain_genuinely_unsupported_model_is_clean_501(
    client, full_signature, monkeypatch
) -> None:
    # A model type that is neither tree nor linear still returns a clean 501 (not a 500).
    from endoscan_api.routes import inference as inf
    from endoscan_core.inference import UnsupportedModelForExplanationError

    def _unsupported(*args, **kwargs):
        raise UnsupportedModelForExplanationError("no implemented attributor for model type 'Foo'")

    monkeypatch.setattr(inf, "explain", _unsupported)
    r = client.post("/explain", json={"endpoint_id": "AR", "signature": full_signature("AR")})
    assert r.status_code == 501
    assert r.json()["error"] == "explain_unsupported_for_model"


# 10 ----------------------------------------------------------------------------------
def test_health_lists_warmed_endpoints(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert {"ER", "AR"} <= set(body["endpoints_loaded"])
    assert isinstance(body["explain_available"], bool)
