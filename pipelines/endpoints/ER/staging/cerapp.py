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

    Output rows match the staged ``cerapp.csv`` label schema consumed by the dataset layer
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


def assemble_evaluation_labels(
    rows: Sequence[dict],
    *,
    casrn_col: str = "CASRN",
    assay_class_col: str = "ASSAY_CLASS_NAME",
    active_col: str = "All_active",
    inchi_col: str = "InChI_Code",
    mode_col: str = "Mode",
    label_mode: str = "binding",
    target: str = "ER",
) -> tuple[list[dict], list[dict]]:
    """Collapse the assay-level CERAPP evaluation set to ONE ER label per compound.

    The real ``Supplemental_Material_4_evaluationSet.xlsx`` is assay-level (many rows
    per CASRN), so it must be collapsed. Parameterized only by column names + a mode
    (nothing hardcoded). Two different selection axes exist in the file and the mode
    picks which one:

    - ``label_mode="binding"``: the assay-TECHNOLOGY axis — keep only rows whose
      ``assay_class_col`` contains "binding" (case-insensitive), i.e. genuine
      binding-assay rows.
    - ``label_mode="agonist"`` / ``"antagonist"``: the CERAPP ENDPOINT axis — keep rows
      whose ``mode_col`` equals "Agonist" / "Antagonist" (case-insensitive EXACT match
      on the mode value, NOT a substring of the assay class). Single endpoint only
      (never fused across modes).
    - ``label_mode="functional"``: the ENDPOINT UNION — keep rows whose ``mode_col`` is
      "Agonist" OR "Antagonist" (ER transactivation-pathway modulation; direction is
      NOT distinguished). Positive = active in ANY functional row; negative = tested in
      those modes and inactive in all. A cross-mode disagreement (agonist-active +
      antagonist-inactive) is POSITIVE under the union, NOT a conflict; only a genuine
      INTRA-mode ``{0,1}`` contradiction is excluded.
    - ``label_mode="any"``: keep all rows (no axis filter).
    - rows with a non-0/1 (NaN/blank) ``active_col`` are dropped.
    - collapse per CASRN: all kept rows inactive -> label 0; >=1 active AND no
      disagreement -> label 1; rows that DISAGREE -> **CONFLICT: excluded** (never
      majority-voted) and returned separately.

    Returns ``(label_rows, conflicts)``. ``label_rows`` carry CASRN through to the
    existing PubChem/UniChem ``CASRN -> InChIKey`` mapping path (``inchikey`` is left
    ``None`` here; the InChI string is carried in ``inchi_code`` for provenance only),
    and include ``consensus_call`` (so the unchanged ``label_retriever`` parser path is
    reused) plus an explicit ``label`` and ``label_provenance``. ``conflicts`` lists the
    excluded compounds with their disagreeing values + row count.
    """
    if label_mode not in ("binding", "agonist", "antagonist", "functional", "any"):
        raise ValueError(
            "label_mode must be 'binding', 'agonist', 'antagonist', 'functional', or "
            f"'any', got {label_mode!r}"
        )

    # Endpoint-axis modes match the Mode column value exactly (case-insensitive).
    _mode_value = {"agonist": "agonist", "antagonist": "antagonist"}.get(label_mode)
    _functional = label_mode == "functional"  # union of {agonist, antagonist} endpoints
    _functional_modes = {"agonist", "antagonist"}

    # Per-CASRN: collect kept 0/1 labels and a representative InChI string (stable order).
    # ``functional`` is resolved separately (per-CASRN, per-mode) after the loop, so it
    # can treat cross-mode disagreement as positive (union) while still excluding a
    # genuine intra-mode contradiction.
    per_casrn: dict[str, dict] = {}
    func_tmp: dict[str, dict] = {}  # casrn -> {"agonist": set, "antagonist": set, "inchi": str}
    for row in rows:
        casrn = str(row.get(casrn_col) or "").strip()
        if not casrn:
            continue
        row_mode = ""
        if label_mode == "binding":
            # Assay-TECHNOLOGY axis: assay-class contains "binding".
            assay_class = str(row.get(assay_class_col) or "").lower()
            if "binding" not in assay_class:
                continue
        elif _mode_value is not None:
            # CERAPP ENDPOINT axis: exact Mode value (agonist | antagonist).
            if str(row.get(mode_col) or "").strip().lower() != _mode_value:
                continue
        elif _functional:
            # ENDPOINT union: keep Agonist OR Antagonist rows (exact value, not substring).
            row_mode = str(row.get(mode_col) or "").strip().lower()
            if row_mode not in _functional_modes:
                continue
        label = _to_label(row.get(active_col))
        if label is None:  # NaN / non-0-1 -> dropped
            continue
        if _functional:
            entry = func_tmp.setdefault(casrn, {"inchi": None})
            entry.setdefault(row_mode, set()).add(label)
            if entry["inchi"] is None and row.get(inchi_col):
                entry["inchi"] = str(row[inchi_col])
            continue
        entry = per_casrn.setdefault(casrn, {"labels": set(), "inchi": None})
        entry["labels"].add(label)
        if entry["inchi"] is None and row.get(inchi_col):
            entry["inchi"] = str(row[inchi_col])

    if _functional:
        # Resolve each compound's functional label from its per-mode label sets:
        # - any single mode internally contradicts ({0,1}) -> genuine contradiction,
        #   resolve to {0,1} so the collapse loop below EXCLUDES it (never voted);
        # - else union: positive if active in ANY functional mode, negative otherwise.
        #   (agonist-active + antagonist-inactive -> positive, NOT a conflict.)
        for casrn, entry in func_tmp.items():
            mode_sets = [v for k, v in entry.items() if k != "inchi"]
            if any(s == {0, 1} for s in mode_sets):
                resolved = {0, 1}
            elif any(1 in s for s in mode_sets):
                resolved = {1}
            else:
                resolved = {0}
            per_casrn[casrn] = {"labels": resolved, "inchi": entry["inchi"]}

    label_rows: list[dict] = []
    conflicts: list[dict] = []
    for casrn in sorted(per_casrn):
        labels = per_casrn[casrn]["labels"]
        if labels == {0, 1}:  # disagreement -> exclude, do NOT vote
            conflicts.append({"casrn": casrn, "labels": [0, 1], "n_label_values": 2})
            continue
        label = 1 if labels == {1} else 0  # {1} -> 1; {0} -> 0
        label_rows.append(
            {
                "casrn": casrn,
                "inchikey": None,  # resolved via the PubChem CASRN->InChIKey staging path
                "inchi_code": per_casrn[casrn]["inchi"],
                "target": target,
                "consensus_call": "active" if label == 1 else "inactive",
                "consensus_score": None,
                "label": label,
                "label_provenance": f"cerapp_experimental_{label_mode}",
            }
        )
    return label_rows, conflicts
