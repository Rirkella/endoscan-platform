"""POST /signatures/parse — parse + validate an uploaded signature, return an honest preview.

Thin: parse bytes -> {gene: value} (parsing.py), then validate with the EXISTING authoritative
validator ``align_signature`` against the resolved endpoint's schema. Stateless — the uploaded
bytes are parsed in-request and discarded; nothing is persisted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile

from endoscan_core.inference import align_signature, load_feature_schema
from endoscan_core.registry import get_endpoint, list_endpoints

from ..deps import get_repo_root
from ..parsing import MalformedUploadError, parse_upload
from ..schemas import ParsePreview, ParseResult

router = APIRouter(prefix="/signatures", tags=["signatures"])

TRUNC = 10


def _resolve_schema_endpoint(endpoint_id: str | None, repo_root: Path):
    """The endpoint whose schema we validate against — the given one, else the first registered.

    ER and AR share the identical 978-gene landmark schema today; the endpoint_id param
    future-proofs schema divergence (validate against exactly that endpoint).
    """
    if endpoint_id:
        return get_endpoint(endpoint_id, repo_root=repo_root)  # 404 handler if unknown
    endpoints = list_endpoints(repo_root=repo_root)
    if not endpoints:
        raise MalformedUploadError("no registered endpoints to validate against.")
    return endpoints[0]


@router.post("/parse", response_model=ParseResult)
def parse_signature(
    repo_root: Path = Depends(get_repo_root),
    file: Annotated[UploadFile | None, File()] = None,
    content: Annotated[str | None, Form()] = None,
    format: Annotated[str, Form()] = "json",
    allow_extra: Annotated[bool, Form()] = False,
    endpoint_id: Annotated[str | None, Form()] = None,
    sample: Annotated[str | None, Form()] = None,
) -> ParseResult:
    """Parse an uploaded signature and preview its alignment.

    Errors: 400 malformed_upload (unparseable) / 422 invalid_signature (parsed but fails
    align_signature) / 404 unknown endpoint.
    """
    if file is not None:
        text = file.file.read().decode("utf-8", errors="replace")
    elif content is not None:
        text = content
    else:
        raise MalformedUploadError("no file or content provided.")

    entry = _resolve_schema_endpoint(endpoint_id, repo_root)
    schema = load_feature_schema(repo_root / entry.feature_schema_path)
    schema_genes = list(schema.features)

    parsed = parse_upload(format, text, sample=sample)  # MalformedUploadError -> 400

    # Multi-column CSV with no chosen sample: report the sample names; do not guess a column.
    if parsed.mapping is None:
        return ParseResult(
            aligned=False,
            format=format,
            schema_endpoint_id=entry.endpoint_id,
            n_schema_genes=len(schema_genes),
            preview=ParsePreview(
                n_detected=0,
                n_matched=0,
                n_missing=len(schema_genes),
                n_extra=0,
                missing_genes=[],
                extra_genes=[],
                samples=parsed.samples,
                needs_sample=True,
            ),
            signature=None,
        )

    mapping = parsed.mapping
    # THE authoritative validation (same code /predict uses). SignatureValidationError -> 422.
    align_signature(mapping, schema, allow_extra=allow_extra)

    # Aligned OK: every schema gene is present. Build the signature in schema order.
    aligned = {gene: mapping[gene] for gene in schema_genes}
    extra = sorted(set(mapping) - set(schema_genes))
    return ParseResult(
        aligned=True,
        format=format,
        schema_endpoint_id=entry.endpoint_id,
        n_schema_genes=len(schema_genes),
        preview=ParsePreview(
            n_detected=len(mapping),
            n_matched=len(schema_genes),
            n_missing=0,
            n_extra=len(extra),
            missing_genes=[],
            extra_genes=extra[:TRUNC],
            samples=parsed.samples,
            selected_sample=sample,
            needs_sample=False,
        ),
        signature=aligned,
    )
