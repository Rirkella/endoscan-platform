"""Normalize PubChem/UniChem mapping rows to the staged ``pubchem.csv`` schema.

Pure parser over already-fetched rows; the network fetch lives in ``fetch.py``.
"""

from __future__ import annotations

from collections.abc import Sequence

# Common CASRN column spellings across CERAPP / EPA files (lowercased).
_CASRN_CANDIDATES = ("casrn", "casn", "cas", "cas_number", "cas_rn", "casrn_id")


def guess_casrn_column(columns: Sequence[str]) -> str | None:
    """Best-effort detect the CASRN column from a header (case-insensitive).

    Used only for the Stop-1 mapping PREVIEW; the operator confirms/overrides the
    column in the Stop-2 config cell. Returns ``None`` if no candidate matches.
    """
    lowered = {str(c).strip().lower(): c for c in columns}
    for candidate in _CASRN_CANDIDATES:
        if candidate in lowered:
            return lowered[candidate]
    return None


def normalize_mapping(rows: Sequence[dict]) -> list[dict]:
    """Map fetched id rows to ``{input_id, input_id_type, inchikey, cid, smiles,
    mapping_confidence}`` consumed by the dataset layer ``compound_mapper``."""
    out: list[dict] = []
    for row in rows:
        inchikey = row.get("inchikey") or row.get("InChIKey")
        if not row.get("input_id") or not inchikey:
            continue
        out.append(
            {
                "input_id": str(row["input_id"]),
                "input_id_type": str(row.get("input_id_type", "CASRN")),
                "inchikey": str(inchikey),
                "cid": row.get("cid"),
                "smiles": row.get("smiles") or row.get("SMILES"),
                "mapping_confidence": str(row.get("mapping_confidence", "exact")),
            }
        )
    return out
