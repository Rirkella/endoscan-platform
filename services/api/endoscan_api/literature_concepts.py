"""Versioned endpoint concepts used to form unambiguous PubMed queries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CONCEPTS_RELPATH = Path("data/literature/endpoint_concepts.v1.json")


@dataclass(frozen=True)
class EndpointLiteratureConcept:
    endpoint_id: str
    preferred_name: str
    search_terms: tuple[str, ...]
    excluded_ambiguous_terms: tuple[str, ...]
    configuration_version: str


@lru_cache(maxsize=4)
def load_endpoint_concept(
    repo_root: Path, endpoint_id: str, endpoint_name: str
) -> EndpointLiteratureConcept:
    path = repo_root / CONCEPTS_RELPATH
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        document = {}
    entry = document.get("endpoints", {}).get(endpoint_id)
    if isinstance(entry, dict) and entry.get("search_terms"):
        return EndpointLiteratureConcept(
            endpoint_id=endpoint_id,
            preferred_name=str(entry.get("preferred_name") or endpoint_name),
            search_terms=tuple(str(value) for value in entry["search_terms"]),
            excluded_ambiguous_terms=tuple(
                str(value) for value in entry.get("excluded_ambiguous_terms", [])
            ),
            configuration_version=str(document.get("configuration_version", "unknown")),
        )
    # Safe generic fallback: the full registered biological target only, never an endpoint ID.
    return EndpointLiteratureConcept(
        endpoint_id=endpoint_id,
        preferred_name=endpoint_name,
        search_terms=(endpoint_name,),
        excluded_ambiguous_terms=(endpoint_id,),
        configuration_version=str(document.get("configuration_version", "generic-fallback")),
    )
