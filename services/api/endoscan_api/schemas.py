"""HTTP request/response schemas for the serving API.

Response models for ``/predict`` and ``/explain`` are the core models REUSED VERBATIM
(``PredictionResult``, ``ExplanationResult`` — each already carries the required
``LimitationsBlock``), so the served contract can never drift from the science library.
The list/detail models below are assembled in the API layer (see ``assemblers.py``) from
existing registry + ``metrics.json`` + ``model_card.md`` data — the registry schema is
NOT changed.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Re-exported so routes can reference a single response contract (no duplication).
from endoscan_core.inference import (
    ExplanationResult,
    LimitationsBlock,
    PredictionResult,
)

from .limits import MAX_ENDPOINT_SELECTION, MAX_GENES

__all__ = [
    "AnalyzeEndpointResult",
    "AnalyzeRequest",
    "AnalyzeResponse",
    "AnalyzeSummary",
    "ContextBlock",
    "ContextVariant",
    "EndpointDetail",
    "EndpointCompatibility",
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
    "PathwayCard",
    "PathwayMethodBlock",
    "PathwaysRequest",
    "PathwaysResponse",
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
        min_length=1,
        max_length=MAX_GENES,
        description="Gene SYMBOL -> value; requirements come from the selected endpoint schema.",
    )
    allow_extra: bool = Field(
        False, description="Drop genes not in the schema instead of rejecting them."
    )


class ExplainRequest(PredictRequest):
    """Same input as predict, plus how many top contributors to return."""

    top_n: int = Field(10, ge=1, le=MAX_GENES, description="Number of top contributors.")


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
    request_id: str


# --- signature upload / parse --------------------------------------------------------


class ParsePreview(BaseModel):
    """Format-level facts from parsing once, before endpoint-specific compatibility."""

    model_config = ConfigDict(extra="forbid")

    n_detected: int  # genes found in the upload
    samples: list[str] | None = None  # multi-column CSV: the sample column names
    selected_sample: str | None = None
    needs_sample: bool = False  # multi-column + no sample chosen -> the UI must pick one


class EndpointCompatibility(BaseModel):
    """Compatibility of one parsed signature with one registered endpoint schema."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: str
    biological_target: str
    compatible: bool
    n_schema_genes: int
    n_detected: int
    n_matched: int
    n_missing: int
    n_extra: int
    missing_genes: list[str]
    extra_genes: list[str]
    reason: str | None = None


class ParseResult(BaseModel):
    """Parse once, then report compatibility against every registered endpoint."""

    model_config = ConfigDict(extra="forbid")

    ready: bool
    format: str
    preview: ParsePreview
    compatibility: list[EndpointCompatibility]
    compatible_endpoint_ids: list[str]
    # Parsed mapping, unchanged, so each endpoint can align it to its own feature order.
    signature: dict[str, float] | None = None


# --- analyze across all endpoints ----------------------------------------------------


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signature: dict[str, float] = Field(
        ...,
        min_length=1,
        max_length=MAX_GENES,
        description="A transcriptomic signature (gene->value).",
    )
    allow_extra: bool = Field(False, description="Drop genes not in an endpoint's schema.")
    endpoint_ids: list[str] | None = Field(
        None,
        min_length=1,
        max_length=MAX_ENDPOINT_SELECTION,
        description="Registered endpoints to run. Omit to run all registered endpoints.",
    )

    @field_validator("endpoint_ids")
    @classmethod
    def _unique_endpoint_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("endpoint_ids must not contain duplicates")
        return value


class AnalyzeEndpointResult(BaseModel):
    """One endpoint's outcome in a fan-out. `result` on success, `error` on failure — a failing
    endpoint does NOT sink the others (per-endpoint isolation)."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: str
    biological_target: str
    ok: bool
    result: PredictionResult | None = None
    error: ErrorResponse | None = None


class AnalyzeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested: int
    succeeded: int
    failed: int
    status: str  # ok | partial | all_failed


class AnalyzeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[AnalyzeEndpointResult]
    summary: AnalyzeSummary


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
        ...,
        min_length=1,
        max_length=MAX_GENES,
        description="A transcriptomic signature (gene->value).",
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


# --- Biological pathways (Reactome over-representation for an /explain result) --------
#
# Reports which Reactome pathways are over-represented among the genes that drove THIS MODEL
# toward an active call. Field names carry the honest framing: the genes INFLUENCED THIS RESULT
# (model-contributing) — they are NOT "affected"/"perturbed" genes, and a pathway appearing is a
# clue for investigation, NOT proof the compound activates it.


class PathwaysRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: str = Field(..., description="Registered endpoint id, e.g. 'ER' or 'AR'.")
    signature: dict[str, float] = Field(
        ...,
        min_length=1,
        max_length=MAX_GENES,
        description="A transcriptomic signature (gene->value).",
    )
    allow_extra: bool = Field(False, description="Drop genes not in the endpoint's schema.")


class PathwayCard(BaseModel):
    """One over-represented pathway. ``genes_influencing_result`` is the ACTUAL overlap from this
    result (toward-signal genes ∩ pathway ∩ universe) — never the pathway's full membership."""

    model_config = ConfigDict(extra="forbid")

    pathway_id: str
    name: str
    description: str | None = None  # Reactome's own text only — never generated
    genes_influencing_result: list[str]
    overlap_count: int
    pathway_size_in_universe: int
    input_size_in_universe: int
    p_value: float
    q_value: float
    evidence: str  # High | Medium | Low (transparently derived from q_value + overlap_count)


class PathwayMethodBlock(BaseModel):
    """Everything a reader needs to verify the labels — surfaced in the UI's Technical details."""

    model_config = ConfigDict(extra="allow")

    input_gene_rule: str
    pinned_top_n: int
    n_toward_genes: int
    n_input_genes: int  # toward genes intersected with the universe
    universe_size: int
    min_pathway_overlap: int
    test: str
    correction: str
    family_size: int
    evidence_mapping: dict[str, str]
    reactome: dict | None = None  # version / source_url / retrieval_date / license / counts


class PathwaysResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: str
    method: str  # the /explain attribution method used (tree_shap | linear_coefficient)
    #: "ok" (ran; pathways may be empty if none cleared the threshold), "unavailable" (no
    #: Reactome artifact), or "too_few_genes" (toward-set below the minimum to test).
    status: str
    reason: str | None = None
    pathways: list[PathwayCard]
    method_block: PathwayMethodBlock | None = None
