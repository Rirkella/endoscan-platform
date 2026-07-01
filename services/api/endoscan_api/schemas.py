"""HTTP request/response schemas for the serving API.

Response models for ``/predict`` and ``/explain`` are the core models REUSED VERBATIM
(``PredictionResult``, ``ExplanationResult`` — each already carries the required
``LimitationsBlock``), so the served contract can never drift from the science library.
The list/detail models below are assembled in the API layer (see ``assemblers.py``) from
existing registry + ``metrics.json`` + ``model_card.md`` data — the registry schema is
NOT changed.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Re-exported so routes can reference a single response contract (no duplication).
from endoscan_core.inference import (
    ExplanationResult,
    LimitationsBlock,
    PredictionResult,
)

__all__ = [
    "AnalyzeEndpointResult",
    "AnalyzeRequest",
    "AnalyzeResponse",
    "ContextBlock",
    "ContextVariant",
    "EndpointDetail",
    "EndpointSummary",
    "ErrorResponse",
    "ExplainRequest",
    "ExplanationResult",
    "HealthResponse",
    "LimitationsBlock",
    "MetricsSummary",
    "ParsePreview",
    "ParseResult",
    "PredictRequest",
    "PredictionResult",
]


# --- requests ------------------------------------------------------------------------


class PredictRequest(BaseModel):
    """One transcriptomic signature to score against a registered endpoint."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: str = Field(..., description="Registered endpoint id, e.g. 'ER' or 'AR'.")
    signature: dict[str, float] = Field(
        ...,
        description="Landmark-gene SYMBOL -> value (the 978-gene signature for this endpoint).",
    )
    allow_extra: bool = Field(
        False, description="Drop genes not in the schema instead of rejecting them."
    )


class ExplainRequest(PredictRequest):
    """Same input as predict, plus how many top contributors to return."""

    top_n: int = Field(10, ge=1, le=978, description="Number of top signed gene contributors.")


# --- responses -----------------------------------------------------------------------


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    endpoints_loaded: list[str]
    explain_available: bool


class EndpointSummary(BaseModel):
    """List-view row — honest status surfaced without a detail call."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: str
    biological_target: str
    status: str
    input_type: str
    frozen: bool


class MetricsSummary(BaseModel):
    """Point metrics + the committed uncertainty (CI) and evidence blocks (read, not recomputed)."""

    model_config = ConfigDict(extra="allow")  # tolerate metrics.json gaining fields later

    auroc: float | None = None
    auprc: float | None = None
    balanced_accuracy: float | None = None
    brier_score: float | None = None
    uncertainty: dict | None = None
    evidence: dict | None = None


class ContextBlock(BaseModel):
    """The biological context of a variant.

    ``cell_lines`` is null today: there is NO committed structured cell-line source
    (slice_manifest is a jobs-side Storage artifact, not in git; metrics.json has no
    cell_lines field). It is populated later when structured context is added — we do
    NOT parse card/scope prose. ``scope`` carries the verbatim structured
    ``metrics.json['claim_scope']`` so the context still reaches consumers.
    """

    model_config = ConfigDict(extra="forbid")

    cell_lines: list[str] | None = None
    label_sources: list[str]
    scope: str | None


class ContextVariant(BaseModel):
    """One context-variant of an endpoint. Endpoints expose a LIST of these (see below)."""

    model_config = ConfigDict(extra="forbid")

    variant_id: str
    context: ContextBlock
    status: str
    limitations: LimitationsBlock
    metrics_summary: MetricsSummary
    model_card_markdown: str


class EndpointDetail(BaseModel):
    """Detail view. ``variants`` is a LIST — length 1 today (ER: MCF7/A549; AR: VCaP),
    additive later (a second context-variant is an append, not a breaking change). No
    field assumes a single variant."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: str
    biological_target: str
    input_type: str
    version: str
    status: str
    frozen: bool
    source_refs: list[str]
    variants: list[ContextVariant]


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: str
    detail: str
    endpoint_id: str | None = None


# --- signature upload / parse --------------------------------------------------------


class ParsePreview(BaseModel):
    """Real (never fabricated) summary of how the uploaded signature aligns to the schema."""

    model_config = ConfigDict(extra="forbid")

    n_detected: int  # genes found in the upload
    n_matched: int  # of the schema's genes, how many the upload supplied
    n_missing: int
    n_extra: int
    missing_genes: list[str]  # truncated
    extra_genes: list[str]  # truncated
    samples: list[str] | None = None  # multi-column CSV: the sample column names
    selected_sample: str | None = None
    needs_sample: bool = False  # multi-column + no sample chosen -> the UI must pick one


class ParseResult(BaseModel):
    """Result of parsing + validating an uploaded signature against an endpoint's schema."""

    model_config = ConfigDict(extra="forbid")

    aligned: bool
    format: str
    schema_endpoint_id: str  # which endpoint's schema it was validated against
    n_schema_genes: int
    preview: ParsePreview
    # The aligned {gene: value} in schema order, ready to POST to /analyze. None when the upload
    # needs a sample choice first (multi-column CSV) — never a fabricated signature.
    signature: dict[str, float] | None = None


# --- analyze across all endpoints ----------------------------------------------------


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signature: dict[str, float] = Field(
        ..., description="A transcriptomic signature (gene->value)."
    )
    allow_extra: bool = Field(False, description="Drop genes not in an endpoint's schema.")


class AnalyzeEndpointResult(BaseModel):
    """One endpoint's outcome in a fan-out. `result` on success, `error` on failure — a failing
    endpoint does NOT sink the others (per-endpoint isolation)."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: str
    biological_target: str
    ok: bool
    result: PredictionResult | None = None
    error: ErrorResponse | None = None


class AnalyzeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[AnalyzeEndpointResult]
