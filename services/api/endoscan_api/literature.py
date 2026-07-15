"""Official NCBI PubMed E-utilities client with bounded queries and process-local caching."""

from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

from .schemas import (
    LiteratureArticle,
    LiteratureProvenance,
    LiteratureQueryRecord,
    LiteratureRequest,
    LiteratureResponse,
)

EUTILS_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
PUBMED_URL = "https://pubmed.ncbi.nlm.nih.gov"
MAX_QUERY_TEMPLATES = 10
ABSTRACT_EXCERPT_CHARS = 600


class LiteratureUnavailableError(RuntimeError):
    """The external literature service could not produce a reliable response."""


class LiteratureRateLimitError(LiteratureUnavailableError):
    """NCBI rejected a request because its rate limit was reached."""


class LiteratureTimeoutError(LiteratureUnavailableError):
    """NCBI did not respond within the configured deadline."""


@dataclass(frozen=True)
class NcbiConfig:
    api_key: str | None
    tool: str
    email: str | None
    base_url: str = EUTILS_BASE_URL
    timeout_seconds: float = 8.0
    cache_ttl_seconds: float = 3600.0

    @classmethod
    def from_env(cls) -> NcbiConfig:
        return cls(
            api_key=os.environ.get("NCBI_API_KEY") or None,
            tool=(os.environ.get("NCBI_TOOL") or "endoscan").strip().replace(" ", "_"),
            email=os.environ.get("NCBI_EMAIL") or None,
        )


@dataclass(frozen=True)
class QuerySpec:
    category: str
    query: str
    genes: tuple[str, ...] = ()
    pathways: tuple[str, ...] = ()


Transport = Callable[[str, float], bytes]


