"""Read-only, versioned routes for verified compounds with measured signatures."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query

from ..catalogue_store import get_signature, search_compounds
from ..deps import get_repo_root
from ..schemas import CatalogueSearchResponse, CatalogueSignatureDetail

router = APIRouter(prefix="/catalogue/v1", tags=["catalogue"])


@router.get("/compounds", response_model=CatalogueSearchResponse)
def catalogue_search(
    query: str = Query(min_length=2, max_length=200, pattern=r"^(?:.*\S){2}.*$"),
    limit: int = Query(default=10, ge=1, le=20),
    repo_root: Path = Depends(get_repo_root),
) -> CatalogueSearchResponse:
    """Search only compounds that have a committed, measured public signature."""
    normalized_query = query.strip()
    doc, results = search_compounds(repo_root, normalized_query, limit)
    return CatalogueSearchResponse(
        schema_version=doc.schema_version,
        catalogue_version=doc.catalogue_version,
        query=normalized_query,
        sources=doc.sources,
        results=results,
    )


@router.get("/signatures/{signature_id}", response_model=CatalogueSignatureDetail)
def catalogue_signature(
    signature_id: str, repo_root: Path = Depends(get_repo_root)
) -> CatalogueSignatureDetail:
    """Return the exact measured signature selected from a search result."""
    return get_signature(repo_root, signature_id)
