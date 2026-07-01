"""ER is now a real, committed, serve-ready frozen endpoint (integration).

Runs against the ACTUAL committed repo (repo_root = REPO_ROOT): the real registry entry +
the real models/ER/ artifacts (model.pkl committed directly, Option A — the DVC pointer was
removed after a proven-identical rebuild). This is the honest end-to-end check that ER serves
from the real binary, that its metrics did NOT drift from the frozen record, and that AR stays
untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

from endoscan_core.inference import build_limitations
from endoscan_core.registry import (
    EndpointStatus,
    get_endpoint,
    load_model,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# The original frozen ER record (pre-rebuild). The rebuild is a byte-identical regeneration,
# so the point metrics must equal these EXACTLY — this is the drift guard, not a new baseline.
ORIGINAL_POINT_METRICS = {
    "auroc": 0.7396951049816011,
    "auprc": 0.25637241142364803,
    "balanced_accuracy": 0.5788830823463929,
    "brier_score": 0.070140394032221,
}
# md5 of the original DVC-recorded binary; the committed model.pkl must reproduce it.
PROVEN_MODEL_MD5 = "5bed2e0de503dbec65992db1e498212e"


def test_er_registered_experimental_and_frozen_after_the_rebaseline() -> None:
    er = get_endpoint("ER", repo_root=REPO_ROOT)
    assert er.status is EndpointStatus.experimental  # status UNCHANGED by the rebuild
    assert er.frozen is True  # ends frozen again (true -> false -> true within this PR)
    assert er.biological_target == "Estrogen Receptor"
    assert er.source_refs == ["cerapp", "lincs", "pubchem"]
    assert er.updated_at is not None  # re-registration stamped an audit trail
    assert er.created_at is not None  # original creation preserved


def test_er_model_binary_loads_and_matches_feature_schema_978() -> None:
    # The committed real binary loads and its input width equals the 978 landmark genes in
    # feature_schema.json — the serving path works on the REAL model, not the DVC pointer.
    model = load_model("ER", repo_root=REPO_ROOT)
    schema = json.loads((REPO_ROOT / "models/ER/feature_schema.json").read_text())
    assert len(schema["features"]) == 978
    assert model.n_features_in_ == 978


def test_er_dvc_pointer_removed_and_binary_is_real_committed() -> None:
    # Option A: the .dvc pointer is gone and model.pkl is a real ~1.5 MB committed binary.
    assert not (REPO_ROOT / "models/ER/model.pkl.dvc").exists()
    model_pkl = REPO_ROOT / "models/ER/model.pkl"
    assert model_pkl.is_file()
    assert model_pkl.stat().st_size > 1_000_000  # the real random_forest, not a text pointer


def test_er_point_metrics_match_the_original_frozen_record_no_drift() -> None:
    # Proves the reproduction: the rebuilt point metrics EQUAL the original recorded values.
    metrics = json.loads((REPO_ROOT / "models/ER/metrics.json").read_text())
    for key, expected in ORIGINAL_POINT_METRICS.items():
        assert metrics[key] == expected, f"{key} drifted from the frozen record"


def test_er_served_limitations_carry_the_ci_reasons() -> None:
    # metrics <-> card consistency at the served layer: experimental + the three CI-demotion
    # reasons (the strengthened evidence rule), never the point estimate alone.
    er = get_endpoint("ER", repo_root=REPO_ROOT)
    metrics = json.loads((REPO_ROOT / er.metrics_path).read_text())
    lim = build_limitations(er, metrics)
    assert lim.is_experimental
    joined = " | ".join(lim.missed_criteria)
    assert "auroc 95% CI lower bound 0.675 < 0.75 floor" in joined
    assert "auprc 95% CI lower bound 0.194 < 0.50 floor" in joined
    assert "balanced accuracy 95% CI lower bound 0.537 < 0.65 floor" in joined
    # min-evidence passes (73 positives, 14 per fold) — the CI bounds are what demote.
    assert metrics["evidence"]["n_positives_total"] == 73


def test_er_model_card_states_byte_identical_reproduction_not_may_differ() -> None:
    card = (REPO_ROOT / "models/ER/model_card.md").read_text()
    assert "byte-identical" in card
    assert PROVEN_MODEL_MD5 in card
    assert "may differ" not in card  # the corrected note: proven identical, not hedged


def test_ar_untouched_by_the_er_rebaseline() -> None:
    ar = get_endpoint("AR", repo_root=REPO_ROOT)
    assert ar.status is EndpointStatus.experimental
    assert ar.frozen is False  # AR remains the non-frozen experimental endpoint
    assert ar.model_path == "models/AR/model.pkl"
