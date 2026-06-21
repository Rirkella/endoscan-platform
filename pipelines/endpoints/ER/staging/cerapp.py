"""Parse CERAPP EXPERIMENTAL ER activity calls into label rows.

CRITICAL (documented in the dataset card): EndoScan trains on CERAPP's
**experimental / measured** ER activity calls (the training + evaluation sets),
NOT the consensus-model PREDICTIONS for the ~32k chemicals. Training on predicted
labels would make EndoScan mimic a structure-based QSAR model and violate the
transcriptomics-first thesis.

This is a pure parser over already-fetched rows (list of dicts). Column names of
the real CERAPP files are FLAGGED for confirmation; the parser accepts the
expected columns and is configurable.
"""

from __future__ import annotations

from collections.abc import Sequence

_ACTIVE = {"active", "binding", "agonist", "antagonist", "1", "1.0", "positive", "yes"}
_INACTIVE = {"inactive", "non-binding", "nonbinding", "0", "0.0", "negative", "no"}


def _to_label(raw: object) -> int | None:
    token = str(raw).strip().lower()
    if token in _ACTIVE:
        return 1
    if token in _INACTIVE:
        return 0
    return None  # unknown/ambiguous -> dropped (recorded by the caller if needed)


def parse_cerapp_experimental(
    rows: Sequence[dict],
    *,
    activity_col: str,
    casrn_col: str = "casrn",
    target: str = "ER",
) -> list[dict]:
    """Map experimental CERAPP rows to normalized label records.

    Output rows match the staged ``cerapp.csv`` label schema consumed by the M2
    ``label_retriever`` (compound id = CASRN; ``consensus_call`` carries the
    experimental call so the existing parser path is reused).
    """
    out: list[dict] = []
    for row in rows:
        label = _to_label(row.get(activity_col))
        if label is None or not row.get(casrn_col):
            continue
        out.append(
            {
                "casrn": str(row[casrn_col]),
                "chemical_name": row.get("chemical_name") or row.get("name"),
                "inchikey": row.get("inchikey") or row.get("InChIKey"),
                "target": target,
                "consensus_call": "active" if label == 1 else "inactive",
                "consensus_score": row.get("consensus_score"),
                "label_provenance": "cerapp_experimental",
            }
        )
    return out
