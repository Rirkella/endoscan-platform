"""Regenerate the FIXTURE_ER inference fixture endpoint (tiny, git-tracked).

Run from the repo root:
    uv run python tests/fixtures/inference/make_fixture_endpoint.py

Produces a self-contained "registry repo root" under tests/fixtures/inference/ with a
TINY ``Pipeline(StandardScaler, RandomForestClassifier)`` model.pkl (5 genes), a
``feature_schema.json``, and a ``metrics.json`` that ALREADY carries the inference layer structured
fields (``validated_mvp_floors``/``_ceilings``, ``claim_scope``) with values chosen so
balanced_accuracy is BELOW its floor — i.e. status ``experimental`` with exactly one
missed criterion. The model is for exercising predict + TreeSHAP offline; its metric
VALUES are a fixture record, not a real estimate.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

HERE = Path(__file__).parent
GENES = ["G1", "G2", "G3", "G4", "G5"]
ENDPOINT = "FIXTURE_ER"
CLAIM_SCOPE = (
    "ER functional modulation (agonist or antagonist transactivation; direction not "
    "distinguished); not a physical-binding, pan-endocrine, or regulatory/clinical/"
    "diagnostic claim"
)


def main() -> None:
    rng = np.random.default_rng(0)
    n = 60
    # Weak, separable-ish synthetic signal so the RF is a real (tiny) tree model.
    y = np.array([1] * 24 + [0] * 36)
    X = rng.normal(size=(n, len(GENES)))
    X[:24, 0] += 1.2  # G1 nudges positives
    X[:24, 1] -= 0.8  # G2 nudges positives the other way

    model = Pipeline(
        [
            ("scaler", StandardScaler()),
            ("clf", RandomForestClassifier(n_estimators=25, random_state=0)),
        ]
    )
    model.fit(X, y)

    model_dir = HERE / "models" / ENDPOINT
    model_dir.mkdir(parents=True, exist_ok=True)
    with (model_dir / "model.pkl").open("wb") as fh:
        pickle.dump(model, fh, protocol=5)

    (model_dir / "feature_schema.json").write_text(
        json.dumps({"features": GENES, "standardized": True, "schema_version": "0.1"}, indent=2),
        encoding="utf-8",
    )

    metrics = {
        "auroc": 0.70,
        "auprc": 0.30,
        "balanced_accuracy": 0.58,  # < the 0.65 floor -> the one missed criterion
        "f1": 0.30,
        "brier_score": 0.10,
        "confusion_matrix": {"tn": 30, "fp": 6, "fn": 14, "tp": 10},  # 24 pos / 36 neg
        "n_samples": 60,
        "n_compounds": 60,
        "selected_model": "random_forest",
        "threshold": 0.5,
        "evaluation": {
            "mode": "nested",
            "outer_splits": 5,
            "inner_splits": 3,
            "per_fold_selected": [
                "random_forest",
                "lasso_logreg",
                "random_forest",
                "random_forest",
                "gradient_boosting",
            ],
        },
        "validated_mvp_floors": {"auroc": 0.65, "auprc": 0.20, "balanced_accuracy": 0.65},
        "validated_mvp_ceilings": {"brier_score": 0.20},
        "claim_scope": CLAIM_SCOPE,
        "estimate": "fixture record (not a real estimate)",
    }
    (model_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    (model_dir / "model_card.md").write_text(
        "# Model Card — FIXTURE_ER\n\nTiny offline inference fixture (status experimental).\n",
        encoding="utf-8",
    )
    (model_dir / "dataset_card.md").write_text(
        "# Dataset Card — FIXTURE_ER\n\nSynthetic 5-gene fixture; not real data.\n",
        encoding="utf-8",
    )

    entry = {
        "endpoint_id": ENDPOINT,
        "biological_target": "Estrogen Receptor (inference fixture)",
        "input_type": "transcriptomics",
        "model_path": f"models/{ENDPOINT}/model.pkl",
        "feature_schema_path": f"models/{ENDPOINT}/feature_schema.json",
        "metrics_path": f"models/{ENDPOINT}/metrics.json",
        "explainer_path": None,
        "model_card_path": f"models/{ENDPOINT}/model_card.md",
        "dataset_card_path": f"models/{ENDPOINT}/dataset_card.md",
        "status": "experimental",
        "version": "0.1.0",
        "created_at": "2026-06-25T00:00:00Z",
        "source_refs": ["fixture:inference-er-v0"],
    }
    index_path = HERE / "registry" / "models" / "endpoints.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps({"endpoints": [entry]}, indent=2) + "\n", encoding="utf-8")
    print(f"wrote FIXTURE_ER inference fixture under {HERE}")


if __name__ == "__main__":
    main()
