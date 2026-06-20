"""Source-access adapters (the seam between tools and source data)."""

from .base import RawTable, SourceAdapter
from .download_adapter import RealDownloadAdapter
from .fixture_adapter import FixtureSourceAdapter
from .staged_adapter import StagedSourceAdapter

__all__ = [
    "RawTable",
    "SourceAdapter",
    "FixtureSourceAdapter",
    "StagedSourceAdapter",
    "RealDownloadAdapter",
]
