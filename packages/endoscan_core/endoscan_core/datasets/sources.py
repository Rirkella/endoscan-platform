"""Data-source allow-list for dataset construction.

``registry/data/sources.yaml`` is the APPROVED, CURATED allow-list of sources the
construction pipeline may ingest from (PROJECT_RULES.md §3.1). It is intentionally
NOT the universe of all possible sources; new sources are added only by explicit
human approval. The construction tools never pull from the open web.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..registry.store import find_repo_root

SourceType = Literal["labels", "signatures", "mapping"]

#: Location of the allow-list, relative to the repository root.
SOURCES_RELPATH = Path("registry/data/sources.yaml")


class UnregisteredSourceError(Exception):
    """Raised when a tool is handed a source that is not in the allow-list."""


class Locator(BaseModel):
    """A concrete download locator for a source artifact (provenance only).

    These document WHERE staged data comes from; the construction tools never use
    them to fetch (that is the offline staging layer's job, via a human-run cloud
    workflow). Optional ``sha256`` pins the artifact for reproducibility.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    url: str
    sha256: str | None = None
    note: str | None = None


class SourceEntry(BaseModel):
    """A single approved data source."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    type: SourceType
    access_method: str
    provenance: str
    version: str
    #: Endpoint ids this source is specific to; empty = broad (applies to all).
    targets: list[str] = Field(default_factory=list)
    #: Concrete public download locators (optional, additive). Provenance only —
    #: the toolchain never fetches from these.
    locators: list[Locator] = Field(default_factory=list)

    def applies_to(self, target: str) -> bool:
        """True if this source is broad or explicitly covers ``target``."""
        return not self.targets or target in self.targets


class SourcesAllowList(BaseModel):
    """The parsed contents of ``sources.yaml``."""

    model_config = ConfigDict(extra="forbid")

    sources: list[SourceEntry] = Field(default_factory=list)

    def ids(self) -> set[str]:
        return {source.id for source in self.sources}

    def by_id(self, source_id: str) -> SourceEntry:
        for source in self.sources:
            if source.id == source_id:
                return source
        raise UnregisteredSourceError(f"Source {source_id!r} is not in the allow-list")


def load_sources(repo_root: Path | None = None) -> SourcesAllowList:
    """Load and validate the allow-list from ``registry/data/sources.yaml``."""
    root = repo_root or find_repo_root()
    path = root / SOURCES_RELPATH
    if not path.is_file():
        raise FileNotFoundError(f"Allow-list not found at {path}")
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return SourcesAllowList.model_validate(raw)


def is_allowed(source_id: str, allow_list: SourcesAllowList) -> bool:
    """True if ``source_id`` is present in the allow-list."""
    return source_id in allow_list.ids()


def require_allowed(sources: list[SourceEntry], allow_list: SourcesAllowList) -> None:
    """Raise `UnregisteredSourceError` if any source is not in the allow-list.

    Enforces PROJECT_RULES.md §3.1 at every tool boundary: construction tools
    only ever touch approved sources.
    """
    allowed = allow_list.ids()
    for source in sources:
        if source.id not in allowed:
            raise UnregisteredSourceError(
                f"Source {source.id!r} is not in the approved allow-list "
                f"(registry/data/sources.yaml)"
            )
