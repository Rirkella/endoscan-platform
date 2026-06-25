"""Honest, parameterized model-card Limitations generation (experimental endpoints)."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd

from endoscan_core.registry import EndpointStatus, get_endpoint  # noqa: F401
from endoscan_core.training.evaluate_endpoint import ConfusionMatrix, EvalMetrics

REPO_ROOT = Path(__file__).resolve().parents[2]
TRAINING_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "training"
RELAXED_GATES = TRAINING_FIXTURES / "quality_gates_relaxed.yaml"
ER_FUSED = TRAINING_FIXTURES / "staged" / "er_fused"

FLOORS = {"auroc": 0.75, "auprc": 0.50, "balanced_accuracy": 0.60}
CEILINGS = {"brier_score": 0.20}
ER_SCOPE = "ER functional modulation (agonist or antagonist transactivation)"


def _metrics(*, auroc=0.740, auprc=0.256, bal=0.579, f1=0.250, brier=0.070) -> EvalMetrics:
    return EvalMetrics(
        auroc=auroc,
        auprc=auprc,
        balanced_accuracy=bal,
        f1=f1,
        brier_score=brier,
        confusion_matrix=ConfusionMatrix(tn=868, fp=18, fn=60, tp=13),
        n_samples=970,
        threshold=0.5,
    )


def _report(*, n_pos=73, n_neg=897, n_overlap=970):
    return SimpleNamespace(per_class_compound_counts={1: n_pos, 0: n_neg}, n_overlap=n_overlap)


def test_missed_floors_mirrors_status_for_comparison(er_run: ModuleType) -> None:
    missed = er_run.missed_floors(
        _metrics(brier=0.25), FLOORS, CEILINGS
    )  # auroc/auprc/bal below floors; brier above ceiling
    assert "auroc 0.740 < 0.75 floor" in missed
    assert "auprc 0.256 < 0.50 floor" in missed
    assert "balanced accuracy 0.579 < 0.60 floor" in missed
    assert "brier score 0.250 > 0.20 ceiling" in missed
    # A met floor is NOT listed.
    assert er_run.missed_floors(_metrics(auroc=0.99, auprc=0.99, bal=0.99), FLOORS, CEILINGS) == []


def test_experimental_limitations_are_parameterized_and_honest(er_run: ModuleType) -> None:
    text = er_run.build_limitations_section(
        status=EndpointStatus.experimental,
        metrics=_metrics(),
        report=_report(),
        evaluation={
            "mode": "nested",
            "outer_splits": 5,
            "per_fold_selected": [
                "random_forest",
                "lasso_logreg",
                "random_forest",
                "random_forest",
                "gradient_boosting",
            ],
        },
        floors=FLOORS,
        ceilings=CEILINGS,
        selected_model="random_forest",
        claim_scope=ER_SCOPE,
    )
    # STATUS: experimental + the missed floor named value-vs-threshold.
    assert "experimental" in text.lower()
    assert "balanced accuracy 0.579 < 0.60 floor" in text
    # STATISTICAL POWER: prevalence + positives-per-fold.
    assert "73 positives / 970 compounds" in text
    assert "prevalence 0.075" in text
    assert "~15 positives per held-out fold" in text  # 73 / 5
    # MODEL SELECTION STABILITY: unstable + the per-fold list.
    assert "UNSTABLE" in text
    assert "lasso_logreg" in text and "gradient_boosting" in text
    # SCOPE OF CLAIM + disclaimer.
    assert ER_SCOPE in text
    assert "confirmed experimentally" in text


def test_stable_validated_limitations_have_no_false_claims(er_run: ModuleType) -> None:
    text = er_run.build_limitations_section(
        status=EndpointStatus.validated_mvp,
        metrics=_metrics(auroc=0.80, auprc=0.60, bal=0.70),
        report=_report(),
        evaluation={"mode": "nested", "outer_splits": 5, "per_fold_selected": ["lasso_logreg"] * 5},
        floors=FLOORS,
        ceilings=CEILINGS,
        selected_model="lasso_logreg",
        claim_scope=None,  # -> generic conservative default
    )
    assert "UNSTABLE" not in text
    assert "Unmet validated_mvp criteria" not in text
    assert "consistent across folds" in text
    assert "validated_mvp" in text
    assert "pan-endocrine" in text  # generic default scope sentence


# --- integration: a fixture run renders the parameterized block end-to-end -------------


def _balanced_fused(dst: Path) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    genes = ["GENE_A", "GENE_B", "GENE_C", "GENE_D", "GENE_E"]
    patterns = [
        [1.5, 1.4, 1.3, 1.2, 1.1],
        [-1.5, -1.4, -1.3, -1.2, -1.1],
        [1.0, -1.0, 1.0, -1.0, 1.0],
        [0.1, 0.0, -0.1, 0.05, -0.05],
    ]
    sig, lab, mp = [], [], []
    for i in range(16):
        key = f"{chr(ord('A') + i) * 14}-{chr(ord('A') + i) * 10}-N"
        casrn = f"300-00-{i:02d}"
        sig.append({"compound_id": key, **dict(zip(genes, patterns[i % 4], strict=True))})
        lab.append(
            {
                "casrn": casrn,
                "target": "ER",
                "consensus_call": "active" if i < 8 else "inactive",
                "consensus_score": 0.9,
            }
        )
        mp.append(
            {
                "input_id": casrn,
                "input_id_type": "CASRN",
                "inchikey": key,
                "mapping_confidence": "exact",
            }
        )
    pd.DataFrame(sig).to_parquet(dst / "lincs.parquet", index=False)
    pd.DataFrame(lab).to_csv(dst / "cerapp.csv", index=False)
    pd.DataFrame(mp).to_csv(dst / "pubchem.csv", index=False)
    return dst


def test_generated_card_carries_scope_and_power(er_run, allow_list, tmp_path: Path) -> None:
    staged = _balanced_fused(tmp_path / "staged")
    output_root = tmp_path / "out"
    (output_root / "registry" / "models").mkdir(parents=True, exist_ok=True)
    (output_root / "registry" / "models" / "endpoints.json").write_text(
        '{\n  "endpoints": []\n}\n', encoding="utf-8"
    )
    config = er_run.PipelineConfig.model_validate(
        {
            "endpoint_id": "ER",
            "biological_target": "Estrogen Receptor",
            "version": "0.1.0",
            "claim_scope": ER_SCOPE,
            "data": {
                "target": "ER",
                "adapter": "staged",
                "staged_dir": str(staged),
                "n_groups": 5,
                "seed": 0,
                "required_label_sources": ["cerapp"],
            },
            "gate": {"thresholds_path": str(RELAXED_GATES)},
            "approval": {"approved": True, "approved_by": "test"},
            "evaluation": {"mode": "nested", "outer_splits": 5, "inner_splits": 3},
        }
    )
    res = er_run.run_pipeline(
        config,
        allow_list=allow_list,
        data_dir=staged,
        thresholds=er_run.load_thresholds(RELAXED_GATES),
        output_root=output_root,
    )
    assert res.trained is True
    card = (output_root / "models" / "ER" / "model_card.md").read_text()
    # Parameterized Limitations block reached the card with real, endpoint-specific text.
    assert ER_SCOPE in card
    assert "Statistical power:" in card
    assert "positives" in card and "prevalence" in card
    assert "Model-selection stability:" in card
