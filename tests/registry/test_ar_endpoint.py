"""AR is now a real, committed, serve-ready experimental endpoint (integration).

Runs against the ACTUAL committed repo (repo_root = REPO_ROOT): the real registry entry +
the real models/AR/ artifacts (model.pkl committed directly, Option A) — NOT a fixture. This
is the honest end-to-end check that AR serves and that ER stays untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

from endoscan_core.inference import build_limitations
from endoscan_core.registry import (
    EndpointStatus,
    get_endpoint,
    list_endpoints,
    load_model,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ar_is_registered_experimental_not_frozen() -> None:
    ids = {e.endpoint_id for e in list_endpoints(repo_root=REPO_ROOT)}
    assert {"ER", "AR"} <= ids  # AR joined ER in the registry
    ar = get_endpoint("AR", repo_root=REPO_ROOT)
    assert ar.status is EndpointStatus.experimental
    assert ar.frozen is False  # AR is NOT the protected baseline (only ER is)
    assert ar.biological_target == "Androgen Receptor"
    assert ar.source_refs == ["compara", "lincs", "pubchem"]


def test_ar_model_binary_loads_and_matches_feature_schema_978() -> None:
    # The committed real binary loads and its input width equals the 978 landmark genes in
    # feature_schema.json — the serving path works on the REAL model, not a fixture.
    model = load_model("AR", repo_root=REPO_ROOT)
    schema = json.loads((REPO_ROOT / "models/AR/feature_schema.json").read_text())
    assert len(schema["features"]) == 978
    assert model.n_features_in_ == 978


def test_ar_registry_paths_resolve_to_committed_artifacts() -> None:
    ar = get_endpoint("AR", repo_root=REPO_ROOT)
    for rel in ar.artifact_paths():
        assert (REPO_ROOT / rel).is_file(), f"missing committed artifact: {rel}"
    assert ar.metrics_path == "models/AR/metrics.json"


def test_ar_served_limitations_carry_the_honest_ci_reasons() -> None:
    # metrics <-> card consistency at the served layer: experimental + the four CI-demotion
    # reasons (the strengthened evidence rule), never the point estimate alone.
    ar = get_endpoint("AR", repo_root=REPO_ROOT)
    metrics = json.loads((REPO_ROOT / ar.metrics_path).read_text())
    lim = build_limitations(ar, metrics)
    assert lim.is_experimental
    joined = " | ".join(lim.missed_criteria)
    assert "auroc 95% CI lower bound 0.678 < 0.75 floor" in joined
    assert "auprc 95% CI lower bound 0.420 < 0.50 floor" in joined
    assert "balanced accuracy 95% CI lower bound 0.642 < 0.65 floor" in joined
    assert "brier score 95% CI upper bound 0.243 > 0.20 ceiling" in joined
    # Thin-positive context is recorded in the evidence block.
    assert metrics["evidence"]["n_positives_total"] == 37


def test_er_entry_untouched_still_frozen() -> None:
    er = get_endpoint("ER", repo_root=REPO_ROOT)
    assert er.status is EndpointStatus.experimental and er.frozen is True
    assert er.model_path == "models/ER/model.pkl"  # ER record unchanged by this PR
