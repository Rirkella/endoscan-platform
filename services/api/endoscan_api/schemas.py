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
    "ExploreCounts",
    "ExploreDomain",
    "ExploreLocateRequest",
    "ExploreLocateResponse",
    "ExploreManifestSummary",
    "ExploreMapResponse",
    "ExploreNeighbor",
    "ExplorePoint",
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


# --- Explore: the data-space (UMAP) view ---------------------------------------------
#
# UMAP is a VISUALIZATION of how the real training signatures relate — not a model
# boundary and not proof of anything. A submitted signature is NOT projected with
# ``UMAP.transform``; it is placed APPROXIMATELY at the centroid of its nearest training
# neighbours (found in the original 978-gene space). There is NO in-/out-of-domain flag:
# the honest domain signal is a DEFINED, computed metric (distance to the k-th training
# neighbour) shown against the training reference distribution — the human judges.


class ExplorePoint(BaseModel):
    """One real training compound's position on the 2-D map. ``label`` is null when uncoloured."""

    model_config = ConfigDict(extra="forbid")

    compound_id: str
    x: float
    y: float
    label: str | None = None  # "active" | "inactive" | null (never fabricated)


class ExploreCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n_total: int
    n_active: int
    n_inactive: int
    n_unlabeled: int


class ExploreManifestSummary(BaseModel):
    """The map's provenance surfaced to the client (params + seed + source hash + built_at)."""

    model_config = ConfigDict(extra="allow")  # tolerate manifest gaining fields later

    target: str
    n_compounds: int
    umap: dict
    domain_metric_k: int
    label_status: str
    source_sha256: str
    built_at: str | None = None


class ExploreMapResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    points: list[ExplorePoint]
    counts: ExploreCounts
    manifest: ExploreManifestSummary


class ExploreLocateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str = Field(..., description="Endpoint id whose map to place against, e.g. 'ER'.")
    signature: dict[str, float] = Field(
        ..., description="A transcriptomic signature (gene->value)."
    )
    allow_extra: bool = Field(False, description="Drop genes not in the map's feature set.")


class ExploreNeighbor(BaseModel):
    """A real nearest training compound (distance in the ORIGINAL gene space, + its map coords)."""

    model_config = ConfigDict(extra="forbid")

    compound_id: str
    distance: float
    x: float
    y: float
    label: str | None = None


class ExploreDomain(BaseModel):
    """The DEFINED, computed domain signal — NOT an asserted in-/out-of-domain verdict.

    ``query_kth_distance`` is the submitted signature's distance to its k-th nearest TRAINING
    neighbour; ``training_reference_quantiles`` is that same metric across the training set;
    ``percentile`` is the fraction of training points whose own k-th-neighbour distance is <=
    the query's. The human reads these numbers — the API asserts no membership.
    """

    model_config = ConfigDict(extra="forbid")

    metric: str
    k: int
    query_kth_distance: float
    training_reference_quantiles: dict[str, float]
    percentile: float


class ExploreLocateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    placement: str  # always "approximate_nearest_neighbor" — never an exact projection
    approx_xy: dict[str, float]  # centroid of the neighbours' precomputed map coords
    neighbors: list[ExploreNeighbor]
    domain: ExploreDomain
