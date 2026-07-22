"""Legacy direct-download seam — deliberately disabled.

Reviewed source access now lives in ``endoscan_workflows`` behind typed provider,
approval, provenance, and artifact boundaries. Core dataset construction must not
bypass those controls, so this compatibility class always raises.
"""

from __future__ import annotations

from ..sources import SourceEntry
from .base import RawTable


class RealDownloadAdapter:
    """Disabled compatibility seam for ungoverned direct downloads."""

    def has_source(self, source: SourceEntry) -> bool:
        raise NotImplementedError(
            f"Direct source download for {source.id!r} is disabled; "
            "use the reviewed endoscan_workflows provider boundary."
        )

    def read_records(self, source: SourceEntry) -> RawTable:
        raise NotImplementedError(
            f"Direct source download for {source.id!r} is disabled; "
            "use the reviewed endoscan_workflows provider and artifact boundary."
        )
