"""Explore data-space (UMAP) routes — thin, read-only, NO science, NO fake projection.

``GET  /explore/{context}/umap``   serve the committed map (points + counts + provenance);
                                   404 "not yet computed" when no artifact is committed.
``POST /explore/locate``           place a submitted signature by exact k-NN in the ORIGINAL
                                   978-gene space (never ``UMAP.transform``); return the
                                   nearest real neighbours, an APPROXIMATE centroid position,
                                   and a DEFINED domain metric — never an in-/out-of-domain flag.

The one authoritative validator (``align_signature``) checks the query; its
``SignatureValidationError`` maps to 422 via the existing handler.
"""

from __future__ import annotations

import bisect
from pathlib import Path

import numpy as np
from fastapi import APIRouter, Depends

from endoscan_core.features.landmark_features import FeatureSchema
from endoscan_core.inference import align_signature

from ..deps import get_repo_root
from ..explore_store import load_map, load_support
from ..schemas import (
    ExploreCounts,
    ExploreDomain,
    ExploreLocateRequest,
    ExploreLocateResponse,
    ExploreManifestSummary,
    ExploreMapResponse,
    ExploreNeighbor,
    ExplorePoint,
)

router = APIRouter(tags=["explore"])


def _manifest_summary(context: str, manifest: dict) -> ExploreManifestSummary:
    return ExploreManifestSummary(
        target=manifest.get("target", context),
        n_compounds=int(manifest.get("n_compounds", 0)),
        umap=manifest.get("umap", {}),
        domain_metric_k=int(manifest.get("domain_metric", {}).get("k", 0)),
        label_status=manifest.get("labels", {}).get("status", "none"),
        source_sha256=manifest.get("source", {}).get("sha256", ""),
        built_at=manifest.get("built_at"),
    )


@router.get("/explore/{context}/umap", response_model=ExploreMapResponse)
def explore_umap(context: str, repo_root: Path = Depends(get_repo_root)) -> ExploreMapResponse:
    """Serve the committed data-space map for ``context`` (missing artifact -> 404)."""
    umap_doc, manifest = load_map(repo_root, context)
    points = [ExplorePoint(**p) for p in umap_doc.get("points", [])]
    counts = ExploreCounts(**umap_doc["counts"])
    return ExploreMapResponse(
        context=context,
        points=points,
        counts=counts,
        manifest=_manifest_summary(context, manifest),
    )


@router.post("/explore/locate", response_model=ExploreLocateResponse)
def explore_locate(
    body: ExploreLocateRequest, repo_root: Path = Depends(get_repo_root)
) -> ExploreLocateResponse:
    """Place a signature by nearest neighbours — NOT a projection; NO domain verdict.

    404 if the map is not computed; 422 (via the shared handler) if the signature fails the
    schema; 500 if the support artifact fails its integrity hash.
    """
    umap_doc, manifest = load_map(repo_root, body.context)  # 404 handler if missing
    support = load_support(repo_root, body.context, manifest)  # 500 handler if corrupt

    # Align the query to the MAP's exact feature order (the space the map was built in), using
    # the ONE authoritative validator. SignatureValidationError -> 422 via the shared handler.
    feature_names = manifest["feature_names"]
    schema = FeatureSchema(features=list(feature_names))
    aligned, _is_single = align_signature(body.signature, schema, allow_extra=body.allow_extra)
    query = aligned[0]

    # Exact k-NN in the ORIGINAL gene space (euclidean == the map/manifest metric). No transform.
    distances = np.linalg.norm(support - query, axis=1)
    order = np.argsort(distances, kind="stable")
    domain = manifest["domain_metric"]
    k = int(domain["k"])
    k = min(k, len(order))

    compound_ids = manifest["compound_ids"]
    coord = {p["compound_id"]: p for p in umap_doc.get("points", [])}
    neighbors: list[ExploreNeighbor] = []
    for idx in order[:k]:
        cid = compound_ids[idx]
        pt = coord.get(cid, {})
        neighbors.append(
            ExploreNeighbor(
                compound_id=cid,
                distance=float(distances[idx]),
                x=float(pt.get("x", 0.0)),
                y=float(pt.get("y", 0.0)),
                label=pt.get("label"),
            )
        )
    # Approximate placement = centroid of the neighbours' PRECOMPUTED map coords (honest;
    # never a claimed exact 2-D projection of the query).
    approx_xy = {
        "x": float(np.mean([n.x for n in neighbors])),
        "y": float(np.mean([n.y for n in neighbors])),
    }

    # DEFINED domain metric: the query's distance to its k-th nearest training neighbour,
    # read against the training reference distribution. No in-/out-of-domain boolean.
    query_kth = float(distances[order[k - 1]])
    reference = domain.get("training_kth_nn_distances", [])
    percentile = bisect.bisect_right(reference, query_kth) / len(reference) if reference else 0.0

    return ExploreLocateResponse(
        context=body.context,
        placement="approximate_nearest_neighbor",
        approx_xy=approx_xy,
        neighbors=neighbors,
        domain=ExploreDomain(
            metric=domain["metric"],
            k=k,
            query_kth_distance=query_kth,
            training_reference_quantiles=domain.get("quantiles", {}),
            percentile=percentile,
        ),
    )
