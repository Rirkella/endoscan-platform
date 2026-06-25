"""M4 inference + explainability: schema validation, predict, TreeSHAP, limitations.

Runs against the self-contained FIXTURE_ER fixture endpoint (tiny git-tracked RF) plus
a schema-only check against the committed real ER feature_schema.json (978 genes, no
model binary needed in CI).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from endoscan_core.inference import (
    ExplanationResult,
    ModelArtifactUnavailableError,
    PredictionResult,
    align_signature,
    build_limitations,
    explain,
    load_endpoint_model,
    load_feature_schema,
    predict,
    predict_batch,
    validate_signature,
)
from endoscan_core.inference.schema_validation import SignatureValidationError
from endoscan_core.registry.schema import EndpointEntry

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "inference"
GENES = ["G1", "G2", "G3", "G4", "G5"]
SIG = {"G1": 1.5, "G2": -0.5, "G3": 0.2, "G4": 0.0, "G5": -0.3}


def _schema():
    return load_feature_schema(FIXTURE_ROOT / "models" / "FIXTURE_ER" / "feature_schema.json")


# --- schema validation ----------------------------------------------------------------


def test_valid_signature_aligns_to_schema_order() -> None:
    schema = _schema()
    vec = validate_signature(SIG, schema)
    assert list(vec) == [SIG[g] for g in GENES]  # aligned to schema order


def test_alignment_is_order_independent_for_mappings() -> None:
    schema = _schema()
    shuffled = {g: SIG[g] for g in reversed(GENES)}  # insertion order reversed
    assert list(validate_signature(shuffled, schema)) == [SIG[g] for g in GENES]


def test_missing_gene_is_rejected() -> None:
    schema = _schema()
    bad = {g: SIG[g] for g in GENES if g != "G3"}
    with pytest.raises(SignatureValidationError, match="missing"):
        validate_signature(bad, schema)


def test_extra_gene_is_rejected_by_default_but_droppable() -> None:
    schema = _schema()
    extra = {**SIG, "GZZZ": 9.9}
    with pytest.raises(SignatureValidationError, match="not in the schema"):
        validate_signature(extra, schema)
    # allow_extra drops the extra and still aligns.
    assert list(validate_signature(extra, schema, allow_extra=True)) == [SIG[g] for g in GENES]


def test_unsupported_type_and_nan_rejected() -> None:
    schema = _schema()
    with pytest.raises(SignatureValidationError):
        validate_signature([1.5, -0.5, 0.2, 0.0, -0.3], schema)  # bare list: order unverifiable
    with pytest.raises(SignatureValidationError, match="NaN"):
        validate_signature({**SIG, "G3": float("nan")}, schema)


def test_batch_dataframe_aligns_shuffled_columns() -> None:
    schema = _schema()
    df = pd.DataFrame([SIG, {g: 0.0 for g in GENES}])[list(reversed(GENES))]  # shuffled cols
    X, is_single = align_signature(df, schema)
    assert not is_single and X.shape == (2, 5)
    assert list(X[0]) == [SIG[g] for g in GENES]


# --- prediction -----------------------------------------------------------------------


def test_prediction_is_deterministic_and_carries_limitations() -> None:
    r1 = predict("FIXTURE_ER", SIG, repo_root=FIXTURE_ROOT)
    r2 = predict("FIXTURE_ER", SIG, repo_root=FIXTURE_ROOT)
    assert isinstance(r1, PredictionResult)
    assert r1.probability == r2.probability  # deterministic
    assert 0.0 <= r1.probability <= 1.0
    assert r1.call == (r1.probability >= r1.threshold)
    assert r1.threshold == 0.5  # metrics.json threshold
    assert r1.limitations.status == "experimental"  # never a bare score


def test_predict_batch_returns_one_result_per_row() -> None:
    df = pd.DataFrame([SIG, {g: 0.0 for g in GENES}])
    out = predict_batch("FIXTURE_ER", df, repo_root=FIXTURE_ROOT)
    assert len(out) == 2 and all(isinstance(r, PredictionResult) for r in out)
    assert all(r.limitations.status == "experimental" for r in out)


# --- explanation (TreeSHAP) -----------------------------------------------------------


def test_treeshap_attributions_map_to_gene_symbols() -> None:
    pytest.importorskip("shap")
    res = explain("FIXTURE_ER", SIG, top_n=3, repo_root=FIXTURE_ROOT)
    assert isinstance(res, ExplanationResult)
    assert len(res.top_contributors) == 3
    assert all(c.gene in GENES for c in res.top_contributors)
    assert all(c.direction in {"toward", "away"} for c in res.top_contributors)
    # signed + ranked by |value|
    mags = [abs(c.shap_value) for c in res.top_contributors]
    assert mags == sorted(mags, reverse=True)
    assert res.limitations.status == "experimental"


def test_treeshap_additivity_and_full_feature_coverage() -> None:
    pytest.importorskip("shap")
    from endoscan_core.inference.explain import TreeSHAPAttribution
    from endoscan_core.inference.predict import _resolve

    root, _entry, schema, _metrics = _resolve("FIXTURE_ER", FIXTURE_ROOT)
    X, _ = align_signature(SIG, schema)
    model = load_endpoint_model("FIXTURE_ER", repo_root=root)
    values, base = TreeSHAPAttribution().attribute(model, X)
    assert values.shape == (1, len(GENES))  # one attribution per gene
    proba = float(model.predict_proba(X)[:, 1][0])
    assert base + float(values[0].sum()) == pytest.approx(proba, abs=1e-6)  # exact additivity


def test_explain_module_imports_shap_lazily() -> None:
    # CORE must import cleanly with shap absent: explain.py has NO top-level `import shap`.
    src = (REPO_ROOT / "packages/endoscan_core/endoscan_core/inference/explain.py").read_text()
    tree = ast.parse(src)
    top_imports = {
        n.name.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.Import)
        for n in node.names
    }
    top_imports |= {
        node.module.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "shap" not in top_imports


# --- limitations on EVERY output ------------------------------------------------------


def test_limitations_required_on_every_result_type() -> None:
    # The field is required (no default): constructing without it raises.
    with pytest.raises(ValidationError, match="limitations"):
        PredictionResult(
            endpoint_id="X", probability=0.5, call=True, threshold=0.5, standardized_input=True
        )
    with pytest.raises(ValidationError, match="limitations"):
        ExplanationResult(endpoint_id="X", base_value=0.0, n_features=1, top_contributors=[])


def test_limitations_carry_status_missed_floor_and_scope() -> None:
    res = predict("FIXTURE_ER", SIG, repo_root=FIXTURE_ROOT)
    lim = res.limitations
    assert lim.is_experimental and lim.status == "experimental"
    assert lim.floors_recorded is True
    assert any("balanced accuracy" in m and "floor" in m for m in lim.missed_criteria)
    assert "ER functional modulation" in lim.claim_scope
    assert lim.prevalence is not None and lim.disclaimer


def test_limitations_degrade_when_fields_absent() -> None:
    # A legacy metrics.json without structured floors/claim_scope must not fabricate.
    entry = EndpointEntry.model_validate(
        json.loads((FIXTURE_ROOT / "registry/models/endpoints.json").read_text())["endpoints"][0]
    )
    legacy = {"confusion_matrix": {"tn": 30, "fp": 6, "fn": 14, "tp": 10}, "n_samples": 60}
    lim = build_limitations(entry, legacy)
    assert lim.status == "experimental"  # from the entry
    assert lim.floors_recorded is False and lim.missed_criteria == []  # not fabricated
    assert lim.prevalence == pytest.approx(24 / 60)  # still derived from metrics
    assert "pan-endocrine" in lim.claim_scope  # generic fallback scope


# --- DVC-absent model -----------------------------------------------------------------


def test_dvc_absent_model_raises_actionable_error(tmp_path: Path) -> None:
    (tmp_path / "models" / "GHOST").mkdir(parents=True)
    (tmp_path / "models" / "GHOST" / "model.pkl.dvc").write_text("outs:\n- path: model.pkl\n")
    entry = {
        "endpoint_id": "GHOST",
        "biological_target": "x",
        "input_type": "transcriptomics",
        "model_path": "models/GHOST/model.pkl",
        "feature_schema_path": "models/GHOST/feature_schema.json",
        "metrics_path": "models/GHOST/metrics.json",
        "explainer_path": None,
        "model_card_path": "models/GHOST/model_card.md",
        "dataset_card_path": "models/GHOST/dataset_card.md",
        "status": "experimental",
        "version": "0.1.0",
        "created_at": "2026-06-25T00:00:00Z",
        "source_refs": [],
    }
    idx = tmp_path / "registry" / "models" / "endpoints.json"
    idx.parent.mkdir(parents=True)
    idx.write_text(json.dumps({"endpoints": [entry]}), encoding="utf-8")
    with pytest.raises(ModelArtifactUnavailableError, match="dvc pull"):
        load_endpoint_model("GHOST", repo_root=tmp_path)


# --- real ER: schema-only (committed feature_schema.json, no model binary) -------------


def test_real_er_feature_schema_validates_a_correct_signature() -> None:
    schema = load_feature_schema(REPO_ROOT / "models" / "ER" / "feature_schema.json")
    assert len(schema.features) == 978
    good = {g: 0.1 for g in schema.features}
    vec = validate_signature(good, schema)
    assert vec.shape == (978,)
    with pytest.raises(SignatureValidationError, match="missing"):
        validate_signature({g: 0.1 for g in schema.features[:-1]}, schema)
