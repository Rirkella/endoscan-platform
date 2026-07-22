"""POST /interpret/pathways — biological-pathway over-representation for a prediction.

Thin: reuse the tested ``explain`` (pinned depth), select the toward-signal contributors, and run
STANDARD ORA (scipy hypergeometric tail + local BH) against the committed Reactome gene sets on
the model's OWN 978-landmark universe. No science added to ``endoscan_core``; no pathway is ever
fabricated. Honest states:
  * no Reactome artifact         -> 200 status="unavailable" (empty; never fabricate cards)
  * toward-set below the minimum -> 200 status="too_few_genes" (no q-values on a handful of genes)
  * ran but nothing cleared q<0.10 -> 200 status="ok" with an empty pathway list (a real result)
Errors reuse the /explain contract: unsupported model -> 501, missing shap -> 503, bad signature
-> 422, unknown endpoint -> 404 (global handlers).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from endoscan_core.inference import (
    ExplanationResult,
    UnsupportedModelForExplanationError,
    explain,
    load_feature_schema,
)
from endoscan_core.registry import get_endpoint

from ..biological_response import (
    MAX_GENE_SET_SIZE,
    MIN_GENE_SET_SIZE,
    SUPPORTED_DIFFERENTIAL_TYPES,
    run_preranked_enrichment,
)
from ..deps import get_repo_root
from ..errors import error_payload
from ..literature import (
    LiteratureRateLimitError,
    LiteratureService,
    LiteratureTimeoutError,
    LiteratureUnavailableError,
    get_literature_service,
)
from ..literature_concepts import load_endpoint_concept
from ..pathways import (
    EVIDENCE_MAPPING,
    MIN_PATHWAY_OVERLAP,
    MIN_TOWARD_GENES,
    PINNED_TOP_N,
    run_enrichment,
    select_toward_genes,
)
from ..reactome_store import ReactomeUnavailableError, load_reactome
from ..schemas import (
    BiologicalPathwayCard,
    BiologicalResponseMethodBlock,
    BiologicalResponseRequest,
    BiologicalResponseResponse,
    LiteratureRequest,
    LiteratureResponse,
    PathwayCard,
    PathwayMethodBlock,
    PathwaysRequest,
    PathwaysResponse,
)

router = APIRouter(prefix="/interpret", tags=["interpret"])

_INPUT_RULE = (
    f"toward-signal contributors: all direction='toward' genes among the pinned top "
    f"{PINNED_TOP_N} /explain contributors (attribution depth fixed, not the caller's top_n)"
)
_TEST = "Fisher exact, one-sided (over-representation)"
_CORRECTION = "Benjamini-Hochberg FDR (across the tested pathway family)"


@router.post("/biological-response", response_model=BiologicalResponseResponse)
def interpret_biological_response(
    body: BiologicalResponseRequest, repo_root: Path = Depends(get_repo_root)
) -> BiologicalResponseResponse:
    """Analyze the complete signed response independently of any endpoint model."""
    if body.input_value_type not in SUPPORTED_DIFFERENTIAL_TYPES:
        return BiologicalResponseResponse(
            status="unsupported_input",
            reason=(
                "Biological-response pathway analysis requires a differential or ranked "
                "signature. Raw expression values without a matched reference cannot be "
                "interpreted as increased or decreased pathway response."
            ),
            input_value_type=body.input_value_type,
            increased_pathways=[],
            decreased_pathways=[],
            tested_gene_universe=[],
        )
    try:
        reactome = load_reactome(repo_root)
    except ReactomeUnavailableError:
        return BiologicalResponseResponse(
            status="unavailable",
            reason="Reactome pathway data is not available for this deployment.",
            input_value_type=body.input_value_type,
            increased_pathways=[],
            decreased_pathways=[],
            tested_gene_universe=[],
        )

    outcome = run_preranked_enrichment(body.signature, reactome.pathways)
    method = BiologicalResponseMethodBlock(
        method="Competitive preranked Wilcoxon rank-sum enrichment",
        method_version="endoscan-preranked-wilcoxon-1.0.0",
        ranking_statistic=(
            "supplied signed transcriptomic value, ranked ascending with average ties"
        ),
        input_value_type=body.input_value_type,
        universe_size=len(outcome.universe),
        pathways_tested=outcome.pathways_tested,
        correction="Benjamini-Hochberg FDR across all tested Reactome pathways",
        min_gene_set_size=MIN_GENE_SET_SIZE,
        max_gene_set_size=MAX_GENE_SET_SIZE,
        leading_edge_rule=(
            "up to 10 pathway genes from the enriched signed tail; falls back to the extreme "
            "ranked pathway genes when the tail contains no non-zero value"
        ),
        reactome=dict(reactome.provenance),
    )

    def card(item) -> BiologicalPathwayCard:
        return BiologicalPathwayCard(**item.__dict__)

    increased = [card(item) for item in outcome.increased]
    decreased = [card(item) for item in outcome.decreased]
    return BiologicalResponseResponse(
        status="ok" if increased or decreased else "empty",
        reason=(
            None
            if increased or decreased
            else "No Reactome gene set met the configured size bounds."
        ),
        input_value_type=body.input_value_type,
        increased_pathways=increased,
        decreased_pathways=decreased,
        tested_gene_universe=outcome.universe,
        method_block=method,
    )


@router.post("/literature", response_model=LiteratureResponse)
def interpret_literature(
    body: LiteratureRequest,
    repo_root: Path = Depends(get_repo_root),
    service: LiteratureService = Depends(get_literature_service),
) -> LiteratureResponse:
    """Retrieve transparent supporting PubMed records; never make causal claims."""
    entry = get_endpoint(body.endpoint_id, repo_root=repo_root)
    endpoint_name = entry.biological_target
    concept = load_endpoint_concept(repo_root.resolve(), body.endpoint_id, endpoint_name)
    try:
        return service.lookup(body, endpoint_name, concept)
    except LiteratureRateLimitError:
        return service.failure_response(
            body,
            endpoint_name,
            status="rate_limited",
            reason="PubMed's request limit was reached. Please try again later.",
            concept=concept,
        )
    except LiteratureTimeoutError:
        return service.failure_response(
            body,
            endpoint_name,
            status="timeout",
            reason="PubMed did not respond before the request deadline.",
            concept=concept,
        )
    except LiteratureUnavailableError as exc:
        reason = (
            "PubMed integration is not configured."
            if "NCBI_EMAIL" in str(exc)
            else "PubMed is temporarily unavailable."
        )
        return service.failure_response(
            body, endpoint_name, status="unavailable", reason=reason, concept=concept
        )


@router.post("/pathways", response_model=PathwaysResponse)
def interpret_pathways(
    body: PathwaysRequest, request: Request, repo_root: Path = Depends(get_repo_root)
) -> PathwaysResponse | JSONResponse:
    # 1) Reactome artifact — absent => honest empty state, never fabricated pathways.
    try:
        reactome = load_reactome(repo_root)
    except ReactomeUnavailableError:
        return PathwaysResponse(
            endpoint_id=body.endpoint_id,
            method="",
            status="unavailable",
            reason="pathway data not available",
            pathways=[],
        )

    # 2) The tested explanation at PINNED depth (science must not depend on a UI knob).
    capability = request.app.state.explanation_capabilities.get(body.endpoint_id)
    if capability is not None and not capability["available"]:
        return JSONResponse(
            status_code=503,
            content=error_payload(
                request,
                error="explain_unavailable",
                detail=capability["reason"] or "Explanation capability is unavailable.",
                endpoint_id=body.endpoint_id,
            ),
        )
    try:
        result = explain(
            body.endpoint_id,
            body.signature,
            top_n=PINNED_TOP_N,
            repo_root=repo_root,
            allow_extra=body.allow_extra,
        )
    except UnsupportedModelForExplanationError:
        return JSONResponse(
            status_code=501,
            content=error_payload(
                request,
                error="explain_unsupported_for_model",
                detail="Explanations are not available for this model type.",
                endpoint_id=body.endpoint_id,
            ),
        )
    except ImportError:  # shap extra absent for a tree endpoint
        return JSONResponse(
            status_code=503,
            content=error_payload(
                request,
                error="explain_unavailable",
                detail="Explanation service is temporarily unavailable.",
                endpoint_id=body.endpoint_id,
            ),
        )
    explanation = result if isinstance(result, ExplanationResult) else result[0]
    toward = select_toward_genes(explanation.top_contributors)

    reactome_block = dict(reactome.provenance)

    def _method_block(n_input: int, universe_size: int, family_size: int) -> PathwayMethodBlock:
        return PathwayMethodBlock(
            input_gene_rule=_INPUT_RULE,
            pinned_top_n=PINNED_TOP_N,
            n_toward_genes=len(toward),
            n_input_genes=n_input,
            universe_size=universe_size,
            min_pathway_overlap=MIN_PATHWAY_OVERLAP,
            test=_TEST,
            correction=_CORRECTION,
            family_size=family_size,
            evidence_mapping=EVIDENCE_MAPPING,
            reactome=reactome_block,
        )

    # 3) Too few contributing genes -> honest refusal (no enrichment on a handful of genes).
    if len(toward) < MIN_TOWARD_GENES:
        return PathwaysResponse(
            endpoint_id=explanation.endpoint_id,
            method=explanation.method,
            status="too_few_genes",
            reason=(
                f"too few contributing genes for reliable pathway analysis "
                f"({len(toward)} toward-signal genes; need >= {MIN_TOWARD_GENES})"
            ),
            pathways=[],
            method_block=_method_block(0, 0, 0),
        )

    # 4) ORA on the landmark ∩ Reactome universe (never the genome).
    entry = get_endpoint(body.endpoint_id, repo_root=repo_root)
    schema = load_feature_schema(repo_root / entry.feature_schema_path)
    outcome = run_enrichment(toward, list(schema.features), reactome.pathways)

    cards = [
        PathwayCard(
            pathway_id=r.pathway_id,
            name=r.name,
            description=r.description,
            genes_influencing_result=r.genes_influencing_result,
            overlap_count=r.overlap_count,
            pathway_size_in_universe=r.pathway_size_in_universe,
            input_size_in_universe=r.input_size_in_universe,
            p_value=r.p_value,
            q_value=r.q_value,
            evidence=r.evidence,
        )
        for r in outcome.results
    ]
    exploratory_cards = [
        PathwayCard(
            pathway_id=r.pathway_id,
            name=r.name,
            description=r.description,
            genes_influencing_result=r.genes_influencing_result,
            overlap_count=r.overlap_count,
            pathway_size_in_universe=r.pathway_size_in_universe,
            input_size_in_universe=r.input_size_in_universe,
            p_value=r.p_value,
            q_value=r.q_value,
            evidence=r.evidence,
        )
        for r in outcome.exploratory
    ]
    return PathwaysResponse(
        endpoint_id=explanation.endpoint_id,
        method=explanation.method,
        status="ok",
        reason=None if cards else "no pathways met the evidence threshold for this result",
        pathways=cards,
        exploratory_pathways=exploratory_cards,
        method_block=_method_block(
            outcome.n_input_genes, outcome.universe_size, outcome.family_size
        ),
    )
