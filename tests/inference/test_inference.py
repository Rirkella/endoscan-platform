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
    missed_criteria,
    predict,
    predict_batch,
    validate_signature,
)
from endoscan_core.inference.schema_validation import SignatureValidationError
from endoscan_core.registry.schema import EndpointEntry
from endoscan_core.training.evaluate_endpoint import ConfusionMatrix, EvalMetrics

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "inference"
GENES = ["G1", "G2", "G3", "G4", "G5"]
SIG = {"G1": 1.5, "G2": -0.5, "G3": 0.2, "G4": 0.0, "G5": -0.3}

# A legacy metrics.json (e.g. the frozen ER record) carries no bootstrap-CI / fold
# evidence, so the strengthened rule reports these honest reasons IN ADDITION to any
# point floor/ceiling miss — validation predates CI evidence, so validated_mvp is
# unattainable (the record stays experimental; nothing about it is fabricated).
NO_EVIDENCE_REASONS = [
    "no sample-evidence summary (per-fold support not recorded); validated_mvp requires "
    "recorded fold support",
    "no CI evidence / no fold support — validation predates confidence-interval evidence; "
    "validated_mvp requires a bootstrap CI",
]


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


# --- status / limitations agreement: same keys + mirrored operators -------------------


def _er_eval_metrics() -> EvalMetrics:
    # The frozen real-ER committed values (models/ER/metrics.json).
    return EvalMetrics(
        auroc=0.7396951049816011,
        auprc=0.25637241142364803,
        balanced_accuracy=0.5788830823463929,
        f1=0.25,
        brier_score=0.070140394032221,
        confusion_matrix=ConfusionMatrix(tn=868, fp=18, fn=60, tp=13),
        n_samples=959,
        threshold=0.5,
    )


def test_status_and_limitations_compare_identical_keys_and_operators(er_run) -> None:
    """missed_floors (run.py) == missed_criteria (limitations.py) == the _status_for verdict.

    A future metric-key rename can't make one detect a floor the other misses: every
    floor/ceiling key must be BOTH an EvalMetrics attribute (run.py reads it via getattr)
    AND a key emitted into metrics.json (limitations.py reads it via dict lookup).
    """
    floors = {"auroc": 0.65, "auprc": 0.15, "balanced_accuracy": 0.60}
    ceilings = {"brier_score": 0.20}
    m = _er_eval_metrics()
    md = er_run._metrics_dict(
        m,
        959,
        "random_forest",
        {"mode": "nested", "outer_splits": 5, "inner_splits": 3},
        validated_mvp_floors=floors,
        validated_mvp_ceilings=ceilings,
        claim_scope="ER functional modulation",
    )

    # (a) every floor/ceiling key resolves in BOTH namespaces — no silent divergence.
    for key in (*floors, *ceilings):
        assert hasattr(m, key), f"{key} is not an EvalMetrics attribute (run.py getattr)"
        assert key in md, f"{key} is not emitted into metrics.json (limitations.py lookup)"

    # (b) the two implementations produce the identical missed list.
    assert er_run.missed_floors(m, floors, ceilings) == missed_criteria(md, floors, ceilings)

    # (c) emptiness of that list agrees with the _status_for validated_mvp/experimental verdict.
    status = er_run._status_for(m, floors, ceilings)
    assert (missed_criteria(md, floors, ceilings) == []) == (status.value == "validated_mvp")

    # Pinned to this frozen ER case (no CI/fold evidence in md): the honest evidence
    # reasons + the one point floor miss, status experimental. Both surfaces AGREE.
    assert missed_criteria(md, floors, ceilings) == [
        *NO_EVIDENCE_REASONS,
        "balanced accuracy 0.579 < 0.60 floor",
    ]
    assert status.value == "experimental"


def test_partial_floor_record_evaluates_without_raising() -> None:
    # A small/partial floor record (one key, present in metrics) evaluates normally; with no
    # CI/fold evidence the honest evidence reasons accompany any point miss (and remain even
    # when the point floor IS met — validated_mvp needs the evidence, not just the point).
    metrics = {"balanced_accuracy": 0.579, "brier_score": 0.07}
    assert missed_criteria(metrics, {"balanced_accuracy": 0.60}, {}) == [
        *NO_EVIDENCE_REASONS,
        "balanced accuracy 0.579 < 0.60 floor",
    ]
    assert missed_criteria(metrics, {"balanced_accuracy": 0.50}, {}) == NO_EVIDENCE_REASONS
    assert missed_criteria(metrics, {}, {"brier_score": 0.20}) == NO_EVIDENCE_REASONS  # no raise


def test_floor_or_ceiling_naming_absent_metric_raises() -> None:
    # An un-evaluable criterion (metric absent from metrics) RAISES — never silently skipped.
    metrics = {"balanced_accuracy": 0.579}
    with pytest.raises(ValueError, match="specificity"):
        missed_criteria(metrics, {"specificity": 0.5}, {})
    with pytest.raises(ValueError, match="ceiling"):
        missed_criteria(metrics, {}, {"log_loss": 0.30})


# --- real ER: PARTIAL floor record reads truthfully (structurally sourced) -------------


def test_real_er_partial_floor_record_reads_like_the_card() -> None:
    """The regenerated real-ER metrics.json carries a PARTIAL floor record: only the
    ground-truth-recovered balanced_accuracy floor (0.60) + brier ceiling (0.20), plus
    claim_scope and a floors_provenance note. The limitations block must now read
    identically to the frozen model card — STRUCTURALLY sourced, not degrading — and
    must NOT imply "all floors met" from the incomplete set.
    """
    entries = json.loads((REPO_ROOT / "registry/models/endpoints.json").read_text())
    er = next(e for e in entries["endpoints"] if e["endpoint_id"] == "ER")
    entry = EndpointEntry.model_validate(er)
    metrics = json.loads((REPO_ROOT / "models/ER/metrics.json").read_text())

    lim = build_limitations(entry, metrics)
    assert lim.status == "experimental" and lim.is_experimental
    assert lim.floors_recorded is True
    # The honest evidence reasons (this frozen record predates CI evidence) + the single
    # point floor miss — present key, evaluated (the #24 hardening does NOT raise). ER stays
    # experimental; the strengthened rule makes the missing-CI fact explicit, not fabricated.
    assert lim.missed_criteria == [*NO_EVIDENCE_REASONS, "balanced accuracy 0.579 < 0.60 floor"]
    assert "ER functional modulation" in lim.claim_scope  # verbatim scope, card-matching
    # Provenance makes the partial record self-evident (auroc/auprc floors unrecorded).
    assert lim.floors_provenance is not None
    assert "not recoverable" in lim.floors_provenance
    assert lim.prevalence == pytest.approx(73 / 959)  # (fn+tp)=73 over n_samples=959


def test_partial_floor_record_does_not_raise_on_present_key() -> None:
    # The real-ER partial record has ONE floor key (balanced_accuracy), present in
    # metrics -> hardened missed_criteria evaluates it without raising.
    metrics = json.loads((REPO_ROOT / "models" / "ER" / "metrics.json").read_text())
    floors = metrics["validated_mvp_floors"]
    ceilings = metrics["validated_mvp_ceilings"]
    assert list(floors) == ["balanced_accuracy"]  # partial, single key
    assert missed_criteria(metrics, floors, ceilings) == [
        *NO_EVIDENCE_REASONS,
        "balanced accuracy 0.579 < 0.60 floor",
    ]
