"""Compound-identity helpers for ER staging (dependency-light, staging-only).

The chemistry identity used to join CERAPP labels to LINCS signatures is the
**InChIKey**. CERAPP CASRNs are messy free-text strings, so resolving identity by
``CASRN -> PubChem`` misses the vast majority of compounds. Every CERAPP row, however,
carries an ``InChI_Code``, and an InChI maps deterministically + offline to an
InChIKey — so that is the PRIMARY identity path here; normalized-CASRN -> PubChem is
only a FALLBACK for rows whose InChI is missing or unparseable.

RDKit (used for ``InChI -> InChIKey``) is a **staging-only** dependency installed in
the Colab runbook; it is NOT a dependency of ``endoscan_core`` and is NOT installed in
CI. It is imported lazily inside :func:`inchi_to_inchikey`, and the InChI->key seam is
unit-tested by monkeypatching that function (so CI needs no RDKit).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

# LINCS pert_info uses "-666" as its missing-value sentinel; PubChem/CERAPP may emit
# blanks or NaN. None of these are real InChIKeys and must never join.
_INCHIKEY_SENTINELS = {"", "-666", "NAN", "NONE", "NA", "NULL"}

# CASRN canonical form: 2-7 digits, 2 digits, 1 check digit (e.g. 50-00-0).
_CASRN_RE = re.compile(r"^(\d{2,7})-(\d{2})-(\d)$")


def normalize_inchikey(value: object) -> str | None:
    """Normalize an InChIKey for like-for-like joining, or ``None`` if it is a sentinel.

    Strips surrounding whitespace and upper-cases (InChIKeys are canonically
    upper-case). Returns ``None`` for empty strings, NaN, and the LINCS ``-666``
    missing-value sentinel so those rows can never silently match. Does NOT truncate:
    a full 27-char InChIKey stays full and a 14-char block-1 prefix stays a prefix, so
    callers can compare like-for-like and refuse to match full against prefix.
    """
    if value is None:
        return None
    token = str(value).strip()
    if token.upper() in _INCHIKEY_SENTINELS:
        return None
    return token.upper()


def inchi_to_inchikey(inchi: object) -> str | None:
    """Deterministically derive an InChIKey from an InChI string via RDKit (offline).

    RDKit is a STAGING-ONLY dependency (installed only in the Colab runbook, never in
    ``endoscan_core``/CI), so it is imported lazily here; if it is unavailable or the
    InChI cannot be parsed, returns ``None`` and the caller falls back to CASRN mapping.
    The InChI->key seam is unit-tested by monkeypatching THIS function.
    """
    if not inchi:
        return None
    text = str(inchi).strip()
    if not text:
        return None
    try:
        from rdkit import Chem, RDLogger  # noqa: PLC0415 — STAGING-ONLY dep (Colab), not CI
    except Exception:
        return None
    try:
        RDLogger.DisableLog("rdApp.*")
        return Chem.InchiToInchiKey(text) or None
    except Exception:
        return None


def is_valid_casrn(casrn: object) -> bool:
    """Validate a CASRN's format AND its check digit (the trailing digit).

    The check digit is ``(sum of each preceding digit * its 1-based position from the
    right) mod 10`` — so transcription errors in messy CASRN strings are rejected.
    """
    if casrn is None:
        return False
    match = _CASRN_RE.match(str(casrn).strip())
    if not match:
        return False
    body, mid, check = match.group(1), match.group(2), match.group(3)
    digits = body + mid
    total = sum(int(d) * weight for weight, d in enumerate(reversed(digits), start=1))
    return total % 10 == int(check)


def normalize_casrn_candidates(raw: object) -> list[str]:
    """Clean a messy CERAPP CASRN cell into a list of VALID candidate CASRNs.

    Handles the real-file messiness before mapping: splits comma/semicolon-separated
    lists, strips trailing count suffixes like ``"(1)"``, removes leading-zero padding,
    and keeps only candidates that pass :func:`is_valid_casrn` (check digit). Returns a
    de-duplicated list in first-seen order (possibly empty).
    """
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for piece in re.split(r"[;,]", str(raw)):
        token = re.sub(r"\s*\([^)]*\)\s*$", "", piece).strip()  # drop a trailing "(...)"
        if not token:
            continue
        token = re.sub(r"^0+(?=\d)", "", token)  # strip leading-zero pad (keep >=1 digit)
        if is_valid_casrn(token) and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def derive_inchikeys(
    label_rows: Sequence[dict],
    *,
    casrn_resolver: Callable[[str], str | None] | None = None,
    inchi_col: str = "inchi_code",
    casrn_col: str = "casrn",
) -> tuple[list[dict], dict[str, int]]:
    """Attach a clean full InChIKey to each CERAPP label row; never drop rows silently.

    PRIMARY: derive from ``row[inchi_col]`` via :func:`inchi_to_inchikey` (RDKit,
    offline, deterministic), normalized via :func:`normalize_inchikey`. FALLBACK (only
    when the InChI is missing/unparseable AND ``casrn_resolver`` is given): normalize
    the CASRN (:func:`normalize_casrn_candidates`), resolve each candidate, and collapse
    by the resulting InChIKey (a single distinct key -> use it; otherwise the
    lexicographically-first, deterministically). Rows resolved by neither path keep
    ``inchikey=None`` and are COUNTED as ``unresolved`` (never dropped).

    Returns ``(rows, stats)``. Each output row preserves the original fields (raw
    ``casrn`` + ``inchi_code`` provenance) and adds ``inchikey`` + ``inchikey_source``
    (one of ``"inchi"`` / ``"casrn_fallback"`` / ``None``). ``stats`` carries
    ``n_labels``, ``derived_from_inchi``, ``derived_from_casrn_fallback``, ``unresolved``.
    """
    rows: list[dict] = []
    stats = dict.fromkeys(
        ("n_labels", "derived_from_inchi", "derived_from_casrn_fallback", "unresolved"), 0
    )
    stats["n_labels"] = len(label_rows)
    for row in label_rows:
        inchikey = normalize_inchikey(inchi_to_inchikey(row.get(inchi_col)))
        source: str | None = None
        if inchikey is not None:
            source = "inchi"
            stats["derived_from_inchi"] += 1
        elif casrn_resolver is not None:
            resolved = sorted(
                {
                    normalize_inchikey(casrn_resolver(c))
                    for c in normalize_casrn_candidates(row.get(casrn_col))
                }
                - {None}
            )
            if resolved:
                inchikey = resolved[0]  # collapse candidates by resulting InChIKey
                source = "casrn_fallback"
                stats["derived_from_casrn_fallback"] += 1
            else:
                stats["unresolved"] += 1
        else:
            stats["unresolved"] += 1
        out = dict(row)
        out["inchikey"] = inchikey
        out["inchikey_source"] = source
        rows.append(out)
    return rows, stats
