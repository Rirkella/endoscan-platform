"""POST /interpret/pathways — biological-pathway over-representation for a prediction.

Thin: reuse the tested ``explain`` (pinned depth), select the toward-signal contributors, and run
STANDARD ORA (scipy hypergeometric tail + local BH) against the committed Reactome gene sets on
the model's OWN 978-landmark universe. No science added to ``endoscan_core``; no pathway is ever
fabricated. Honest states:
  * no Reactome artifact         -> 200 status="unavailable"  (graceful empty; never fake cards)
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

from ..deps import get_repo_root
from ..errors import error_payload
from ..literature import (
    LiteratureRateLimitError,
    LiteratureService,
    LiteratureTimeoutError,
    LiteratureUnavailableError,
    get_literature_service,
)
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


@router.post("/literature", response_model=LiteratureResponse)
def interpret_literature(
    body: LiteratureRequest,
    repo_root: Path = Depends(get_repo_root),
    service: LiteratureService = Depends(get_literature_service),
) -> LiteratureResponse:
    """Retrieve transparent supporting PubMed records; never make causal claims."""
    entry = get_endpoint(body.endpoint_id, repo_root=repo_root)
    endpoint_name = entry.biological_target
    try:
        return service.lookup(body, endpoint_name)
    except LiteratureRateLimitError:
        return service.failure_response(
            body,
            endpoint_name,
            status="rate_limited",
            reason="PubMed's request limit was reached. Please try again later.",
        )
    except LiteratureTimeoutError:
        return service.failure_response(
            body,
            endpoint_name,
            status="timeout",
            reason="PubMed did not respond before the request deadline.",
        )
    except LiteratureUnavailableError as exc:
        reason = (
            "PubMed access is not configured for this deployment."
            if "NCBI_EMAIL" in str(exc)
            else "PubMed is temporarily unavailable."
        )
        return service.failure_response(
            body, endpoint_name, status="unavailable", reason=reason
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
    return PathwaysResponse(
        endpoint_id=explanation.endpoint_id,
        method=explanation.method,
        status="ok",
        reason=None if cards else "no pathways met the evidence threshold for this result",
        pathways=cards,
        method_block=_method_block(
            outcome.n_input_genes, outcome.universe_size, outcome.family_size
        ),
    )
