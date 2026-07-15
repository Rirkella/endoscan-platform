"""PubMed evidence lookup: transparent queries, bounded transport, and honest failures."""

from __future__ import annotations

import json
import threading

import pytest

from endoscan_api.literature import (
    LiteratureRateLimitError,
    LiteratureService,
    LiteratureTimeoutError,
    NcbiConfig,
    build_query_specs,
    get_literature_service,
)
from endoscan_api.schemas import LiteratureRequest


def _request(**updates) -> LiteratureRequest:
    values = {
        "endpoint_id": "ER",
        "genes": ["ESR1", "GREB1"],
        "pathways": [
            {"pathway_id": "R-HSA-1", "name": "Estrogen signaling", "genes": ["ESR1"]}
        ],
        "compound": "caffeic acid",
        "context": "MCF7 10 uM 24 h",
        "result_limit": 4,
    }
    values.update(updates)
    return LiteratureRequest(**values)


class PubmedTransport:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def __call__(self, url: str, timeout: float) -> bytes:
        self.urls.append(url)
        utility = url.rsplit("/", 1)[-1].split(".", 1)[0]
        if utility == "esearch":
            return json.dumps({"esearchresult": {"idlist": ["12345"]}}).encode()
        if utility == "esummary":
            return json.dumps(
                {
                    "result": {
                        "uids": ["12345"],
                        "12345": {
                            "title": "Estrogen receptor transcription in human cells",
                            "authors": [{"name": "Example A"}],
                            "fulljournalname": "Example Journal",
                            "pubdate": "2024 Jan",
                        },
                    }
                }
            ).encode()
        return b"""<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>12345</PMID>
          <Article><Abstract><AbstractText>Measured expression evidence.</AbstractText></Abstract>
          </Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""


def _service(transport, **config) -> LiteratureService:
    return LiteratureService(
        NcbiConfig(api_key=None, tool="endoscan-tests", email="tests@example.org", **config),
        transport=transport,
        sleep=lambda _: None,
    )


def test_success_records_queries_provenance_and_noncausal_reason() -> None:
    transport = PubmedTransport()
    result = _service(transport).lookup(_request(), "Estrogen receptor")

    assert result.status == "ok"
    assert result.articles[0].pmid == "12345"
    assert result.articles[0].pubmed_url == "https://pubmed.ncbi.nlm.nih.gov/12345/"
    assert "not evidence of causality" in result.articles[0].relevance_reason
    assert result.articles[0].abstract_excerpt == "Measured expression evidence."
    assert result.queries and all(record.query for record in result.queries)
    assert result.provenance.provider == "NCBI PubMed E-utilities"
    assert result.provenance.email_configured is True
    assert any(
        "tool=endoscan-tests" in url and "email=tests%40example.org" in url
        for url in transport.urls
    )
    assert transport.urls[-2].find("esummary.fcgi") > 0
    assert transport.urls[-1].find("efetch.fcgi") > 0


def test_empty_is_cached_and_second_response_marks_cache_hit() -> None:
    calls: list[str] = []

    def empty(url: str, timeout: float) -> bytes:
        calls.append(url)
        return b'{"esearchresult":{"idlist":[]}}'

    service = _service(empty)
    first = service.lookup(_request(genes=["ESR1"], pathways=[], compound=None), "ER")
    call_count = len(calls)
    second = service.lookup(_request(genes=["ESR1"], pathways=[], compound=None), "ER")
    assert first.status == "empty" and second.status == "empty"
    assert len(calls) == call_count
    assert first.provenance.cache_hit is False and second.provenance.cache_hit is True


def test_query_builder_sanitizes_fields_and_is_bounded() -> None:
    body = _request(
        genes=[f"G{i}" for i in range(20)],
        pathways=[
            {"pathway_id": f"P{i}", "name": f"Path {i}", "genes": [f"G{i}"]}
            for i in range(10)
        ],
        context='MCF7 [unsafe] "quoted"',
    )
    specs = build_query_specs(body, 'Estrogen receptor [unsafe]')
    assert len(specs) <= 10
    assert all("[unsafe]" not in spec.query and '"quoted"' not in spec.query for spec in specs)


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (LiteratureRateLimitError("limit"), "rate_limited"),
        (LiteratureTimeoutError("slow"), "timeout"),
    ],
)
def test_route_returns_structured_external_failure(client, raised, expected) -> None:
    service = _service(lambda url, timeout: (_ for _ in ()).throw(raised))
    client.app.dependency_overrides[get_literature_service] = lambda: service
    try:
        response = client.post("/interpret/literature", json=_request().model_dump(mode="json"))
    finally:
        client.app.dependency_overrides.pop(get_literature_service, None)
    assert response.status_code == 200
    assert response.json()["status"] == expected
    assert response.json()["articles"] == []
    assert response.json()["queries"]


def test_missing_contact_metadata_is_honest_unavailable(client) -> None:
    service = LiteratureService(NcbiConfig(api_key=None, tool="test", email=None))
    client.app.dependency_overrides[get_literature_service] = lambda: service
    try:
        response = client.post("/interpret/literature", json=_request().model_dump(mode="json"))
    finally:
        client.app.dependency_overrides.pop(get_literature_service, None)
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert "not configured" in response.json()["reason"]


def test_request_rate_is_three_per_second_without_key() -> None:
    clock = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    service = LiteratureService(
        NcbiConfig(api_key=None, tool="test", email="x@y.test"),
        transport=lambda url, timeout: b"{}",
        monotonic=lambda: clock[0],
        sleep=sleep,
    )
    service._request("esearch", {"db": "pubmed"})
    service._request("esearch", {"db": "pubmed"})
    assert sleeps == [pytest.approx(1 / 3)]


def test_concurrent_identical_lookup_is_single_flight() -> None:
    transport = PubmedTransport()
    entered = threading.Event()
    release = threading.Event()

    def blocking(url: str, timeout: float) -> bytes:
        if not entered.is_set():
            entered.set()
            release.wait(2)
        return transport(url, timeout)

    service = _service(blocking, timeout_seconds=2)
    results = []
    first = threading.Thread(target=lambda: results.append(service.lookup(_request(), "ER")))
    second = threading.Thread(target=lambda: results.append(service.lookup(_request(), "ER")))
    first.start()
    assert entered.wait(1)
    second.start()
    release.set()
    first.join(3)
    second.join(3)
    assert len(results) == 2
    assert len(transport.urls) == len(build_query_specs(_request(), "ER")) + 2
    assert sum(result.provenance.cache_hit for result in results) == 1
