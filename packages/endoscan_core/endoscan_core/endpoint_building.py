"""Deterministic scientific operations for the endpoint-building lifecycle.

The workflow package owns persistence and approvals.  This module owns the
framework-free transformations that turn an approved recipe and immutable
source rows into a leakage-audited dataset, persisted Explore projections and
bounded baseline model comparisons.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.decomposition import PCA
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    balanced_accuracy_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_score,
    roc_auc_score,
)
from sklearn.neighbors import NearestNeighbors
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

SEED = 17


@dataclass(frozen=True)
class AssembledDataset:
    X: pd.DataFrame
    y: pd.Series
    sample_metadata: pd.DataFrame
    compound_metadata: pd.DataFrame
    selected_signature_ids: list[str]
    gene_schema: list[str]
    split_assignments: pd.DataFrame
    excluded_records: pd.DataFrame
    raw_activity_records: pd.DataFrame


@dataclass(frozen=True)
class CandidateBenchmark:
    name: str
    model: Any
    validation_probabilities: np.ndarray
    metrics: dict[str, Any]


def stable_identifier(value: str) -> str:
    """Return an upper-case, whitespace-normalized stable identifier."""

    return " ".join(str(value).strip().upper().split())


def deterministic_compound_splits(
    compound_labels: dict[str, int], *, seed: int = SEED
) -> pd.DataFrame:
    """Create class-aware compound-grouped train/validation/test assignments.

    A compound appears in exactly one split.  Assignment is hash ordered and
    class-stratified, avoiding row-level leakage while remaining replay stable.
    """

    if len(compound_labels) < 6:
        raise ValueError("At least six resolved compounds are required for three splits.")
    by_label: dict[int, list[str]] = defaultdict(list)
    for compound_id, label in compound_labels.items():
        if label not in {0, 1}:
            raise ValueError("Only approved binary derived labels may enter benchmarking.")
        by_label[label].append(compound_id)
    if set(by_label) != {0, 1}:
        raise ValueError("Both derived label classes are required.")

    rows: list[dict[str, Any]] = []
    for label, compounds in sorted(by_label.items()):
        ordered = sorted(
            compounds,
            key=lambda item: hashlib.sha256(f"{seed}:{item}".encode()).hexdigest(),
        )
        count = len(ordered)
        train_end = max(1, math.floor(count * 0.6))
        validation_end = max(train_end + 1, math.floor(count * 0.8))
        validation_end = min(validation_end, count - 1)
        for index, compound_id in enumerate(ordered):
            split = (
                "train" if index < train_end else "validation" if index < validation_end else "test"
            )
            rows.append({"compound_id": compound_id, "label": label, "split": split})
    result = pd.DataFrame(rows).sort_values("compound_id").reset_index(drop=True)
    if result["compound_id"].duplicated().any():
        raise AssertionError("A compound was assigned to more than one split.")
    if set(result["split"]) != {"train", "validation", "test"}:
        raise ValueError("Every compound split must be represented.")
    return result


def assemble_approved_dataset(
    *,
    activity_rows: list[dict[str, Any]],
    transcriptomic_rows: list[dict[str, Any]],
    expression_rows: list[dict[str, Any]],
    approved_activity_sources: set[str],
    approved_transcriptomic_sources: set[str],
    approved_modalities: set[str],
    approved_label_policy: dict[str, Any],
    approved_context_filters: dict[str, Any],
    exclusion_rules: list[str],
) -> AssembledDataset:
    """Apply an approved recipe without overwriting the raw source outcomes."""

    if not approved_modalities:
        raise ValueError("An assembly recipe must approve at least one modality.")
    if not approved_activity_sources or not approved_transcriptomic_sources:
        raise ValueError("An assembly recipe must approve activity and transcriptomic sources.")
    operator = approved_label_policy.get("operator")
    if operator not in {"source_backed_activity_call", "functional_or"}:
        raise ValueError("The approved label policy is not executable by this builder.")

    activity = pd.DataFrame(activity_rows)
    transcriptomic = pd.DataFrame(transcriptomic_rows)
    expression = pd.DataFrame(expression_rows)
    required_activity = {
        "compound_id",
        "source_id",
        "modality",
        "raw_outcome",
        "provenance_id",
    }
    required_profiles = {
        "signature_id",
        "compound_id",
        "cell",
        "dose",
        "time",
        "provider",
        "provenance_id",
    }
    if not required_activity.issubset(activity.columns):
        raise ValueError("Activity artifacts do not contain the recipe-required fields.")
    if not required_profiles.issubset(transcriptomic.columns):
        raise ValueError("Transcriptomic artifacts do not contain the recipe-required fields.")
    if "signature_id" not in expression.columns:
        raise ValueError("Expression artifacts require signature_id.")

    activity = activity.copy()
    transcriptomic = transcriptomic.copy()
    if activity["compound_id"].isna().any() or transcriptomic["compound_id"].isna().any():
        raise ValueError("Unresolved compound identities cannot enter assembly.")
    activity["compound_id"] = activity["compound_id"].map(stable_identifier)
    transcriptomic["compound_id"] = transcriptomic["compound_id"].map(stable_identifier)
    if (activity["compound_id"] == "").any() or (transcriptomic["compound_id"] == "").any():
        raise ValueError("Unresolved compound identities cannot enter assembly.")
    unknown_activity_sources = set(activity["source_id"].astype(str)) - approved_activity_sources
    unknown_transcriptomic_sources = (
        set(transcriptomic["provider"].astype(str)) - approved_transcriptomic_sources
    )
    if unknown_activity_sources or unknown_transcriptomic_sources:
        raise ValueError("Assembly input contains source access not approved by the recipe.")
    activity = activity[activity["modality"].isin(approved_modalities)].copy()
    for key in ("cell", "dose", "time", "provider"):
        allowed = approved_context_filters.get(key)
        if allowed is not None:
            values = {str(value) for value in (allowed if isinstance(allowed, list) else [allowed])}
            transcriptomic = transcriptomic[transcriptomic[key].astype(str).isin(values)]

    valid_outcomes = activity["raw_outcome"].isin(["active", "inactive"])
    excluded = activity.loc[~valid_outcomes].copy()
    if not excluded.empty:
        excluded["exclusion_reason"] = "ambiguous_or_missing_source_outcome"
    labeled = activity.loc[valid_outcomes].copy()
    labeled["derived_label"] = (labeled["raw_outcome"] == "active").astype(int)

    conflicts: list[dict[str, Any]] = []
    compound_labels: dict[str, int] = {}
    for compound_id, group in labeled.groupby("compound_id", sort=True):
        labels = set(group["derived_label"].astype(int))
        if operator == "functional_or":
            compound_labels[compound_id] = int(1 in labels)
        elif len(labels) == 1:
            compound_labels[compound_id] = labels.pop()
        else:
            conflicts.append(
                {
                    "compound_id": compound_id,
                    "exclusion_reason": "conflicting_source_outcomes",
                }
            )
    if conflicts:
        excluded = pd.concat([excluded, pd.DataFrame(conflicts)], ignore_index=True)

    profiles = transcriptomic[transcriptomic["compound_id"].isin(compound_labels)].copy()
    profiles["derived_label"] = profiles["compound_id"].map(compound_labels).astype(int)
    joined = profiles.merge(expression, on="signature_id", how="inner", validate="one_to_one")
    if joined.empty:
        raise ValueError("The approved source combination produced no joinable profiles.")
    feature_columns = sorted(column for column in expression.columns if column != "signature_id")
    if not feature_columns:
        raise ValueError("No expression features were present.")
    if joined[feature_columns].isna().any().any():
        raise ValueError("Expression features contain missing values.")

    splits = deterministic_compound_splits(compound_labels)
    metadata = joined[
        [
            "signature_id",
            "compound_id",
            "cell",
            "dose",
            "time",
            "provider",
            "provenance_id",
            "derived_label",
        ]
    ].merge(splits[["compound_id", "split"]], on="compound_id", how="left", validate="many_to_one")
    if metadata["split"].isna().any():
        raise AssertionError("A selected profile lacks a compound-grouped split.")
    compound_metadata = (
        labeled.groupby("compound_id", sort=True)
        .agg(
            modalities=("modality", lambda values: sorted(set(values))),
            activity_sources=("source_id", lambda values: sorted(set(values))),
            activity_provenance=("provenance_id", lambda values: sorted(set(values))),
        )
        .reset_index()
    )
    compound_metadata["derived_label"] = compound_metadata["compound_id"].map(compound_labels)
    compound_metadata = compound_metadata[compound_metadata["derived_label"].notna()].copy()
    compound_metadata["approved_exclusion_rules"] = [list(exclusion_rules)] * len(compound_metadata)
    return AssembledDataset(
        X=joined[feature_columns].reset_index(drop=True),
        y=metadata["derived_label"].astype(int).reset_index(drop=True),
        sample_metadata=metadata.reset_index(drop=True),
        compound_metadata=compound_metadata.reset_index(drop=True),
        selected_signature_ids=metadata["signature_id"].tolist(),
        gene_schema=feature_columns,
        split_assignments=splits,
        excluded_records=excluded.reset_index(drop=True),
        raw_activity_records=activity.reset_index(drop=True),
    )


def dataset_quality_summary(dataset: AssembledDataset) -> dict[str, Any]:
    """Return deterministic quality and leakage diagnostics for human review."""

    metadata = dataset.sample_metadata
    compound_splits = metadata.groupby("compound_id")["split"].nunique()
    leaking = sorted(compound_splits[compound_splits > 1].index.tolist())
    class_counts = Counter(int(value) for value in dataset.y)
    return {
        "unique_compounds": int(metadata["compound_id"].nunique()),
        "total_profiles": int(len(metadata)),
        "class_distribution": {str(key): value for key, value in sorted(class_counts.items())},
        "cell_distribution": metadata["cell"].value_counts().sort_index().to_dict(),
        "dose_distribution": metadata["dose"].astype(str).value_counts().sort_index().to_dict(),
        "time_distribution": metadata["time"].astype(str).value_counts().sort_index().to_dict(),
        "provider_distribution": metadata["provider"].value_counts().sort_index().to_dict(),
        "missing_values": int(dataset.X.isna().sum().sum() + metadata.isna().sum().sum()),
        "excluded_records": int(len(dataset.excluded_records)),
        "split_sizes": metadata["split"].value_counts().sort_index().to_dict(),
        "compound_split_leakage": leaking,
        "leakage_check_passed": not leaking,
        "warnings": (
            []
            if len(dataset.excluded_records) == 0
            else ["Some source records were excluded under the approved recipe."]
        ),
    }


def build_explore_payload(dataset: AssembledDataset) -> dict[str, Any]:
    """Build persisted PCA/nearest-neighbour artifacts for an approved dataset."""

    scaled = StandardScaler().fit_transform(dataset.X)
    coordinates = PCA(n_components=2, random_state=SEED).fit_transform(scaled)
    neighbors = min(6, len(dataset.X))
    distances, indices = NearestNeighbors(n_neighbors=neighbors).fit(scaled).kneighbors(scaled)
    points = []
    for index, row in dataset.sample_metadata.reset_index(drop=True).iterrows():
        points.append(
            {
                **row.to_dict(),
                "x": float(coordinates[index, 0]),
                "y": float(coordinates[index, 1]),
                "projection": "pca_2d_offline_fallback",
                "neighbors": [
                    {
                        "signature_id": dataset.sample_metadata.iloc[int(neighbor)]["signature_id"],
                        "distance": float(distance),
                    }
                    for neighbor, distance in zip(
                        indices[index][1:], distances[index][1:], strict=True
                    )
                ],
            }
        )
    compound_ids = dataset.sample_metadata["compound_id"].astype(str).reset_index(drop=True)
    compound_support = (
        dataset.X.reset_index(drop=True)
        .assign(compound_id=compound_ids)
        .groupby("compound_id", sort=True)
        .mean(numeric_only=True)
    )
    compound_labels = (
        dataset.sample_metadata.assign(compound_id=compound_ids)
        .groupby("compound_id", sort=True)["derived_label"]
        .first()
        .astype(int)
    )
    compound_scaled = StandardScaler().fit_transform(compound_support)
    projection_method = "pca_2d_offline_fallback"
    if len(compound_support) >= 50:
        from umap import UMAP  # type: ignore[import-not-found]  # jobs/runtime dependency

        compound_coordinates = UMAP(
            n_neighbors=min(15, len(compound_support) - 1),
            min_dist=0.1,
            metric="euclidean",
            n_components=2,
            random_state=SEED,
        ).fit_transform(compound_scaled)
        projection_method = "umap_2d"
    elif len(compound_support) >= 2:
        compound_coordinates = PCA(n_components=2, random_state=SEED).fit_transform(compound_scaled)
    else:
        compound_coordinates = np.zeros((len(compound_support), 2), dtype=float)
    domain_neighbors = min(6, len(compound_support))
    compound_distances = (
        NearestNeighbors(n_neighbors=domain_neighbors)
        .fit(compound_scaled)
        .kneighbors(compound_scaled)[0]
    )
    domain_k = max(0, domain_neighbors - 1)
    kth_distances = compound_distances[:, -1].astype(float).tolist() if domain_k else [0.0]
    map_points = [
        {
            "compound_id": compound_id,
            "x": float(compound_coordinates[index, 0]),
            "y": float(compound_coordinates[index, 1]),
            "label": "active" if int(compound_labels.loc[compound_id]) else "inactive",
        }
        for index, compound_id in enumerate(compound_support.index.astype(str))
    ]
    return {
        "projection_method": projection_method,
        "points": points,
        "map_points": map_points,
        "reference_support": compound_support,
        "domain_metric": {
            "metric": "euclidean_in_standardized_feature_space",
            "k": domain_k,
            "training_kth_nn_distances": sorted(kth_distances),
            "quantiles": {
                "q50": float(np.quantile(kth_distances, 0.5)),
                "q90": float(np.quantile(kth_distances, 0.9)),
                "q95": float(np.quantile(kth_distances, 0.95)),
            },
        },
        "filters": ["derived_label", "cell", "dose", "time", "provider"],
        "recomputed_on_page_load": False,
    }


def _classification_metrics(y_true: np.ndarray, probabilities: np.ndarray) -> dict[str, Any]:
    predicted = (probabilities >= 0.5).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_true, predicted, labels=[0, 1]).ravel()
    sensitivity = float(tp / (tp + fn)) if tp + fn else 0.0
    specificity = float(tn / (tn + fp)) if tn + fp else 0.0
    return {
        "roc_auc": float(roc_auc_score(y_true, probabilities)),
        "pr_auc": float(average_precision_score(y_true, probabilities)),
        "balanced_accuracy": float(balanced_accuracy_score(y_true, predicted)),
        "sensitivity": sensitivity,
        "specificity": specificity,
        "precision": float(precision_score(y_true, predicted, zero_division=0)),
        "f1": float(f1_score(y_true, predicted, zero_division=0)),
        "brier_score": float(brier_score_loss(y_true, probabilities)),
        "calibration_summary": {"mean_probability": float(np.mean(probabilities))},
    }


def benchmark_models(dataset: AssembledDataset) -> list[CandidateBenchmark]:
    """Benchmark three registered baselines without touching the held-out test set."""

    metadata = dataset.sample_metadata.reset_index(drop=True)
    fit_mask = metadata["split"] == "train"
    validation_mask = metadata["split"] == "validation"
    if dataset.y[fit_mask].nunique() != 2 or dataset.y[validation_mask].nunique() != 2:
        raise ValueError("Train and validation partitions must contain both classes.")
    models = {
        "logistic_regression": Pipeline(
            [
                ("scale", StandardScaler()),
                (
                    "classifier",
                    LogisticRegression(class_weight="balanced", random_state=SEED, max_iter=2000),
                ),
            ]
        ),
        "random_forest": RandomForestClassifier(
            n_estimators=64, class_weight="balanced", random_state=SEED
        ),
        "hist_gradient_boosting": HistGradientBoostingClassifier(max_iter=80, random_state=SEED),
    }
    results = []
    for name, prototype in models.items():
        model = clone(prototype).fit(dataset.X[fit_mask], dataset.y[fit_mask])
        probabilities = model.predict_proba(dataset.X[validation_mask])[:, 1]
        metrics = _classification_metrics(dataset.y[validation_mask].to_numpy(), probabilities)
        metrics.update(
            {
                "fold_stability": (
                    "single deterministic validation partition; grouped CV pending larger data"
                ),
                "training_time_ms": 0.0,
                "inference_time_ms": 0.0,
                "timing_status": "not_profiled_in_deterministic_offline_validation",
                "model_size_bytes": len(__import__("pickle").dumps(model)),
                "explanation_available": name != "hist_gradient_boosting",
                "warnings": [],
                "failed_folds": [],
            }
        )
        results.append(CandidateBenchmark(name, model, probabilities, metrics))
    return results


def validate_selected_model(
    dataset: AssembledDataset, model: Any, *, threshold: float = 0.5
) -> dict[str, Any]:
    """Evaluate the human-selected candidate exactly once on held-out compounds."""

    metadata = dataset.sample_metadata.reset_index(drop=True)
    train_validation = metadata["split"].isin(["train", "validation"])
    test = metadata["split"] == "test"
    if dataset.y[test].nunique() != 2:
        raise ValueError("Held-out test partition must contain both classes.")
    selected = clone(model).fit(dataset.X[train_validation], dataset.y[train_validation])
    probabilities = selected.predict_proba(dataset.X[test])[:, 1]
    metrics = _classification_metrics(dataset.y[test].to_numpy(), probabilities)
    metrics.update(
        {
            "threshold": threshold,
            "held_out_compounds": sorted(metadata.loc[test, "compound_id"].unique()),
            "test_profiles": int(test.sum()),
        }
    )
    return {"model": selected, "metrics": metrics}
