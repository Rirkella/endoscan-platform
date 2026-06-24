"""Offline, fixture-backed source adapter (used in tests only).

Resolves a source to a bundled CSV by ``source.id`` (e.g. ``lincs`` ->
``<root>/lincs.csv``). It deliberately ignores ``access_method`` — fixtures stand
in for any real access method at M2. Missing values become ``None``.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..sources import SourceEntry
from .base import RawTable


class FixtureSourceAdapter:
    """Reads ``<root>/{source.id}.csv`` and returns rows as dicts."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def has_source(self, source: SourceEntry) -> bool:
        """True if a CSV fixture for ``source`` exists under the root."""
        return (self.root / f"{source.id}.csv").is_file()

    def read_records(self, source: SourceEntry) -> RawTable:
        path = self.root / f"{source.id}.csv"
        if not path.is_file():
            raise FileNotFoundError(f"No fixture for source {source.id!r} at {path}")
        frame = pd.read_csv(path)
        # Normalize pandas NaN -> None so downstream parsers see explicit nulls.
        cleaned = frame.astype(object).where(pd.notnull(frame), None)
        return cleaned.to_dict(orient="records")
