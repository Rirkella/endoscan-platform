"""Normalize PubChem/UniChem mapping rows to the staged ``pubchem.csv`` schema.

Pure parser over already-fetched rows; the network fetch lives in ``fetch.py``.
"""

from __future__ import annotations

from collections.abc import Sequence


def normalize_mapping(rows: Sequence[dict]) -> list[dict]:
    """Map fetched id rows to ``{input_id, input_id_type, inchikey, cid, smiles,
    mapping_confidence}`` consumed by the M2 ``compound_mapper``."""
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
