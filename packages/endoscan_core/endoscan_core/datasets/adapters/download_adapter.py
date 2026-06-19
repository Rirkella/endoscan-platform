"""Real-download source adapter — STUB.

Intentionally not implemented at M2 (the milestone is fixtures-only). Real source
download/extraction adapters are wired up in a later dedicated issue. This class
exists to document the seam; it is never used by tests.
"""

from __future__ import annotations

from ..sources import SourceEntry
from .base import RawTable


class RealDownloadAdapter:
    """Placeholder for real source downloads. Every call raises."""

    def read_records(self, source: SourceEntry) -> RawTable:
        raise NotImplementedError(
            f"Real source download for {source.id!r} is not implemented yet "
            "(M2 is fixtures-only). TODO: implement real source-download adapters "
            "in a later dedicated issue."
        )
