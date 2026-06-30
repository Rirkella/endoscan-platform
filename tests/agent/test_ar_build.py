"""AR functional_modulation build wiring: AR rides the existing gated agent unchanged.

Confirms (offline, fixtures only — the real build is the server follow-up):
- AR builds through start_build on the committed AR fixture config -> a gate verdict +
  workspace + dataset card (failed_qc on the strict gate / tiny fixtures, registry intact);
- the curated FUSED parquet contract (compound_id / InChIKey) is consumed by the agent's
  signature_retriever via the broad `lincs` source;
- the chokepoint still holds for AR (no token -> ApprovalRequiredError; failed gate +
  forged token -> GateFailedError);
- AR floors == ER floors (no lowered threshold; same quality_gates.yaml);
- the run.py card-text generalization leaves ER's card byte-identical while AR gets its own
  wording (no "estrogen receptor").
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest

from agent import (
    ApprovalRequiredError,
    ApprovalToken,
    BuildRecipe,
    BuildState,
    GateFailedError,
)
from agent import builder_agent as ba
from endoscan_core.datasets import StagedSourceAdapter, load_sources, signature_retriever
from endoscan_core.registry import EndpointStatus
from endoscan_core.training.evaluate_endpoint import ConfusionMatrix, EvalMetrics

REPO_ROOT = Path(__file__).resolve().parents[2]
AR_RUNNER = "pipelines/endpoints/ER/run.py"  # the reused, target-agnostic runner
AR_CONFIG = "pipelines/endpoints/AR/config.yaml"
ER_CONFIG = "pipelines/endpoints/ER/config.yaml"


@pytest.fixture
def builds_root(tmp_path: Path) -> Path:
    return tmp_path / "builds"


@pytest.fixture
def output_root(tmp_path: Path) -> Path:
    index = tmp_path / "registry" / "models" / "endpoints.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text('{\n  "endpoints": []\n}\n', encoding="utf-8")
    (tmp_path / "models").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def ar_recipe() -> BuildRecipe:
    return BuildRecipe(
        name="ar_functional_modulation",
        pipeline_runner_path=AR_RUNNER,
        pipeline_config_path=AR_CONFIG,
    )


def _forge_token(build_id: str, fingerprint: str) -> ApprovalToken:
    return ApprovalToken(
        build_id=build_id,
        gate_fingerprint=fingerprint,
        approver="attacker",
        value="forged",
        issued_at=datetime.now(UTC),
    )


def _load_runner() -> ModuleType:
    return ba._load_runner(REPO_ROOT / AR_RUNNER)


# --- AR builds through the gated agent -------------------------------------------------


def test_ar_builds_through_start_build(ar_recipe, builds_root, output_root):
    # AR is just a new target: the agent sequences the SAME tools and produces a gate
    # verdict + dataset card. On the strict gate the tiny fixture cannot pass -> failed_qc,
    # a valid, honest outcome with the registry left untouched (no training/registration).
    record = ba.start_build(ar_recipe, repo_root=REPO_ROOT, builds_root=builds_root)
    assert record.target == "AR"
    assert record.state in {BuildState.failed_qc, BuildState.awaiting_approval}
    report = ba.get_gate_report(record.build_id, builds_root=builds_root)
    assert report.dataset_card_path and Path(report.dataset_card_path).is_file()
    # Strict gate on the fixture is deterministically a FAIL -> failed_qc, registry intact.
    assert record.state is BuildState.failed_qc and report.passed is False


def test_ar_curated_fused_parquet_is_consumed(tmp_path: Path):
    # The curated AR signatures parquet (fused, InChIKey compound_id) is staged as
    # lincs.parquet (signatures source id == `lincs`) and consumed unchanged.
    staged = tmp_path / "staged"
    staged.mkdir()
    fused = pd.DataFrame(
        {
            "compound_id": ["AAAAAAAAAAAAAA-AAAAAAAAAA-A", "BBBBBBBBBBBBBB-BBBBBBBBBB-B"],
            "GENE_A": [0.1, 0.2],
            "GENE_B": [-0.3, 0.4],
        }
    )
    fused.to_parquet(staged / "lincs.parquet", index=False)

    allow_list = load_sources(repo_root=REPO_ROOT)
    lincs = next(s for s in allow_list.sources if s.id == "lincs")
    sig_set = signature_retriever([lincs], StagedSourceAdapter(staged), allow_list=allow_list)
    assert sig_set.granularity == "compound"  # fused contract, not per-signature
    assert sig_set.feature_names == ["GENE_A", "GENE_B"]
    assert all(r.compound_id_type == "inchikey" for r in sig_set.records)


def test_ar_promote_requires_approval_and_gate_pass(ar_recipe, builds_root, output_root):
    record = ba.start_build(ar_recipe, repo_root=REPO_ROOT, builds_root=builds_root)
    # No token -> refused before anything runs.
    with pytest.raises(ApprovalRequiredError):
        ba.promote(
            record.build_id, None,
            repo_root=REPO_ROOT, output_root=output_root, builds_root=builds_root,
        )  # fmt: skip
    # A well-formed, fingerprint-matching but UNAUTHORIZED token cannot pass the failed gate.
    report = ba.get_gate_report(record.build_id, builds_root=builds_root)
    with pytest.raises(GateFailedError):
        ba.promote(
            record.build_id, _forge_token(record.build_id, report.fingerprint()),
            repo_root=REPO_ROOT, output_root=output_root, builds_root=builds_root,
        )  # fmt: skip
    assert not (output_root / "models" / "AR").exists()  # registry/artifacts untouched


# --- same thresholds, no AR-special floors ---------------------------------------------


def test_ar_floors_and_gate_identical_to_er():
    runner = _load_runner()
    ar = runner.load_config(REPO_ROOT / AR_CONFIG)
    er = runner.load_config(REPO_ROOT / ER_CONFIG)
    assert ar.training.validated_mvp_floors == er.training.validated_mvp_floors
    assert ar.training.validated_mvp_ceilings == er.training.validated_mvp_ceilings
    # The SAME strict gate file — not an AR-special (lowered) thresholds file.
    assert ar.gate.thresholds_path == er.gate.thresholds_path == "registry/data/quality_gates.yaml"


# --- run.py card-text generalization: ER byte-identical, AR its own wording -------------


def _metrics() -> EvalMetrics:
    return EvalMetrics(
        auroc=0.74, auprc=0.30, balanced_accuracy=0.60, f1=0.25, brier_score=0.10,
        confusion_matrix=ConfusionMatrix(tn=80, fp=10, fn=30, tp=10), n_samples=130,
    )  # fmt: skip


def _report():
    return SimpleNamespace(per_class_compound_counts={1: 40, 0: 100}, n_overlap=140)


def _render(config) -> str:
    runner = _load_runner()
    return runner._render_model_card(
        config,
        EndpointStatus.experimental,
        "random_forest",
        _metrics(),
        _report(),
        978,
        {"mode": "nested", "outer_splits": 5, "per_fold_selected": ["random_forest"] * 5},
        ["compara", "lincs", "pubchem"],
    )


def test_er_card_use_lines_byte_identical_after_generalization():
    # Drift-pin: the ER config leaves the use fields unset -> the card renders the EXACT
    # original ER strings (the run.py defaults), so ER's card is unchanged by this PR.
    runner = _load_runner()
    er = runner.load_config(REPO_ROOT / ER_CONFIG)
    assert er.recommended_use is None and er.not_recommended_use is None  # ER unset -> defaults
    card = _render(er)
    assert "Research prioritization of estrogen-receptor activity from signatures." in card
    assert "Any regulatory, clinical, or diagnostic decision." in card


def test_ar_card_uses_androgen_wording_not_estrogen():
    runner = _load_runner()
    ar = runner.load_config(REPO_ROOT / AR_CONFIG)
    card = _render(ar)
    assert "androgen-receptor functional modulation" in card
    assert "estrogen" not in card.lower()  # AR must NOT inherit ER's wording


def test_card_use_defaults_match_original_er_strings():
    # The defaults ARE the original hardcoded ER strings (the source of the byte-identity).
    runner = _load_runner()
    assert (
        runner._DEFAULT_RECOMMENDED_USE
        == "Research prioritization of estrogen-receptor activity from signatures."
    )
    expected_not = "Any regulatory, clinical, or diagnostic decision."
    assert runner._DEFAULT_NOT_RECOMMENDED_USE == expected_not
