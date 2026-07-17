"""POST /signatures/parse — parse once, then check every registered endpoint schema."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile

from endoscan_core.inference import SignatureValidationError, align_signature, load_feature_schema
from endoscan_core.registry import list_endpoints

from ..deps import get_repo_root
from ..limits import MAX_UPLOAD_BYTES
from ..parsing import MalformedUploadError, parse_upload
from ..schemas import EndpointCompatibility, ParsePreview, ParseResult

router = APIRouter(prefix="/signatures", tags=["signatures"])

TRUNC = 10


def _read_text(file: UploadFile | None, content: str | None) -> str:
    if file is not None:
        raw = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise MalformedUploadError(
                f"uploaded file exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit."
            )
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MalformedUploadError("uploaded file must be valid UTF-8 text.") from exc
    if content is not None:
        if len(content.encode("utf-8")) > MAX_UPLOAD_BYTES:
            raise MalformedUploadError(
                f"uploaded content exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit."
            )
        return content
    raise MalformedUploadError("no file or content provided.")


def _compatibility(mapping: dict[str, float], entry, repo_root: Path, allow_extra: bool):
    schema = load_feature_schema(repo_root / entry.feature_schema_path)
    schema_genes = list(schema.features)
    supplied = set(mapping)
    required = set(schema_genes)
    missing = sorted(required - supplied)
    extra = sorted(supplied - required)
    reason: str | None = None
    compatible = True
    try:
        align_signature(mapping, schema, allow_extra=allow_extra)
    except SignatureValidationError as exc:
        compatible = False
        reason = str(exc)
    return EndpointCompatibility(
        endpoint_id=entry.endpoint_id,
        biological_target=entry.biological_target,
        compatible=compatible,
        n_schema_genes=len(schema_genes),
        n_detected=len(mapping),
        n_matched=len(required & supplied),
        n_missing=len(missing),
        n_extra=len(extra),
        missing_genes=missing[:TRUNC],
        extra_genes=extra[:TRUNC],
        reason=reason,
    )


@router.post("/parse", response_model=ParseResult)
def parse_signature(
    repo_root: Path = Depends(get_repo_root),
    file: Annotated[UploadFile | None, File()] = None,
    content: Annotated[str | None, Form()] = None,
    format: Annotated[str, Form()] = "json",
    allow_extra: Annotated[bool, Form()] = False,
    sample: Annotated[str | None, Form()] = None,
    input_value_type: Annotated[
        str,
        Form(pattern="^(differential_zscore|log2_fold_change|ranked_statistic|raw_expression)$"),
    ] = "ranked_statistic",
) -> ParseResult:
    """Parse one upload and report compatibility with every registered endpoint.

    The returned signature is the parsed mapping, not one endpoint's aligned vector. The normal
    ``/analyze`` route aligns it independently for the explicitly selected compatible endpoints.
    """
    text = _read_text(file, content)
    parsed = parse_upload(format, text, sample=sample)

    if parsed.mapping is None:
        return ParseResult(
            ready=False,
            format=format,
            input_value_type=input_value_type,
            preview=ParsePreview(
                n_detected=0,
                samples=parsed.samples,
                selected_sample=None,
                needs_sample=True,
            ),
            compatibility=[],
            compatible_endpoint_ids=[],
            signature=None,
        )

    endpoints = list_endpoints(repo_root=repo_root)
    if not endpoints:
        raise MalformedUploadError("no registered endpoints to validate against.")
    compatibility = [
        _compatibility(parsed.mapping, entry, repo_root, allow_extra) for entry in endpoints
    ]
    compatible_ids = [item.endpoint_id for item in compatibility if item.compatible]
    return ParseResult(
        ready=bool(compatible_ids),
        format=format,
        input_value_type=input_value_type,
        preview=ParsePreview(
            n_detected=len(parsed.mapping),
            samples=parsed.samples,
            selected_sample=sample,
            needs_sample=False,
        ),
        compatibility=compatibility,
        compatible_endpoint_ids=compatible_ids,
        signature=parsed.mapping,
    )
