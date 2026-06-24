"""Staged-data source adapter (reads local extracts; no live downloads).

Resolves a source to a local file under a staged-data directory by ``source.id``,
reading **Parquet** (the wide LINCS signature matrix) or **CSV** (narrow
label/mapping tables). Parquet is preferred when both exist. This is the adapter
used for the one-time real ER run (Phase 2); the data is staged locally by a human
(e.g. converted from a LINCS ``.gctx`` — see ``docs/er_real_data_phase2.md``).

It implements the same `SourceAdapter` interface as the fixture adapter, so the
M2 tools consume it unchanged. ``RealDownloadAdapter`` remains a stub — nothing
here fetches from the network.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..sources import SourceEntry
from .base import RawTable

#: File extensions tried, in priority order.
_EXTENSIONS = (".parquet", ".csv")


class StagedSourceAdapter:
    """Reads ``<root>/{source.id}.parquet`` or ``.csv`` and returns rows as dicts."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def has_source(self, source: SourceEntry) -> bool:
        """True if a Parquet or CSV extract for ``source`` exists under the root."""
        return any((self.root / f"{source.id}{ext}").is_file() for ext in _EXTENSIONS)

    def read_records(self, source: SourceEntry) -> RawTable:
        for ext in _EXTENSIONS:
            path = self.root / f"{source.id}{ext}"
            if path.is_file():
                frame = pd.read_parquet(path) if ext == ".parquet" else pd.read_csv(path)
                # Normalize pandas NaN -> None so downstream parsers see explicit nulls.
                cleaned = frame.astype(object).where(pd.notnull(frame), None)
                return cleaned.to_dict(orient="records")
        tried = ", ".join(f"{source.id}{ext}" for ext in _EXTENSIONS)
        raise FileNotFoundError(
            f"No staged extract for source {source.id!r} under {self.root} (tried: {tried})"
        )
