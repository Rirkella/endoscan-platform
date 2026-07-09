"""Read the committed Reactome gene-set artifact (read-only; NO fabrication).

The real artifact is produced by ``scripts/extract_reactome_pathways.py`` from the REAL
Reactome download (recording version/source/date/license) and transferred into
``data/reactome/reactome_pathways.json`` (checksum-verified, like the Explore artifacts).
When it is absent, the API returns an honest "pathway data not available" state upstream —
this module simply raises; it never invents pathways.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .pathways import Pathway

#: Committed artifact location the API reads (relative to repo_root). Endpoint-agnostic.
REACTOME_PATH = ("data", "reactome", "reactome_pathways.json")

#: Provenance keys surfaced in Technical details (recorded at extraction, never assumed).
_PROVENANCE_KEYS = (
    "reactome_version",
    "source_url",
    "retrieval_date",
    "license",
    "n_pathways",
    "n_genes",
    "is_fixture",
)


class ReactomeUnavailableError(FileNotFoundError):
    """No committed Reactome artifact -> the pathways endpoint returns a graceful empty state."""


@dataclass(frozen=True)
class ReactomeData:
    pathways: list[Pathway]
    provenance: dict


def _artifact_path(repo_root: Path) -> Path:
    return repo_root.joinpath(*REACTOME_PATH)


def load_reactome(repo_root: Path) -> ReactomeData:
    """Load the committed Reactome pathways or raise ``ReactomeUnavailableError``."""
    path = _artifact_path(repo_root)
    if not path.is_file():
        raise ReactomeUnavailableError("pathway data not available")
    doc = json.loads(path.read_text(encoding="utf-8"))
    pathways = [
        Pathway(
            id=str(p["id"]),
            name=str(p["name"]),
            genes=frozenset(str(g) for g in p.get("genes", [])),
            description=p.get("description"),
        )
        for p in doc.get("pathways", [])
    ]
    provenance = {k: doc.get(k) for k in _PROVENANCE_KEYS if k in doc}
    return ReactomeData(pathways=pathways, provenance=provenance)
