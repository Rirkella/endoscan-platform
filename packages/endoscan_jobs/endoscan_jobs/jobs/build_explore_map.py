"""build_explore_map job — a seeded UMAP over an endpoint's REAL curated signatures.

This is the *visualization* side of Explore. It reads the fused per-compound signature
matrix produced by :mod:`extract_signatures` (``curated/<TARGET>/signatures.parquet`` —
one 978-gene row per ``compound_id``) and writes three artifacts under
``artifacts/<TARGET>/explore/``:

  - ``umap.json``     — the 2-D map: one ``{compound_id, x, y, label}`` per real compound,
                        plus per-label counts. UMAP is a VISUALIZATION of how the training
                        signatures relate; it is not a model boundary or a claim of anything.
  - ``support.npy``   — the ORIGINAL 978-D training vectors (row order == manifest
                        ``compound_ids``). The API does request-time k-NN in THIS space
                        (never ``UMAP.transform`` on a new point), so a submitted signature
                        is placed by nearest neighbours, honestly labelled "approximate".
  - ``manifest.json`` — full provenance: the exact UMAP params + seed, the source-data
                        sha256, ``feature_names``/``compound_ids`` order, and the DEFINED
                        domain metric (distance to the k-th training neighbour) with its
                        training reference distribution. No asserted in-/out-of-domain flag.

Honesty rules baked in here:
  * The map is built ONLY from real curated signatures. If ``signatures.parquet`` is absent
    the job FAILS (``JobError``) — it never invents a synthetic matrix.
  * Labels are OPTIONAL (``--labels-csv`` mapping ``compound_id -> label``). They are joined
    to colour the map (a real active/inactive class-separation view). If the join yields no
    matches the map ships UNCOLOURED; unmatched compounds are ``label: null`` — a label is
    NEVER fabricated to colour a point.

Server-only heavy fit (``umap-learn`` is a jobs dep, never in the API). The real ER/AR maps
are computed on the server over the real ``signatures.parquet`` and the validated artifacts
are transferred into ``models/<ENDPOINT>/explore/`` (checksum-recorded, like ``model.pkl``);
CI exercises this whole code path on a small fixture matrix.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from ..runner import JobContext, JobError, JobOutcome

#: Reproducibility + shape defaults (all recorded in the manifest; overridable via options).
DEFAULT_SEED = 42
DEFAULT_N_NEIGHBORS = 15
DEFAULT_MIN_DIST = 0.1
DEFAULT_METRIC = "euclidean"
#: k for the domain metric — "distance to the k-th nearest training neighbour" (confirmed k=5).
DEFAULT_K = 5
#: Below this many compounds a 2-D neighbour embedding is not meaningful — fail honestly.
MIN_COMPOUNDS = 5

_ACTIVE = "active"
_INACTIVE = "inactive"
#: Accepted label encodings -> canonical class. Anything else -> unlabeled (never guessed).
_LABEL_MAP = {
    "1": _ACTIVE, "1.0": _ACTIVE, "active": _ACTIVE, "true": _ACTIVE, "pos": _ACTIVE,
    "0": _INACTIVE, "0.0": _INACTIVE, "inactive": _INACTIVE, "false": _INACTIVE, "neg": _INACTIVE,
}  # fmt: skip


def _load_labels(ctx: JobContext, compound_ids: list[str]) -> tuple[dict[str, str], str]:
    """Optional ``compound_id -> {active|inactive}`` join. Returns ``({}, status)`` for uncoloured.

    ``status`` is one of ``none`` (no ``--labels-csv``), ``unjoined`` (provided but 0 matched
    -> ship uncoloured), ``partial`` (some matched), ``joined`` (all matched). A label is never
    fabricated: unrecognised encodings and unmatched compounds simply stay unlabeled.
    """
    path = ctx.options.get("labels_csv")
    if not path:
        return {}, "none"
    from pathlib import Path  # noqa: PLC0415 - local, only when labels are requested

    lp = Path(path)
    if not lp.is_file():
        raise JobError(f"--labels-csv {path!r} not found")
    ldf = pd.read_csv(lp)
    id_col = ctx.options.get("labels_id_col") or "compound_id"
    label_col = ctx.options.get("labels_label_col") or "label"
    if id_col not in ldf.columns or label_col not in ldf.columns:
        raise JobError(
            f"--labels-csv must have {id_col!r} and {label_col!r} columns; got {list(ldf.columns)}"
        )
    id_set = set(compound_ids)
    matched: dict[str, str] = {}
    for cid, raw in zip(ldf[id_col].astype(str), ldf[label_col], strict=False):
        if cid not in id_set:
            continue
        canon = _LABEL_MAP.get(str(raw).strip().lower())
        if canon is not None:  # unrecognised encoding -> leave unlabeled, do NOT guess
            matched[cid] = canon
    if not matched:
        return {}, "unjoined"
    return matched, ("joined" if len(matched) == len(id_set) else "partial")


def _quantiles(values: np.ndarray) -> dict[str, float]:
    qs = np.quantile(values, [0.0, 0.25, 0.5, 0.75, 0.95, 1.0])
    keys = ["min", "p25", "median", "p75", "p95", "max"]
    return {k: float(v) for k, v in zip(keys, qs, strict=True)}


def build_explore_map_job(ctx: JobContext) -> JobOutcome:
    target = ctx.target
    seed = int(ctx.options.get("umap_seed") or DEFAULT_SEED)
    n_neighbors = int(ctx.options.get("umap_neighbors") or DEFAULT_N_NEIGHBORS)
    min_dist = float(ctx.options.get("umap_min_dist") or DEFAULT_MIN_DIST)
    metric = ctx.options.get("umap_metric") or DEFAULT_METRIC
    k = int(ctx.options.get("nn_k") or DEFAULT_K)

    # 1) Load the REAL curated signatures (never a synthetic matrix).
    sig_key = f"curated/{target}/signatures.parquet"
    if not ctx.storage.exists(sig_key):
        raise JobError(
            f"no curated signatures at {sig_key!r}; run extract_signatures first "
            "(the map is built from real data only — no synthetic matrix)"
        )
    raw = ctx.storage.get_bytes(sig_key)
    source_sha = hashlib.sha256(raw).hexdigest()
    df = pd.read_parquet(io.BytesIO(raw))
    if "compound_id" not in df.columns:
        raise JobError("signatures.parquet is missing the required 'compound_id' column")
    feature_names = [c for c in df.columns if c != "compound_id"]
    if not feature_names:
        raise JobError("signatures.parquet has no gene feature columns")
    compound_ids = df["compound_id"].astype(str).tolist()
    n = len(compound_ids)
    if n < MIN_COMPOUNDS:
        raise JobError(f"only {n} compounds; need >= {MIN_COMPOUNDS} for a 2-D data-space map")
    X = df[feature_names].to_numpy(dtype=float)
    if not np.isfinite(X).all():
        raise JobError("signature matrix contains non-finite values; cannot build the map")

    eff_neighbors = min(n_neighbors, n - 1)  # UMAP requires n_neighbors < n_samples
    eff_k = min(k, n - 1)  # k-th neighbour must exist
    labels_map, label_status = _load_labels(ctx, compound_ids)
    ctx.log("explore_input", n_compounds=n, n_features=len(feature_names), labels=label_status)

    # 2) Seeded UMAP fit (heavy; umap-learn is a jobs-only dependency). random_state makes it
    #    deterministic (umap forces single-threaded when seeded) -> reproducible artifact.
    import umap  # noqa: PLC0415 - heavy import, only when the job actually runs

    reducer = umap.UMAP(
        n_neighbors=eff_neighbors,
        min_dist=min_dist,
        metric=metric,
        n_components=2,
        random_state=seed,
    )
    embedding = np.asarray(reducer.fit_transform(X), dtype=float)
    ctx.log("umap_fit", n_neighbors=eff_neighbors, min_dist=min_dist, metric=metric, seed=seed)

    # 3) DEFINED domain metric reference: each training point's distance to its k-th nearest
    #    OTHER training point, in the ORIGINAL 978-D space (not the 2-D map).
    from sklearn.neighbors import NearestNeighbors  # noqa: PLC0415 - heavy, job-time only

    nn = NearestNeighbors(n_neighbors=eff_k + 1, metric=metric).fit(X)
    dists, _ = nn.kneighbors(X)  # column 0 is self (distance 0)
    kth_distances = np.sort(dists[:, eff_k])  # k-th nearest OTHER point, ascending

    # 4) Build points (colour by joined label; unmatched -> null, never fabricated).
    n_active = n_inactive = n_unlabeled = 0
    points: list[dict] = []
    for cid, (x, y) in zip(compound_ids, embedding, strict=True):
        label = labels_map.get(cid)
        if label == _ACTIVE:
            n_active += 1
        elif label == _INACTIVE:
            n_inactive += 1
        else:
            n_unlabeled += 1
        points.append({"compound_id": cid, "x": float(x), "y": float(y), "label": label})
    counts = {
        "n_total": n,
        "n_active": n_active,
        "n_inactive": n_inactive,
        "n_unlabeled": n_unlabeled,
    }

    # 5) Support vectors (original space, manifest-ordered) for request-time k-NN placement.
    support_buf = io.BytesIO()
    np.save(support_buf, X.astype(np.float64))
    support_bytes = support_buf.getvalue()
    support_sha = hashlib.sha256(support_bytes).hexdigest()

    manifest = {
        "target": target,
        "n_compounds": n,
        "feature_names": feature_names,
        "compound_ids": compound_ids,  # row order of support.npy AND of the points
        "umap": {
            "n_neighbors": eff_neighbors,
            "requested_n_neighbors": n_neighbors,
            "min_dist": min_dist,
            "metric": metric,
            "n_components": 2,
            "random_state": seed,
            "umap_version": getattr(umap, "__version__", "unknown"),
        },
        "domain_metric": {
            "metric": "distance_to_kth_training_neighbor",
            "k": eff_k,
            "requested_k": k,
            "quantiles": _quantiles(kth_distances),
            "training_kth_nn_distances": [float(v) for v in kth_distances],
        },
        "labels": {"status": label_status, **counts},
        "source": {"key": sig_key, "sha256": source_sha},
        "support_sha256": support_sha,
        "built_at": datetime.now(UTC).isoformat(),
    }
    umap_doc = {"target": target, "counts": counts, "points": points}

    base = f"artifacts/{target}/explore"
    ctx.storage.put_text(f"{base}/umap.json", json.dumps(umap_doc))
    ctx.storage.put_bytes(f"{base}/support.npy", support_bytes)
    ctx.storage.put_text(f"{base}/manifest.json", json.dumps(manifest, indent=2))
    ctx.log("explore_built", base=base, n_compounds=n, labels=label_status)

    summary = (
        f"{target} explore map: built; {n} compounds "
        f"({n_active} active / {n_inactive} inactive / {n_unlabeled} unlabeled), "
        f"UMAP(n_neighbors={eff_neighbors}, min_dist={min_dist}, seed={seed}). "
        f"Transfer {base}/* -> models/{target}/explore/ after review."
    )
    return JobOutcome(report_key=f"{base}/umap.json", summary=summary, verdict="built")
