"""Select candidate sources for a target — from the allow-list only."""

from __future__ import annotations

from collections.abc import Iterable

from .sources import SourceEntry, SourcesAllowList, SourceType


def source_selector(
    target: str,
    allow_list: SourcesAllowList,
    types: Iterable[SourceType] | None = None,
) -> list[SourceEntry]:
    """Return approved sources applicable to ``target``.

    A source is returned when it is broad (no ``targets``) or explicitly covers
    ``target``, optionally filtered to the given ``types``. Because it only
    iterates the allow-list, it can never return a source outside it
    (PROJECT_RULES.md §3.1).
    """
    wanted = set(types) if types is not None else None
    selected: list[SourceEntry] = []
    for source in allow_list.sources:
        if wanted is not None and source.type not in wanted:
            continue
        if not source.applies_to(target):
            continue
        selected.append(source)
    return selected
