"""The source-access adapter seam.

Construction tools depend on this interface, never on concrete fixture files or
download logic. A fixture-backed adapter is used offline in tests; a real
download adapter is stubbed and wired up in a later dedicated issue.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..sources import SourceEntry

#: A raw, source-shaped table: one dict per row (column name -> value).
RawTable = list[dict[str, Any]]


@runtime_checkable
class SourceAdapter(Protocol):
    """Reads raw records for a single approved source.

    Implementations decide HOW to obtain the data (bundled fixture, real
    download, …); tools only consume the returned `RawTable` and interpret it
    according to ``source.type``.
    """

    def read_records(self, source: SourceEntry) -> RawTable: ...
