"""Explore data-space (UMAP) routes — thin, read-only, NO science, NO fake projection.

``GET  /explore/{context}/umap``   serve the committed map (points + counts + provenance);
                                   404 "not yet computed" when no artifact is committed.
``POST /explore/locate``           place a submitted signature by exact k-NN in the ORIGINAL
                                   978-gene space (never ``UMAP.transform``); use stored map
                                   coordinates for an exact existing record, otherwise return an
                                   approximate neighbour centroid and a defined isolation metric.

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

from ..catalogue_store import identity_index
from ..deps import get_repo_root
from ..explore_store import load_map, load_reference_row, load_support
from ..schemas import (
    ExploreCounts,
    ExploreDomain,
    ExploreLocateRequest,
    ExploreLocateResponse,
    ExploreManifestSummary,
    ExploreMapResponse,
    ExploreNeighbor,
    ExplorePoint,
    ExploreReferenceSignature,
)

router = APIRouter(tags=["explore"])

_EXACT_TOLERANCE = 1e-9


def _identity_fields(compound_id: str, identities: dict, _source_key: str) -> dict:
    compound = identities.get(compound_id)
    if compound is None:
        return {
            "preferred_name": None,
            "pubchem_cid": None,
            "source_dataset": "LINCS L1000",
            "experimental_contexts": [],
            "full_signature_id": None,
            "full_vector_available": True,
            "underlying_condition_available": False,
            "profile_type": "Aggregated reference profile",
        }
    signature = compound.signatures[0] if compound.signatures else None
    contexts = []
    if signature is not None:
        context = " + ".join(signature.cell_lines)
        qualifiers = [value for value in (signature.dose, signature.timepoint) if value]
        contexts = [" · ".join([context, *qualifiers]) if qualifiers else context]
    return {
        "preferred_name": compound.preferred_name,
        "pubchem_cid": compound.pubchem_cid,
        "source_dataset": signature.dataset if signature else "LINCS L1000",
        "experimental_contexts": contexts,
        "full_signature_id": signature.signature_id if signature else None,
        "full_vector_available": True,
        "underlying_condition_available": False,
        "profile_type": "Aggregated reference profile",
    }


def _similarity_category(rank: int, n_reference: int) -> str:
    percentile = rank / max(1, n_reference)
    if percentile <= 0.01:
        return "very similar response"
    if percentile <= 0.05:
        return "similar response"
    if percentile <= 0.20:
        return "moderately similar response"
    return "distant response"


def _manifest_summary(context: str, manifest: dict) -> ExploreManifestSummary:
    return ExploreManifestSummary(
        target=manifest.get("target", context),
        n_compounds=int(manifest.get("n_compounds", 0)),
        umap=manifest.get("umap", {}),
        domain_metric_k=int(manifest.get("domain_metric", {}).get("k", 0)),
        label_status=manifest.get("labels", {}).get("status", "none"),
        source_sha256=manifest.get("source", {}).get("sha256", ""),
        source_key=manifest.get("source", {}).get("key", ""),
        point_definition=manifest.get(
            "point_definition",
            "one measured compound-level signature per canonical InChIKey",
        ),
        aggregation=manifest.get("aggregation", {}),
        built_at=manifest.get("built_at"),
    )


@router.get("/explore/{context}/umap", response_model=ExploreMapResponse)
def explore_umap(context: str, repo_root: Path = Depends(get_repo_root)) -> ExploreMapResponse:
    """Serve the committed data-space map for ``context`` (missing artifact -> 404)."""
    umap_doc, manifest = load_map(repo_root, context)
    identities = identity_index(repo_root)
    source_key = manifest.get("source", {}).get("key", "")
    points = [
        ExplorePoint(
            **p,
            **_identity_fields(str(p["compound_id"]), identities, source_key),
        )
        for p in umap_doc.get("points", [])
    ]
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
    exact_idx = int(order[0]) if len(order) and distances[order[0]] <= _EXACT_TOLERANCE else None
    distinct_order = [int(idx) for idx in order if int(idx) != exact_idx]
    k = min(k, len(distinct_order))

    compound_ids = manifest["compound_ids"]
    coord = {p["compound_id"]: p for p in umap_doc.get("points", [])}
    identities = identity_index(repo_root)
    source_key = manifest.get("source", {}).get("key", "")

    def neighbor(idx: int, rank: int, category: str | None = None) -> ExploreNeighbor:
        cid = compound_ids[idx]
        pt = coord.get(cid, {})
        return ExploreNeighbor(
            compound_id=cid,
            distance=float(distances[idx]),
            x=float(pt.get("x", 0.0)),
            y=float(pt.get("y", 0.0)),
            label=pt.get("label"),
            **_identity_fields(cid, identities, source_key),
            similarity_category=category or _similarity_category(rank, len(distinct_order)),
            similarity_rank=rank,
            similarity_percentile=rank / max(1, len(distinct_order)),
        )

    neighbors: list[ExploreNeighbor] = []
    for rank, idx in enumerate(distinct_order[:k], start=1):
        neighbors.append(neighbor(idx, rank))

    exact_match = neighbor(exact_idx, 0, "exact match") if exact_idx is not None else None
    # Approximate placement = centroid of the neighbours' PRECOMPUTED map coords (honest;
    # never a claimed exact 2-D projection of the query).
    approx_xy = (
        {"x": exact_match.x, "y": exact_match.y}
        if exact_match is not None
        else {
            "x": float(np.mean([n.x for n in neighbors])),
            "y": float(np.mean([n.y for n in neighbors])),
        }
    )
    # DEFINED domain metric: the query's distance to its k-th nearest training neighbour,
    # read against the training reference distribution. No in-/out-of-domain boolean.
    query_kth = float(distances[distinct_order[k - 1]])
    reference = domain.get("training_kth_nn_distances", [])
    percentile = bisect.bisect_right(reference, query_kth) / len(reference) if reference else 0.0

    return ExploreLocateResponse(
        context=body.context,
        placement="exact_existing_reference" if exact_match else "approximate_nearest_neighbor",
        approx_xy=approx_xy,
        neighbors=neighbors,
        exact_match=exact_match,
        domain=ExploreDomain(
            metric=domain["metric"],
            k=k,
            query_kth_distance=query_kth,
            training_reference_quantiles=domain.get("quantiles", {}),
            percentile=percentile,
        ),
    )


@router.get(
    "/explore/{context}/signatures/{compound_id}",
    response_model=ExploreReferenceSignature,
)
def explore_reference_signature(
    context: str,
    compound_id: str,
    repo_root: Path = Depends(get_repo_root),
) -> ExploreReferenceSignature:
    """Retrieve one exact support row; never infer a vector from its 2-D map coordinates."""
    umap_doc, manifest, row, row_index = load_reference_row(repo_root, context, compound_id)
    feature_names = [str(value) for value in manifest["feature_names"]]
    signature = {
        gene: float(value)
        for gene, value in zip(feature_names, row.tolist(), strict=True)
    }
    point = next(
        (item for item in umap_doc.get("points", []) if item.get("compound_id") == compound_id),
        {},
    )
    identity = identity_index(repo_root).get(compound_id)
    aggregation = manifest.get("aggregation", {})
    return ExploreReferenceSignature(
        context=context,
        compound_id=compound_id,
        preferred_name=identity.preferred_name if identity else None,
        pubchem_cid=identity.pubchem_cid if identity else None,
        endpoint_label=point.get("label"),
        underlying_condition_available=False,
        underlying_conditions=[],
        reference_comparison=(
            "Calculated during LINCS processing using the corresponding experimental controls. "
            "EndoScan receives the resulting differential signature and does not choose "
            "the control."
        ),
        cell_models=[
            "MCF7 — human breast cancer cell line",
            "A549 — human lung adenocarcinoma cell line",
        ],
        aggregation_description=(
            "This profile combines the measured experimental conditions selected for this "
            "compound when the reference map was built."
        ),
        aggregate_pathway_warning=(
            "This reference profile combines responses from multiple cell models. Opposing "
            "cell-specific changes may be attenuated in the aggregate."
        ),
        feature_names=feature_names,
        signature=signature,
        n_genes=len(feature_names),
        support_row_index=row_index,
        support_sha256=str(manifest["support_sha256"]),
        source_key=str(manifest.get("source", {}).get("key", "")),
        source_sha256=str(manifest.get("source", {}).get("sha256", "")),
        provenance={
            "point_definition": manifest.get("point_definition"),
            "aggregation": aggregation,
            "support_row_contract": (
                "manifest.compound_ids[row] aligned to support.npy[row] and "
                "manifest.feature_names[column]"
            ),
            "vector_origin": "original support matrix; never reconstructed from UMAP coordinates",
        },
    )
