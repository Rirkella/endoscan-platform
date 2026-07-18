"""Typed deterministic tools for bounded NCBI GEO metadata discovery."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlparse
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .artifacts import LocalArtifactStore
from .config import AgentRunMode
from .contracts import SourceToolDiagnostic, ToolInvocation
from .source_cache import SourceResponseCache
from .source_security import (
    ScientificResponse,
    ScientificSourceClient,
    SourceFormatError,
    SourceRateLimitError,
    SourceResponseError,
    SourceTimeoutError,
    SourceToolError,
    SourceUnavailableError,
    sanitize_untrusted_text,
)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
GEO_SOFT = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi"
GSE_PATTERN = re.compile(r"^GSE[1-9][0-9]{1,8}$", re.I)
ALLOWED_GEO_ORGANISMS = frozenset({"Homo sapiens", "Mus musculus"})
ALLOWED_GEO_STUDY_TYPES = frozenset(
    {
        "Expression profiling by array",
        "Expression profiling by high throughput sequencing",
        "array",
        "sequencing",
    }
)


class ToolContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchGeoSeriesInput(ToolContract):
    scientific_terms: list[str] = Field(
        min_length=1,
        max_length=4,
        description=(
            "Required concrete scientific concepts for the bounded GEO query. "
            "At least one non-empty term is required; empty strings are not allowed."
        ),
    )
    organism_alternatives: list[str] = Field(
        default_factory=lambda: ["Homo sapiens"],
        max_length=4,
        description=(
            "Optional allowlisted organism alternatives. Return [] when no organism "
            "constraint is intended. Empty strings are not allowed."
        ),
    )
    study_type_alternatives: list[str] = Field(
        default_factory=list,
        max_length=4,
        description=(
            "Optional allowlisted GEO study-type terms. Return [] when no study-type "
            "constraint is intended. Empty strings are not allowed."
        ),
    )
    cell_tissue_terms: list[str] = Field(
        default_factory=list,
        max_length=4,
        description=(
            "Optional concrete cell-line, tissue, or organ terms. Return [] when no such "
            "constraint is intended. Empty strings are not allowed."
        ),
    )
    treatment_terms: list[str] = Field(
        default_factory=list,
        max_length=4,
        description=(
            "Optional concrete treatment or exposure terms. Return [] when no such "
            "constraint is intended. Empty strings are not allowed."
        ),
    )
    maximum_results: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Maximum bounded GEO Series results to return (1 through 10).",
    )
    publication_date_start: str | None = Field(
        default=None, pattern=r"^[0-9]{4}(/[0-9]{2}/[0-9]{2})?$"
    )
    publication_date_end: str | None = Field(
        default=None, pattern=r"^[0-9]{4}(/[0-9]{2}/[0-9]{2})?$"
    )
    strategy_reason: str = Field(
        min_length=5,
        max_length=500,
        description="Brief scientific reason for this bounded search plan.",
    )

    @field_validator(
        "scientific_terms",
        "organism_alternatives",
        "study_type_alternatives",
        "cell_tissue_terms",
        "treatment_terms",
    )
    @classmethod
    def validate_terms(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .+/_-]{0,119}", item):
                raise ValueError("search terms contain unsupported characters")
        return value

    @field_validator("organism_alternatives")
    @classmethod
    def validate_organisms(cls, value: list[str]) -> list[str]:
        allowed = {item.casefold() for item in ALLOWED_GEO_ORGANISMS}
        if any(item.casefold() not in allowed for item in value):
            raise ValueError("organism alternatives must use allowlisted values")
        return value

    @field_validator("study_type_alternatives")
    @classmethod
    def validate_study_types(cls, value: list[str]) -> list[str]:
        allowed = {item.casefold() for item in ALLOWED_GEO_STUDY_TYPES}
        if any(item.casefold() not in allowed for item in value):
            raise ValueError("study type alternatives must use allowlisted values")
        return value

    @field_validator("publication_date_start", "publication_date_end")
    @classmethod
    def validate_publication_date(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            datetime.strptime(value, "%Y" if "/" not in value else "%Y/%m/%d")
        except ValueError as exc:
            raise ValueError("publication date is invalid") from exc
        return value

    @model_validator(mode="after")
    def validate_publication_range(self) -> SearchGeoSeriesInput:
        if self.publication_date_start and self.publication_date_end:
            start = self.publication_date_start.replace("/", "")
            end = self.publication_date_end.replace("/", "")
            if start.ljust(8, "0") > end.ljust(8, "9"):
                raise ValueError("publication date range is invalid")
        return self


class SearchGeoSeriesOutput(ToolContract):
    schema_version: str = "1.0.0"
    typed_request: dict[str, Any]
    rendered_query: str
    normalized_query: str
    strategy_reason: str
    results: list[dict[str, Any]]
    result_count: int = Field(ge=0)
    new_accession_count: int = Field(ge=0)
    retrieval_timestamp: datetime
    source_artifact_id: str
    cache_status: Literal["live", "cached", "not_executed"]
    search_executed: bool = True
    stop_reason: Literal["duplicate_query", "sufficient_candidates", "search_limit"] | None = None


def render_geo_query(request: SearchGeoSeriesInput) -> str:
    """Render a deterministic NCBI query: concepts use AND, alternatives use OR."""

    def term(value: str, field: str) -> str:
        return f'"{value}"[{field}]'

    def alternatives(values: list[str], field: str) -> str | None:
        if not values:
            return None
        rendered = [term(value, field) for value in values]
        return rendered[0] if len(rendered) == 1 else f"({' OR '.join(rendered)})"

    concepts = [term(value, "All Fields") for value in request.scientific_terms]
    concepts.append("gse[Entry Type]")
    for values, field in (
        (request.organism_alternatives, "Organism"),
        (request.study_type_alternatives, "All Fields"),
        (request.cell_tissue_terms, "All Fields"),
        (request.treatment_terms, "All Fields"),
    ):
        rendered = alternatives(values, field)
        if rendered:
            concepts.append(rendered)
    if request.publication_date_start or request.publication_date_end:
        start = request.publication_date_start or "1900"
        end = request.publication_date_end or "3000"
        concepts.append(f"{start}:{end}[Publication Date]")
    return " AND ".join(concepts)


def _geo_query_identity(request: SearchGeoSeriesInput) -> str:
    """Canonical identity for semantically identical commutative GEO query clauses."""

    return json.dumps(
        {
            "scientific_terms": sorted(item.casefold() for item in request.scientific_terms),
            "organism_alternatives": sorted(
                item.casefold() for item in request.organism_alternatives
            ),
            "study_type_alternatives": sorted(
                item.casefold() for item in request.study_type_alternatives
            ),
            "cell_tissue_terms": sorted(item.casefold() for item in request.cell_tissue_terms),
            "treatment_terms": sorted(item.casefold() for item in request.treatment_terms),
            "publication_date_start": request.publication_date_start,
            "publication_date_end": request.publication_date_end,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class GeoAccessionInput(ToolContract):
    accession: str = Field(pattern=r"^GSE[1-9][0-9]{1,8}$")


GeoValidationStatus = Literal[
    "public_valid",
    "indexed_but_record_unavailable",
    "not_public",
    "not_found",
    "invalid_accession",
    "temporarily_unavailable",
    "unexpected_source_format",
]


class GeoAccessionsInput(ToolContract):
    accessions: list[Annotated[str, Field(pattern=r"^GSE[1-9][0-9]{1,8}$")]] = Field(
        min_length=1,
        max_length=5,
        description=(
            "One to five explicit GEO Series accessions. Supply GSE identifiers only; "
            "never supply URLs. Duplicates are removed while preserving first order."
        ),
    )

    @field_validator("accessions", mode="before")
    @classmethod
    def normalize_accessions(cls, values: Any) -> Any:
        if not isinstance(values, list):
            return values
        normalized: list[str] = []
        for value in values:
            if not isinstance(value, str):
                return values
            accession = value.strip().upper()
            if accession not in normalized:
                normalized.append(accession)
        return normalized


class GeoValidationOutput(ToolContract):
    schema_version: str = "1.0.0"
    accession: str
    status: GeoValidationStatus
    exists_in_geo_index: bool
    public_record_available: bool
    title: str | None = None
    organism: list[str] = Field(default_factory=list)
    study_type: list[str] = Field(default_factory=list)
    source_reference: str
    source_artifact_id: str | None = None
    source_artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_references: list[str] = Field(default_factory=list)
    validation_timestamp: datetime
    safe_warning_or_error_category: str | None = None
    retryable: bool = False
    cache_status: Literal["live", "cached", "not_available"]
    source_diagnostic: SourceToolDiagnostic | None = None


class GeoValidationBatchOutput(ToolContract):
    results: list[GeoValidationOutput]
    public_valid_count: int = Field(ge=0, le=5)
    unavailable_count: int = Field(ge=0, le=5)
    invalid_count: int = Field(ge=0, le=5)
    source_artifact_references: list[str] = Field(default_factory=list, max_length=5)
    cache_states: list[Literal["live", "cached", "not_available"]] = Field(
        default_factory=list, max_length=5
    )


class GeoSeriesMetadataOutput(ToolContract):
    schema_version: str = "1.0.0"
    accession: str
    title: str
    summary: str
    organism: list[str]
    study_type: list[str]
    sample_metadata_summary: list[dict[str, Any]]
    platform_ids: list[str]
    sample_count: int
    related_publication_ids: list[str]
    experimental_variables: list[str]
    source_links: list[str]
    raw_source_artifact_id: str
    evidence_references: list[str]
    prompt_injection_warnings: list[str]
    cache_status: Literal["live", "cached"]


class GeoSampleDesignOutput(ToolContract):
    schema_version: str = "1.0.0"
    accession: str
    likely_treatment_groups: list[str]
    likely_control_groups: list[str]
    cell_lines_or_tissues: list[str]
    dose_fields: list[str]
    time_fields: list[str]
    replicate_counts: dict[str, int]
    missing_metadata: list[str]
    evidence_references: list[str]
    uncertainty: list[str]
    prompt_injection_warnings: list[str]


class PublicationMetadataInput(ToolContract):
    publication_ids: list[str] = Field(min_length=1, max_length=10)


class PublicationMetadataOutput(ToolContract):
    schema_version: str = "1.0.0"
    publications: list[dict[str, Any]]
    source_artifact_id: str
    evidence_references: list[str]
    prompt_injection_warnings: list[str]
    cache_status: Literal["live", "cached"]


class DatasetComparisonCandidate(ToolContract):
    """Bounded fields accepted by the deterministic candidate comparator."""

    accession: str = Field(pattern=r"^GSE[1-9][0-9]{1,8}$")
    title: str = Field(max_length=500)
    organism: list[str] = Field(default_factory=list, max_length=10)
    sample_count: int = Field(default=0, ge=0, le=1_000_000)
    biological_context: str = Field(default="", max_length=2000)
    likely_treatment_groups: list[str] = Field(default_factory=list, max_length=50)
    likely_control_groups: list[str] = Field(default_factory=list, max_length=50)
    dose_time_evidence: str = Field(default="", max_length=2000)
    source_artifact_ids: list[str] = Field(default_factory=list, max_length=50)


class CompareDatasetCandidatesInput(ToolContract):
    candidates: list[DatasetComparisonCandidate] = Field(min_length=1, max_length=10)


class CompareDatasetCandidatesOutput(ToolContract):
    schema_version: str = "1.0.0"
    candidates: list[dict[str, Any]]
    comparison_fields: list[str]


class DiscoveryToolService:
    def __init__(
        self,
        cache: SourceResponseCache,
        artifacts: LocalArtifactStore,
        client: ScientificSourceClient,
        *,
        ncbi_email: str | None = None,
        ncbi_api_key: str | None = None,
    ):
        self.cache = cache
        self.artifacts = artifacts
        self.client = client
        self.ncbi_email = ncbi_email
        self.ncbi_api_key = ncbi_api_key
        self._search_state: dict[tuple[str, str | None], dict[str, Any]] = {}

    def search_geo_series(
        self, request: SearchGeoSeriesInput, invocation: ToolInvocation
    ) -> SearchGeoSeriesOutput:
        args = request.model_dump(mode="json")
        cache_args = {key: value for key, value in args.items() if key != "strategy_reason"}
        rendered_query = render_geo_query(request)
        normalized_query = rendered_query.casefold()
        query_identity = _geo_query_identity(request)
        search_key = (_required(invocation.workflow_id, "workflow_id"), invocation.step_id)
        state = self._search_state.setdefault(
            search_key,
            {"queries": {}, "seen_accessions": set(), "search_count": 0, "last": None},
        )
        prior = state["queries"].get(query_identity)
        if prior is not None:
            return prior.model_copy(
                update={
                    "typed_request": args,
                    "strategy_reason": request.strategy_reason,
                    "results": [],
                    "new_accession_count": 0,
                    "cache_status": "not_executed",
                    "search_executed": False,
                    "stop_reason": "duplicate_query",
                }
            )
        if len(state["seen_accessions"]) >= 2:
            return self._skipped_search(
                request,
                rendered_query,
                state["last"],
                reason="sufficient_candidates",
            )
        if state["search_count"] >= 4:
            return self._skipped_search(
                request,
                rendered_query,
                state["last"],
                reason="search_limit",
            )
        state["search_count"] += 1

        def retrieve() -> tuple[ScientificResponse, dict[str, Any]]:
            common = self._eutils_params()
            search = self.client.get(
                f"{EUTILS}/esearch.fcgi",
                tool_name="search_geo_series",
                params={
                    **common,
                    "db": "gds",
                    "term": rendered_query,
                    "retmax": request.maximum_results,
                    "retmode": "json",
                },
                accepted_types={"application/json"},
            )
            search_json = json.loads(search.content)
            ids = search_json.get("esearchresult", {}).get("idlist", [])
            summaries: dict[str, Any] = {}
            summary_response = search
            if ids:
                summary_response = self.client.get(
                    f"{EUTILS}/esummary.fcgi",
                    tool_name="search_geo_series",
                    params={**common, "db": "gds", "id": ",".join(ids), "retmode": "json"},
                    accepted_types={"application/json"},
                )
                summaries = json.loads(summary_response.content).get("result", {})
            parsed_results = []
            accessions = set()
            for uid in ids:
                item = summaries.get(str(uid), {})
                accession = str(item.get("accession") or item.get("Accession") or "").upper()
                if not GSE_PATTERN.fullmatch(accession):
                    continue
                if accession in accessions:
                    continue
                accessions.add(accession)
                title = sanitize_untrusted_text(
                    str(item.get("title") or ""), source_id=f"geo:{accession}:title"
                )
                summary = sanitize_untrusted_text(
                    str(item.get("summary") or item.get("description") or ""),
                    source_id=f"geo:{accession}:summary",
                )
                parsed_results.append(
                    {
                        "accession": accession,
                        "title": title["untrusted_text"],
                        "summary": summary["untrusted_text"],
                        "organism": item.get("taxon") or item.get("taxa") or [],
                        "study_type": item.get("gdstype") or item.get("entrytype") or "",
                        "sample_count": int(item.get("n_samples") or item.get("samples") or 0),
                        "source_url": f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={accession}",
                        "prompt_injection_warnings": [
                            *title["prompt_injection_warnings"],
                            *summary["prompt_injection_warnings"],
                        ],
                    }
                )
            combined = {
                "typed_request": args,
                "rendered_query": rendered_query,
                "search": search_json,
                "summaries": summaries,
            }
            combined_bytes = json.dumps(combined, sort_keys=True).encode()
            response = ScientificResponse(
                url=summary_response.url,
                content=combined_bytes,
                content_type="application/json",
                status_code=200,
                headers={"content-type": "application/json"},
                retrieved_at=summary_response.retrieved_at,
                diagnostic=summary_response.diagnostic,
            )
            return response, {
                "results": parsed_results,
                "result_count": len(parsed_results),
                "retrieval_timestamp": datetime.fromtimestamp(
                    response.retrieved_at, UTC
                ).isoformat(),
            }

        parsed, artifact_id, cache_status, _url, _diagnostic, _sha256 = self._cached_source(
            "search_geo_series", cache_args, invocation, retrieve, suffix="json"
        )
        new_results = [
            item
            for item in parsed.get("results", [])
            if item["accession"] not in state["seen_accessions"]
        ]
        state["seen_accessions"].update(item["accession"] for item in new_results)
        output = SearchGeoSeriesOutput(
            typed_request=args,
            rendered_query=rendered_query,
            normalized_query=normalized_query,
            strategy_reason=request.strategy_reason,
            results=new_results,
            result_count=int(parsed.get("result_count", len(parsed.get("results", [])))),
            new_accession_count=len(new_results),
            retrieval_timestamp=parsed.get("retrieval_timestamp", datetime.now(UTC)),
            source_artifact_id=artifact_id,
            cache_status=cache_status,
        )
        state["queries"][query_identity] = output
        state["last"] = output
        return output

    @staticmethod
    def _skipped_search(
        request: SearchGeoSeriesInput,
        rendered_query: str,
        prior: SearchGeoSeriesOutput | None,
        *,
        reason: Literal["sufficient_candidates", "search_limit"],
    ) -> SearchGeoSeriesOutput:
        if prior is None:
            raise SourceUnavailableError("No prior GEO search is available for bounded reuse.")
        return prior.model_copy(
            update={
                "typed_request": request.model_dump(mode="json"),
                "rendered_query": rendered_query,
                "normalized_query": rendered_query.casefold(),
                "strategy_reason": request.strategy_reason,
                "results": [],
                "result_count": 0,
                "new_accession_count": 0,
                "cache_status": "not_executed",
                "search_executed": False,
                "stop_reason": reason,
            }
        )

    def validate_geo_accession(
        self, request: GeoAccessionInput, invocation: ToolInvocation
    ) -> GeoValidationOutput:
        """Compatibility wrapper over the production bounded batch validator."""

        return self.validate_geo_accessions(
            GeoAccessionsInput(accessions=[request.accession]), invocation
        ).results[0]

    def validate_geo_accessions(
        self, request: GeoAccessionsInput, invocation: ToolInvocation
    ) -> GeoValidationBatchOutput:
        results: list[GeoValidationOutput] = []
        for accession in request.accessions:
            results.append(self._validate_geo_candidate(accession, invocation))
        valid = sum(item.status == "public_valid" for item in results)
        unavailable = sum(
            item.status in {"indexed_but_record_unavailable", "temporarily_unavailable"}
            for item in results
        )
        return GeoValidationBatchOutput(
            results=results,
            public_valid_count=valid,
            unavailable_count=unavailable,
            invalid_count=len(results) - valid - unavailable,
            source_artifact_references=[
                f"artifact:{item.source_artifact_id}"
                for item in results
                if item.source_artifact_id
            ],
            cache_states=[item.cache_status for item in results],
        )

    def _validate_geo_candidate(
        self, accession: str, invocation: ToolInvocation
    ) -> GeoValidationOutput:
        requested = accession.upper()
        source_reference = f"{GEO_SOFT}?acc={requested}"
        now = datetime.now(UTC)
        if not GSE_PATTERN.fullmatch(requested):
            return GeoValidationOutput(
                accession=requested,
                status="invalid_accession",
                exists_in_geo_index=False,
                public_record_available=False,
                source_reference=source_reference,
                validation_timestamp=now,
                safe_warning_or_error_category="invalid_accession",
                cache_status="not_available",
            )
        try:
            parsed, artifact_id, cache_status, url, diagnostic, sha256 = self._geo_brief(
                requested, invocation
            )
        except SourceFormatError as exc:
            return self._negative_validation(
                requested,
                "unexpected_source_format",
                source_reference,
                now,
                exc,
            )
        except SourceResponseError as exc:
            status = exc.diagnostic.http_status if exc.diagnostic else None
            if status in {401, 403}:
                outcome: GeoValidationStatus = "not_public"
                indexed = True
            elif status in {404, 410}:
                indexed = self._geo_index_exists(requested)
                outcome = "indexed_but_record_unavailable" if indexed else "not_found"
            else:
                indexed = False
                outcome = "unexpected_source_format"
            return self._negative_validation(
                requested,
                outcome,
                source_reference,
                now,
                exc,
                exists_in_geo_index=indexed,
            )
        except (SourceTimeoutError, SourceRateLimitError, SourceUnavailableError) as exc:
            return self._negative_validation(
                requested,
                "temporarily_unavailable",
                source_reference,
                now,
                exc,
                retryable=True,
            )
        returned = str(parsed.get("accession") or "").upper()
        record_type = str(parsed.get("record_type") or "")
        record_status = str(parsed.get("record_status") or "").casefold()
        if returned != requested or record_type != "Series":
            return GeoValidationOutput(
                accession=requested,
                status="unexpected_source_format",
                exists_in_geo_index=bool(returned),
                public_record_available=False,
                source_reference=url,
                source_artifact_id=artifact_id,
                source_artifact_sha256=sha256,
                evidence_references=[f"artifact:{artifact_id}#Series_geo_accession"],
                validation_timestamp=now,
                safe_warning_or_error_category=(
                    "accession_mismatch"
                    if returned and returned != requested
                    else "record_type_mismatch"
                ),
                cache_status=cache_status,
                source_diagnostic=diagnostic,
            )
        if not record_status:
            return GeoValidationOutput(
                accession=requested,
                status="unexpected_source_format",
                exists_in_geo_index=True,
                public_record_available=False,
                source_reference=url,
                source_artifact_id=artifact_id,
                source_artifact_sha256=sha256,
                evidence_references=[f"artifact:{artifact_id}#Series_geo_accession"],
                validation_timestamp=now,
                safe_warning_or_error_category="record_status_missing",
                cache_status=cache_status,
                source_diagnostic=diagnostic,
            )
        if any(marker in record_status for marker in ("private", "suppressed", "on hold")):
            return GeoValidationOutput(
                accession=requested,
                status="not_public",
                exists_in_geo_index=True,
                public_record_available=False,
                source_reference=url,
                source_artifact_id=artifact_id,
                source_artifact_sha256=sha256,
                evidence_references=[f"artifact:{artifact_id}#Series_status"],
                validation_timestamp=now,
                safe_warning_or_error_category="record_not_public",
                cache_status=cache_status,
                source_diagnostic=diagnostic,
            )
        return GeoValidationOutput(
            accession=requested,
            status="public_valid",
            exists_in_geo_index=True,
            public_record_available=True,
            title=parsed.get("title") or None,
            organism=parsed.get("organism", []),
            study_type=parsed.get("study_type", []),
            source_reference=url,
            source_artifact_id=artifact_id,
            source_artifact_sha256=sha256,
            evidence_references=[
                f"artifact:{artifact_id}#Series_geo_accession",
                f"artifact:{artifact_id}#Series_title",
            ],
            validation_timestamp=now,
            cache_status=cache_status,
            source_diagnostic=diagnostic,
        )

    @staticmethod
    def _negative_validation(
        accession: str,
        status: GeoValidationStatus,
        source_reference: str,
        timestamp: datetime,
        exc: SourceToolError,
        *,
        exists_in_geo_index: bool = False,
        retryable: bool | None = None,
    ) -> GeoValidationOutput:
        return GeoValidationOutput(
            accession=accession,
            status=status,
            exists_in_geo_index=exists_in_geo_index,
            public_record_available=False,
            source_reference=source_reference,
            validation_timestamp=timestamp,
            safe_warning_or_error_category=(
                exc.diagnostic.source_error_category if exc.diagnostic else status
            ),
            retryable=exc.retryable if retryable is None else retryable,
            cache_status="not_available",
            source_diagnostic=exc.diagnostic,
        )

    def _geo_index_exists(self, accession: str) -> bool:
        try:
            response = self.client.get(
                f"{EUTILS}/esearch.fcgi",
                tool_name="validate_geo_accessions",
                params={
                    **self._eutils_params(),
                    "db": "gds",
                    "term": f'"{accession}"[Accession] AND gse[Entry Type]',
                    "retmax": 1,
                    "retmode": "json",
                },
                accepted_types={"application/json"},
            )
            payload = json.loads(response.content)
            return bool(payload.get("esearchresult", {}).get("idlist", []))
        except (SourceToolError, json.JSONDecodeError):
            return False

    def fetch_geo_series_metadata(
        self, request: GeoAccessionInput, invocation: ToolInvocation
    ) -> GeoSeriesMetadataOutput:
        parsed, artifact_id, cache_status, url, _diagnostic, _sha256 = self._geo_soft(
            request.accession, invocation
        )
        if parsed.get("accession") != request.accession.upper():
            raise SourceResponseError("GEO accession did not resolve to an official Series record.")

        def reference(field: str) -> str:
            return f"artifact:{artifact_id}#{field}"

        return GeoSeriesMetadataOutput(
            accession=parsed["accession"],
            title=parsed.get("title", ""),
            summary=parsed.get("summary", ""),
            organism=parsed.get("organism", []),
            study_type=parsed.get("study_type", []),
            sample_metadata_summary=parsed.get("samples", []),
            platform_ids=parsed.get("platform_ids", []),
            sample_count=len(parsed.get("samples", [])),
            related_publication_ids=parsed.get("publication_ids", []),
            experimental_variables=parsed.get("experimental_variables", []),
            source_links=[url],
            raw_source_artifact_id=artifact_id,
            evidence_references=[
                reference("Series_geo_accession"),
                reference("Series_title"),
                reference("Series_summary"),
                reference("Sample_records"),
            ],
            prompt_injection_warnings=parsed.get("prompt_injection_warnings", []),
            cache_status=cache_status,
        )

    def inspect_geo_sample_design(
        self, request: GeoAccessionInput, invocation: ToolInvocation
    ) -> GeoSampleDesignOutput:
        parsed, artifact_id, _cache_status, _url, _diagnostic, _sha256 = self._geo_soft(
            request.accession, invocation
        )
        samples = parsed.get("samples", [])
        treatments: set[str] = set()
        controls: set[str] = set()
        contexts: set[str] = set()
        doses: set[str] = set()
        times: set[str] = set()
        replicate_counts: dict[str, int] = {}
        warnings = list(parsed.get("prompt_injection_warnings", []))
        for sample in samples:
            text = " ".join(str(value) for value in sample.values()).lower()
            title = str(sample.get("title", "unknown"))
            if any(token in text for token in ("control", "vehicle", "untreated", "mock")):
                controls.add(title)
            elif any(token in text for token in ("treated", "exposed", "dose", "compound")):
                treatments.add(title)
            contexts.update(sample.get("source", []))
            for characteristic in sample.get("characteristics", []):
                lower = characteristic.lower()
                if "dose" in lower or "concentration" in lower:
                    doses.add(characteristic)
                if "time" in lower or "duration" in lower:
                    times.add(characteristic)
            normalized_group = re.sub(
                r"\b(rep(licate)?\s*[0-9]+|[0-9]+)$", "", title, flags=re.I
            ).strip()
            replicate_counts[normalized_group or title] = (
                replicate_counts.get(normalized_group or title, 0) + 1
            )
        missing = []
        if not controls:
            missing.append("No explicit control or vehicle group was detected.")
        if not treatments:
            missing.append("No explicit treatment group was detected.")
        if not doses:
            missing.append("Dose metadata was not detected.")
        if not times:
            missing.append("Time metadata was not detected.")
        uncertainty = [
            "Group detection is deterministic keyword extraction, not a scientific label decision.",
            "A human scientist must verify ambiguous treatment and control labels.",
        ]
        return GeoSampleDesignOutput(
            accession=request.accession.upper(),
            likely_treatment_groups=sorted(treatments),
            likely_control_groups=sorted(controls),
            cell_lines_or_tissues=sorted(item for item in contexts if item),
            dose_fields=sorted(doses),
            time_fields=sorted(times),
            replicate_counts=replicate_counts,
            missing_metadata=missing,
            evidence_references=[f"artifact:{artifact_id}#Sample_records"],
            uncertainty=uncertainty,
            prompt_injection_warnings=warnings,
        )

    def fetch_publication_metadata(
        self, request: PublicationMetadataInput, invocation: ToolInvocation
    ) -> PublicationMetadataOutput:
        normalized = sorted({item for item in request.publication_ids if item.isdigit()})
        if len(normalized) != len(set(request.publication_ids)):
            raise SourceResponseError(
                "Publication identifiers must be numeric dataset-linked PMIDs."
            )

        def retrieve() -> tuple[ScientificResponse, dict[str, Any]]:
            response = self.client.get(
                f"{EUTILS}/efetch.fcgi",
                tool_name="fetch_publication_metadata",
                params={
                    **self._eutils_params(),
                    "db": "pubmed",
                    "id": ",".join(normalized),
                    "retmode": "xml",
                },
                accepted_types={"application/xml", "text/xml"},
            )
            root = ElementTree.fromstring(response.content)
            publications = []
            warnings: list[str] = []
            for article in root.findall(".//PubmedArticle"):
                pmid = "".join(article.findtext(".//PMID", default="")).strip()
                title_data = sanitize_untrusted_text(
                    "".join(article.find(".//ArticleTitle").itertext())
                    if article.find(".//ArticleTitle") is not None
                    else "",
                    source_id=f"pubmed:{pmid}:title",
                )
                abstract_data = sanitize_untrusted_text(
                    " ".join(
                        "".join(node.itertext()) for node in article.findall(".//AbstractText")
                    ),
                    source_id=f"pubmed:{pmid}:abstract",
                )
                warnings.extend(title_data["prompt_injection_warnings"])
                warnings.extend(abstract_data["prompt_injection_warnings"])
                publications.append(
                    {
                        "pmid": pmid,
                        "title": title_data["untrusted_text"],
                        "abstract": abstract_data["untrusted_text"],
                        "journal": article.findtext(".//Journal/Title", default=""),
                        "source_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                    }
                )
            return response, {"publications": publications, "warnings": warnings}

        parsed, artifact_id, cache_status, _url, _diagnostic, _sha256 = self._cached_source(
            "fetch_publication_metadata",
            {"publication_ids": normalized},
            invocation,
            retrieve,
            suffix="xml",
        )
        return PublicationMetadataOutput(
            publications=parsed.get("publications", []),
            source_artifact_id=artifact_id,
            evidence_references=[f"artifact:{artifact_id}#PubmedArticle"],
            prompt_injection_warnings=parsed.get("warnings", []),
            cache_status=cache_status,
        )

    @staticmethod
    def compare_dataset_candidates(
        request: CompareDatasetCandidatesInput, _invocation: ToolInvocation
    ) -> CompareDatasetCandidatesOutput:
        normalized = []
        for candidate in request.candidates:
            candidate_payload = candidate.model_dump(mode="json")
            sample_count = candidate.sample_count
            control_evidence = bool(candidate.likely_control_groups)
            treatment_evidence = bool(candidate.likely_treatment_groups)
            metadata_score = (
                min(sample_count, 100) + 25 * control_evidence + 25 * treatment_evidence
            )
            normalized.append({**candidate_payload, "metadata_completeness_score": metadata_score})
        normalized.sort(
            key=lambda item: (
                -int(item["metadata_completeness_score"]),
                str(item.get("accession", "")),
            )
        )
        return CompareDatasetCandidatesOutput(
            candidates=normalized,
            comparison_fields=[
                "sample_count",
                "organism",
                "biological_context",
                "treatment_evidence",
                "control_evidence",
                "dose_time_evidence",
                "metadata_completeness_score",
            ],
        )

    def _geo_soft(
        self, accession: str, invocation: ToolInvocation
    ) -> tuple[
        dict[str, Any],
        str,
        Literal["live", "cached"],
        str,
        SourceToolDiagnostic,
        str,
    ]:
        accession = accession.upper()

        def retrieve() -> tuple[ScientificResponse, dict[str, Any]]:
            response = self.client.get(
                GEO_SOFT,
                tool_name=invocation.tool_name,
                params={"acc": accession, "targ": "self", "form": "text", "view": "full"},
                accepted_types={"text/plain"},
            )
            return response, _parse_geo_soft(response.content.decode("utf-8", errors="replace"))

        return self._cached_source(
            "geo_series_soft", {"accession": accession}, invocation, retrieve, suffix="txt"
        )

    def _geo_brief(
        self, accession: str, invocation: ToolInvocation
    ) -> tuple[
        dict[str, Any],
        str,
        Literal["live", "cached"],
        str,
        SourceToolDiagnostic,
        str,
    ]:
        accession = accession.upper()

        def retrieve() -> tuple[ScientificResponse, dict[str, Any]]:
            response = self.client.get(
                GEO_SOFT,
                tool_name="validate_geo_accessions",
                params={"acc": accession, "targ": "self", "view": "brief", "form": "text"},
                accepted_types={"text/plain"},
            )
            text = response.content.decode("utf-8", errors="replace")
            if _looks_like_html_or_search_form(text):
                diagnostic = response.diagnostic.model_copy(
                    update={
                        "source_error_category": "generic_geo_page",
                        "exception_class": "SourceFormatError",
                        "developer_message": (
                            "GEO Accession Display returned a generic HTML/search document "
                            "instead of a machine-readable Series record."
                        ),
                    }
                ) if response.diagnostic else None
                raise SourceFormatError(
                    "GEO returned a generic page instead of a Series record.",
                    diagnostic=diagnostic,
                )
            parsed = _parse_geo_soft(text)
            if not parsed.get("record_accession") and not parsed.get("accession"):
                diagnostic = response.diagnostic.model_copy(
                    update={
                        "source_error_category": "malformed_geo_record",
                        "exception_class": "SourceFormatError",
                        "developer_message": (
                            "GEO text response did not contain a Series record marker or accession."
                        ),
                    }
                ) if response.diagnostic else None
                raise SourceFormatError(
                    "GEO returned an unrecognized machine-readable record.",
                    diagnostic=diagnostic,
                )
            return response, parsed

        return self._cached_source(
            "validate_geo_accession",
            {"accession": accession, "view": "brief"},
            invocation,
            retrieve,
            suffix="txt",
        )

    def _cached_source(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        invocation: ToolInvocation,
        retrieve,
        *,
        suffix: str,
    ) -> tuple[
        dict[str, Any],
        str,
        Literal["live", "cached"],
        str,
        SourceToolDiagnostic,
        str,
    ]:
        mode = AgentRunMode(str(invocation.run_context.get("run_mode", "replay")))
        refresh = bool(invocation.run_context.get("refresh_source_metadata", False))
        cached = None if refresh else self.cache.get(tool_name, arguments)
        if cached is not None:
            descriptor, raw = self.artifacts.get(cached.raw_artifact_id)
            current = self.artifacts.put_bytes(
                workflow_id=_required(invocation.workflow_id, "workflow_id"),
                step_id=invocation.step_id,
                content=raw,
                mime_type=descriptor.mime_type,
                artifact_type="scientific_source_raw",
                logical_name=f"source-{tool_name}-{cached.content_hash[:12]}.{suffix}",
                producer=tool_name,
                original_source=cached.source_url,
                idempotency_key=f"{invocation.idempotency_key}:cached-source",
            )
            diagnostic_payload = cached.http_metadata.get("diagnostic")
            if isinstance(diagnostic_payload, dict):
                diagnostic = SourceToolDiagnostic.model_validate(diagnostic_payload)
            else:
                parsed_url = urlparse(cached.source_url)
                headers = cached.http_metadata.get("headers", {})
                diagnostic = SourceToolDiagnostic(
                    tool_name=tool_name,
                    source_host=(parsed_url.hostname or "unknown").lower().rstrip("."),
                    safe_url_path=parsed_url.path or "/",
                    http_status=cached.http_metadata.get("status_code"),
                    final_approved_host=(parsed_url.hostname or "unknown").lower().rstrip("."),
                    content_type=(
                        headers.get("content-type") if isinstance(headers, dict) else None
                    ),
                    response_byte_count=len(raw),
                    source_error_category="none",
                    request_duration_ms=0,
                )
            return (
                cached.parsed_output,
                current.id,
                "cached",
                cached.source_url,
                diagnostic,
                cached.content_hash,
            )
        if mode is not AgentRunMode.LIVE and not refresh:
            raise SourceUnavailableError(
                "Cached mode has no fresh source artifact; an explicit live refresh is required."
            )
        response, parsed = retrieve()
        artifact = self.artifacts.put_bytes(
            workflow_id=_required(invocation.workflow_id, "workflow_id"),
            step_id=invocation.step_id,
            content=response.content,
            mime_type=response.content_type,
            artifact_type="scientific_source_raw",
            logical_name=f"source-{tool_name}-{response.sha256[:12]}.{suffix}",
            producer=tool_name,
            original_source=response.url,
            idempotency_key=f"{invocation.idempotency_key}:live-source",
        )
        self.cache.put(
            tool_name,
            arguments,
            source_url=response.url,
            content_hash=response.sha256,
            parsed_output=parsed,
            raw_artifact_id=artifact.id,
            http_metadata={
                "status_code": response.status_code,
                "headers": response.headers,
                "diagnostic": (
                    response.diagnostic.model_dump(mode="json") if response.diagnostic else None
                ),
            },
        )
        diagnostic = response.diagnostic
        if diagnostic is None:
            parsed_url = urlparse(response.url)
            diagnostic = SourceToolDiagnostic(
                tool_name=tool_name,
                source_host=(parsed_url.hostname or "unknown").lower().rstrip("."),
                safe_url_path=parsed_url.path or "/",
                http_status=response.status_code,
                final_approved_host=(parsed_url.hostname or "unknown").lower().rstrip("."),
                content_type=response.content_type,
                response_byte_count=len(response.content),
                source_error_category="none",
            )
        return parsed, artifact.id, "live", response.url, diagnostic, response.sha256

    def _eutils_params(self) -> dict[str, str]:
        values = {"tool": "endoscan_phase1"}
        if self.ncbi_email:
            values["email"] = self.ncbi_email
        if self.ncbi_api_key:
            values["api_key"] = self.ncbi_api_key
        return values


def _looks_like_html_or_search_form(value: str) -> bool:
    head = value[:4_000].casefold()
    return any(
        marker in head
        for marker in ("<!doctype html", "<html", "<form", "geo accession display")
    ) and "^series" not in head


def _parse_geo_soft(value: str) -> dict[str, Any]:
    fields: dict[str, list[str]] = {}
    samples: list[dict[str, Any]] = []
    current_sample: dict[str, Any] | None = None
    warnings: list[str] = []
    record_type = ""
    record_accession = ""
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if line.startswith("^SERIES") and "=" in line:
            record_type = "Series"
            record_accession = line.split("=", 1)[-1].strip().upper()
            continue
        if line.startswith("^SAMPLE"):
            if current_sample:
                samples.append(current_sample)
            current_sample = {
                "accession": line.split("=", 1)[-1].strip(),
                "characteristics": [],
                "source": [],
            }
            continue
        if not line.startswith("!") or "=" not in line:
            continue
        key, raw = (item.strip() for item in line[1:].split("=", 1))
        cleaned = sanitize_untrusted_text(raw, source_id=f"geo-soft:{key}")
        warnings.extend(cleaned["prompt_injection_warnings"])
        text = cleaned["untrusted_text"]
        if current_sample is not None and key.startswith("Sample_"):
            short = key.removeprefix("Sample_").lower()
            if short == "characteristics_ch1":
                current_sample["characteristics"].append(text)
            elif short == "source_name_ch1":
                current_sample["source"].append(text)
            elif short in {"title", "organism_ch1"}:
                current_sample[short.replace("_ch1", "")] = text
            continue
        fields.setdefault(key, []).append(text)
    if current_sample:
        samples.append(current_sample)
    accession = next(iter(fields.get("Series_geo_accession", [])), record_accession).upper()
    if accession and not GSE_PATTERN.fullmatch(accession):
        accession = ""
    variables = sorted(
        {
            characteristic.split(":", 1)[0].strip()
            for sample in samples
            for characteristic in sample.get("characteristics", [])
            if ":" in characteristic
        }
    )
    return {
        "accession": accession,
        "record_accession": record_accession,
        "record_type": record_type,
        "record_status": next(iter(fields.get("Series_status", [])), ""),
        "title": next(iter(fields.get("Series_title", [])), ""),
        "summary": " ".join(fields.get("Series_summary", [])),
        "organism": sorted(
            {
                item
                for key, values in fields.items()
                if key.endswith("organism_ch1")
                for item in values
            }
        ),
        "study_type": sorted(set(fields.get("Series_type", []))),
        "platform_ids": sorted(set(fields.get("Series_platform_id", []))),
        "publication_ids": sorted(set(fields.get("Series_pubmed_id", []))),
        "experimental_variables": variables,
        "samples": samples,
        "prompt_injection_warnings": warnings,
    }


def _required(value: str | None, name: str) -> str:
    if not value:
        raise SourceResponseError(f"Tool invocation is missing required {name} context.")
    return value
