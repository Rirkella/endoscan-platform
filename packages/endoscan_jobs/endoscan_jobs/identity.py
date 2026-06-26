"""Deterministic structure identity via RDKit (the only rdkit user in the codebase).

Derives a full standard InChIKey from an InChI (preferred) or SMILES, and collapses
(InChIKey, label) pairs to one label per structure, recording intra-structure conflicts
— the same identity contract the ER pipeline used. RDKit is a dependency of
``endoscan_jobs`` ONLY; ``endoscan_core`` never imports it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable


def inchikey_from(inchi: str | None, smiles: str | None) -> str | None:
    """Standard InChIKey from an ``InChI=`` string (preferred) or a SMILES; None if neither
    yields a valid molecule. Imported lazily so importing this module never requires rdkit
    at import time (only at call time)."""
    from rdkit import (
        Chem,  # noqa: PLC0415 - lazy: rdkit only needed when deriving identity
        RDLogger,  # noqa: PLC0415
    )
    from rdkit.Chem import inchi as rd_inchi  # noqa: PLC0415

    RDLogger.DisableLog("rdApp.*")
    mol = None
    if isinstance(inchi, str) and inchi.strip().startswith("InChI="):
        mol = rd_inchi.MolFromInchi(inchi.strip())
    if mol is None and isinstance(smiles, str) and smiles.strip():
        mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None:
        return None
    try:
        return rd_inchi.MolToInchiKey(mol)
    except Exception:  # noqa: BLE001 - an un-keyable mol is treated as unresolved
        return None


def collapse_one_label_per_structure(
    pairs: Iterable[tuple[str, int]],
) -> tuple[dict[str, int], int]:
    """Collapse ``(InChIKey, label)`` pairs to ``{InChIKey: label}`` + a conflict count.

    A structure carrying both 0 and 1 is a conflict: excluded (never silently resolved).
    """
    by_key: dict[str, set[int]] = defaultdict(set)
    for ik, label in pairs:
        by_key[ik].add(label)
    clean: dict[str, int] = {}
    conflicts = 0
    for ik, labels in by_key.items():
        if len(labels) > 1:
            conflicts += 1
            continue
        clean[ik] = next(iter(labels))
    return clean, conflicts