def _default_transport(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "EndoScan-literature/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise LiteratureRateLimitError("NCBI PubMed rate limit reached") from exc
        raise LiteratureUnavailableError("NCBI PubMed returned an HTTP error") from exc
    except TimeoutError as exc:
        raise LiteratureTimeoutError("NCBI PubMed request timed out") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise LiteratureTimeoutError("NCBI PubMed request timed out") from exc
        raise LiteratureUnavailableError("NCBI PubMed is unavailable") from exc


def _safe_text(value: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f\[\]\"\\]", " ", value)
    return " ".join(cleaned.split())[:240]


def _safe_phrase(value: str) -> str:
    return f'"{_safe_text(value)}"[Title/Abstract]'


def _endpoint_clause(endpoint_id: str, endpoint_name: str) -> str:
    values = [_safe_phrase(endpoint_name)]
    if endpoint_id.casefold() != endpoint_name.casefold():
        values.append(_safe_phrase(endpoint_id))
    return f"({' OR '.join(values)})"


def _species_clause(species: str) -> str:
    if species.casefold() == "homo sapiens":
        return "(humans[MeSH Terms] OR \"Homo sapiens\"[Title/Abstract])"
    cleaned = _safe_text(species)
    return f'(\"{cleaned}\"[Title/Abstract] OR \"{cleaned}\"[MeSH Terms])'


def build_query_specs(body: LiteratureRequest, endpoint_name: str) -> list[QuerySpec]:
    """Build a small, deterministic family of transparent PubMed query templates."""
    endpoint = _endpoint_clause(body.endpoint_id, endpoint_name)
    species = _species_clause(body.species)
    context = f" AND {_safe_phrase(body.context)}" if body.context else ""
    specs: list[QuerySpec] = []

    for gene in body.genes[:4]:
        specs.append(
            QuerySpec(
                category="gene_endpoint",
                query=f"{_safe_phrase(gene)} AND {endpoint} AND {species}",
                genes=(gene,),
            )
        )

    for pathway in body.pathways[:3]:
        specs.append(
            QuerySpec(
                category="pathway_endpoint",
                query=f"{_safe_phrase(pathway.name)} AND {endpoint} AND {species}",
                pathways=(pathway.name,),
            )
        )

    for pathway in body.pathways:
        overlap = [gene for gene in body.genes if gene in pathway.genes]
        if overlap:
            gene = overlap[0]
            specs.append(
                QuerySpec(
                    category="gene_pathway",
                    query=f"{_safe_phrase(gene)} AND {_safe_phrase(pathway.name)} AND {species}",
                    genes=(gene,),
                    pathways=(pathway.name,),
                )
            )
        if len([spec for spec in specs if spec.category == "gene_pathway"]) >= 2:
            break

    if body.compound:
        for gene in body.genes[:2]:
            specs.append(
                QuerySpec(
                    category="compound_gene",
                    query=(
                        f"{_safe_phrase(body.compound)} AND {_safe_phrase(gene)} "
                        f"AND {endpoint} AND {species}{context}"
                    ),
                    genes=(gene,),
                )
            )
        if body.pathways:
            pathway = body.pathways[0]
            specs.append(
                QuerySpec(
                    category="compound_pathway",
                    query=(
                        f"{_safe_phrase(body.compound)} AND {_safe_phrase(pathway.name)} "
                        f"AND {endpoint} AND {species}{context}"
                    ),
                    pathways=(pathway.name,),
                )
            )

    return specs[:MAX_QUERY_TEMPLATES]


class LiteratureService:
    def __init__(
        self,
        config: NcbiConfig,
        *,
        transport: Transport = _default_transport,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._transport = transport
        self._monotonic = monotonic
        self._sleep = sleep
        self._rate_lock = threading.Lock()
        self._last_request_at: float | None = None
        self._cache_lock = threading.Lock()
        self._cache: dict[str, tuple[float, LiteratureResponse]] = {}
        self._inflight: dict[str, threading.Event] = {}

    @property
    def rate_limit(self) -> int:
        return 10 if self.config.api_key else 3

    def _params(self, extra: dict[str, str]) -> str:
        params = {"tool": self.config.tool, **extra}
        if self.config.email:
            params["email"] = self.config.email
        if self.config.api_key:
            params["api_key"] = self.config.api_key
        return urllib.parse.urlencode(params)

    def _request(self, utility: str, params: dict[str, str]) -> bytes:
        with self._rate_lock:
            now = self._monotonic()
            interval = 1.0 / self.rate_limit
            if self._last_request_at is not None:
                wait = interval - (now - self._last_request_at)
                if wait > 0:
                    self._sleep(wait)
            self._last_request_at = self._monotonic()
        url = f"{self.config.base_url}/{utility}.fcgi?{self._params(params)}"
        try:
            return self._transport(url, self.config.timeout_seconds)
        except (LiteratureRateLimitError, LiteratureTimeoutError, LiteratureUnavailableError):
            raise
        except TimeoutError as exc:
            raise LiteratureTimeoutError("NCBI PubMed request timed out") from exc
        except Exception as exc:
            raise LiteratureUnavailableError("NCBI PubMed response could not be retrieved") from exc

    def _provenance(self, *, cache_hit: bool) -> LiteratureProvenance:
        return LiteratureProvenance(
            provider="NCBI PubMed E-utilities",
            database="pubmed",
            eutils_base_url=self.config.base_url,
            retrieved_at=datetime.now(UTC).isoformat(),
            tool=self.config.tool,
            email_configured=self.config.email is not None,
            api_key_used=self.config.api_key is not None,
            rate_limit_per_second=self.rate_limit,
            cache_hit=cache_hit,
        )

    def failure_response(
        self,
        body: LiteratureRequest,
        endpoint_name: str,
        *,
        status: str,
        reason: str,
    ) -> LiteratureResponse:
        return LiteratureResponse(
            endpoint_id=body.endpoint_id,
            endpoint_name=endpoint_name,
            status=status,
            reason=reason,
            articles=[],
            queries=[
                LiteratureQueryRecord(
                    category=spec.category,
                    query=spec.query,
                    matched_genes=list(spec.genes),
                    matched_pathways=list(spec.pathways),
                    pmids=[],
                )
                for spec in build_query_specs(body, endpoint_name)
            ],
            provenance=self._provenance(cache_hit=False),
        )

    def lookup(self, body: LiteratureRequest, endpoint_name: str) -> LiteratureResponse:
        if not self.config.email:
            raise LiteratureUnavailableError(
                "NCBI_EMAIL is not configured; PubMed requests are disabled until "
                "contact metadata is set"
            )
        cache_key = json.dumps(
            {"request": body.model_dump(mode="json"), "endpoint_name": endpoint_name},
            sort_keys=True,
        )
        now = self._monotonic()
        owner = False
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached and cached[0] > now:
                return _with_cache_hit(cached[1])
            event = self._inflight.get(cache_key)
            if event is None:
                event = threading.Event()
                self._inflight[cache_key] = event
                owner = True

        if not owner:
            if not event.wait(self.config.timeout_seconds * 3):
                raise LiteratureTimeoutError("duplicate PubMed request did not complete in time")
            with self._cache_lock:
                cached = self._cache.get(cache_key)
            if cached and cached[0] > self._monotonic():
                return _with_cache_hit(cached[1])
            raise LiteratureUnavailableError("duplicate PubMed request did not produce a response")

        try:
            response = self._lookup_uncached(body, endpoint_name)
            with self._cache_lock:
                self._cache[cache_key] = (
                    self._monotonic() + self.config.cache_ttl_seconds,
                    response,
                )
            return response
        finally:
            with self._cache_lock:
                self._inflight.pop(cache_key, None)
                event.set()

    def _lookup_uncached(
        self, body: LiteratureRequest, endpoint_name: str
    ) -> LiteratureResponse:
        specs = build_query_specs(body, endpoint_name)
        records: list[LiteratureQueryRecord] = []
        ordered_pmids: list[str] = []
        for spec in specs:
            raw = self._request(
                "esearch",
                {
                    "db": "pubmed",
                    "retmode": "json",
                    "retmax": str(body.result_limit),
                    "sort": "relevance",
                    "term": spec.query,
                },
            )
            try:
                pmids = [str(value) for value in json.loads(raw)["esearchresult"]["idlist"]]
            except (KeyError, TypeError, ValueError) as exc:
                raise LiteratureUnavailableError(
                    "NCBI ESearch returned an invalid response"
                ) from exc
            records.append(
                LiteratureQueryRecord(
                    category=spec.category,
                    query=spec.query,
                    matched_genes=list(spec.genes),
                    matched_pathways=list(spec.pathways),
                    pmids=pmids,
                )
            )
            for pmid in pmids:
                if pmid not in ordered_pmids and len(ordered_pmids) < body.result_limit:
                    ordered_pmids.append(pmid)

        if not ordered_pmids:
            return LiteratureResponse(
                endpoint_id=body.endpoint_id,
                endpoint_name=endpoint_name,
                status="empty",
                reason="No PubMed records matched the recorded query templates.",
                articles=[],
                queries=records,
                provenance=self._provenance(cache_hit=False),
            )

        ids = ",".join(ordered_pmids)
        summary_raw = self._request(
            "esummary", {"db": "pubmed", "retmode": "json", "version": "2.0", "id": ids}
        )
        abstract_raw = self._request(
            "efetch", {"db": "pubmed", "retmode": "xml", "rettype": "abstract", "id": ids}
        )
        summaries = _parse_summaries(summary_raw)
        abstracts = _parse_abstracts(abstract_raw)
        articles = [
            _article(pmid, summaries.get(pmid, {}), abstracts.get(pmid), records, endpoint_name)
            for pmid in ordered_pmids
            if pmid in summaries
        ]
        return LiteratureResponse(
            endpoint_id=body.endpoint_id,
            endpoint_name=endpoint_name,
            status="ok" if articles else "empty",
            reason=None if articles else "PubMed returned no usable article summaries.",
            articles=articles,
            queries=records,
            provenance=self._provenance(cache_hit=False),
        )


def _with_cache_hit(response: LiteratureResponse) -> LiteratureResponse:
    return response.model_copy(
        update={
            "provenance": response.provenance.model_copy(update={"cache_hit": True})
        }
    )


def _parse_summaries(raw: bytes) -> dict[str, dict]:
    try:
        payload = json.loads(raw)
        result = payload["result"]
        return {
            str(pmid): result[str(pmid)]
            for pmid in result.get("uids", [])
            if str(pmid) in result
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise LiteratureUnavailableError("NCBI ESummary returned an invalid response") from exc


def _parse_abstracts(raw: bytes) -> dict[str, str]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise LiteratureUnavailableError("NCBI EFetch returned invalid XML") from exc
    abstracts: dict[str, str] = {}
    for article in root.findall(".//PubmedArticle"):
        pmid = article.findtext(".//MedlineCitation/PMID")
        parts = ["".join(node.itertext()).strip() for node in article.findall(".//AbstractText")]
        text = " ".join(part for part in parts if part)
        if pmid and text:
            abstracts[pmid] = text[:ABSTRACT_EXCERPT_CHARS]
    return abstracts


_CATEGORY_PRIORITY = {
    "compound_pathway": 0,
    "compound_gene": 1,
    "gene_pathway": 2,
    "pathway_endpoint": 3,
    "gene_endpoint": 4,
}


def _article(
    pmid: str,
    summary: dict,
    abstract: str | None,
    records: list[LiteratureQueryRecord],
    endpoint_name: str,
) -> LiteratureArticle:
    matched = [record for record in records if pmid in record.pmids]
    matched.sort(key=lambda item: _CATEGORY_PRIORITY.get(item.category, 99))
    genes = list(dict.fromkeys(gene for record in matched for gene in record.matched_genes))
    pathways = list(
        dict.fromkeys(pathway for record in matched for pathway in record.matched_pathways)
    )
    category = matched[0].category if matched else "endpoint_context"
    reason_parts = []
    if genes:
        reason_parts.append(f"contributing gene(s) {', '.join(genes)}")
    if pathways:
        reason_parts.append(f"enriched pathway(s) {', '.join(pathways)}")
    reason = (
        f"PubMed matched {' and '.join(reason_parts) or 'the endpoint context'} with "
        f"{endpoint_name} using the recorded structured query; this is supporting literature, "
        "not evidence of causality for this result."
    )
    pubdate = str(summary.get("pubdate", ""))
    year_match = re.search(r"\b(?:19|20)\d{2}\b", pubdate)
    authors = [
        str(author.get("name"))
        for author in summary.get("authors", [])
        if isinstance(author, dict) and author.get("name")
    ]
    return LiteratureArticle(
        pmid=pmid,
        title=html.unescape(str(summary.get("title") or "Untitled PubMed record")),
        authors=authors,
        journal=str(summary.get("fulljournalname") or summary.get("source") or "") or None,
        year=year_match.group(0) if year_match else None,
        abstract_excerpt=abstract,
        matched_genes=genes,
        matched_pathways=pathways,
        evidence_category=category,
        relevance_reason=reason,
        pubmed_url=f"{PUBMED_URL}/{pmid}/",
    )


@lru_cache(maxsize=1)
def get_literature_service() -> LiteratureService:
    return LiteratureService(NcbiConfig.from_env())
