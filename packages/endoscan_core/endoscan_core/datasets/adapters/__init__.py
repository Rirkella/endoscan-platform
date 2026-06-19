"""Source-access adapters (the seam between tools and source data)."""

from .base import RawTable, SourceAdapter
from .download_adapter import RealDownloadAdapter
from .fixture_adapter import FixtureSourceAdapter

__all__ = [
    "RawTable",
    "SourceAdapter",
    "FixtureSourceAdapter",
    "RealDownloadAdapter",
]
