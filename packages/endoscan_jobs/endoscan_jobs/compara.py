"""Parse a CoMPARA EXPERIMENTAL archive and extract the MEASURED AR activity labels.

CONFIRMED real format (server inspection of figshare article 10321697 ``Data.zip``): the
measured data is in **SDF files**, not tables. ``Data.zip`` contains a nested
``Data/AR_data.zip`` which holds three SDFs — ``ToxCast_AR_Binding.sdf``,
``ToxCast_AR_Agonist.sdf``, ``ToxCast_AR_Antagonist.sdf``. Each molecule carries SD
fields (exact names): ``casrn``, ``cid``, ``gsid``, ``dsstox_substance_id``,
``preferred_name``, ``Canonical_QSARr``, ``Salt_Solvent``, ``InChI_Code_QSARr``,
``InChI Key_QSARr`` (NOTE the literal space), ``AUC.<Mode>``, ``<Mode>Class``,
``AC50_Calculated`` — where ``<Mode>`` ∈ {Binding, Agonist, Antagonist}. The MEASURED
label is ``<Mode>Class`` ONLY (float ``1.0`` = active, ``0.0`` = inactive); never AUC/AC50.

Modes (mirroring the ER functional-mode logic EXACTLY): ``binding`` / ``agonist`` /
``antagonist`` are single ``<Mode>Class`` fields; ``functional_modulation`` is
``agonist ∪ antagonist`` (binding EXCLUDED — binding is receptor attachment, agonist/
antagonist are functional pathway modulation, a different biological claim);
``broad_any_activity`` is ``binding ∪ agonist ∪ antagonist`` and is DIAGNOSTIC-ONLY
(scientifically weaker — mixes binding with function — and never a registered endpoint).

A legacy TABLE path (``extract_tables`` / ``select_measured_ar_call``) is retained as a
dispatch fallback. RDKit is used ONLY here (in ``endoscan_jobs``), never in
``endoscan_core``, and is imported lazily inside the SDF reader.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import NamedTuple

import pandas as pd
from pydantic import BaseModel, ConfigDict

from .identity import collapse_one_label_per_structure

RE_PRED = re.compile(r"pred|consensus|qsar|model|score|prob|dock|_p_|applicab", re.I)
RE_CALL = re.compile(r"bind|agonist|antagonist|activ|call|class|outcome", re.I)
RE_STRUCT_INCHI = re.compile(r"inchi(?!key)", re.I)
RE_STRUCT_SMILES = re.compile(r"smiles|canonical", re.I)

_ACTIVE = {"active", "1", "1.0", "true", "positive", "agonist", "antagonist", "binder", "yes"}
_INACTIVE = {"inactive", "0", "0.0", "false", "negative", "non-binder", "no"}
_TABULAR_SUFFIXES = (".csv", ".tsv", ".txt", ".tab", ".xlsx", ".xls")


class NoMeasuredCallError(RuntimeError):
    """Raised when no measured (non-predicted) binary AR call column is found."""


class ComparaSelection(BaseModel):
    """The auto-detected measured experimental AR table + columns (auditable)."""

    model_config = ConfigDict(extra="forbid")

    table_name: str
    struct_inchi_col: str | None
    struct_smiles_col: str | None
    call_col: str
    value_distribution: dict[str, int]


def to_binary(value: object) -> int | None:
    """Map a measured activity token to 1 (active) / 0 (inactive) / None (drop)."""
    s = str(value).strip().lower()
    if s in _ACTIVE:
        return 1
    if s in _INACTIVE:
        return 0
    return None


def _read_one(name: str, data: bytes) -> list[tuple[str, pd.DataFrame]]:
    low = name.lower()
    try:
        if low.endswith(".csv"):
            return [(name, pd.read_csv(io.BytesIO(data), low_memory=False))]
        if low.endswith((".tsv", ".txt", ".tab")):
            return [
                (name, pd.read_csv(io.BytesIO(data), sep=None, engine="python", low_memory=False))
            ]
        if low.endswith((".xlsx", ".xls")):
            sheets = pd.read_excel(io.BytesIO(data), sheet_name=None)
            return [(f"{name}::{s}", df) for s, df in sheets.items()]
    except Exception:  # noqa: BLE001 - a malformed inner file is skipped, not fatal
        return []
    return []


def extract_tables(source: Path | str | bytes) -> list[tuple[str, pd.DataFrame]]:
    """Recursively extract tabular tables from a CoMPARA zip (path or raw bytes).

    Nested ``.zip`` entries are descended into; ``.xlsx`` is read as (possibly multi-)
    sheet tables, not unzipped. Files whose NAME looks predicted/consensus are skipped.
    """
    raw = source if isinstance(source, bytes) else Path(source).read_bytes()
    tables: list[tuple[str, pd.DataFrame]] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if RE_PRED.search(Path(name).name):
                continue
            data = zf.read(info)
            low = name.lower()
            if low.endswith(".zip"):
                tables.extend(extract_tables(data))
            elif low.endswith(_TABULAR_SUFFIXES):
                tables.extend(_read_one(Path(name).name, data))
    return tables


def _struct_cols(cols: list[str]) -> tuple[str | None, str | None]:
    inchi = [c for c in cols if RE_STRUCT_INCHI.search(str(c))]
    smiles = [c for c in cols if RE_STRUCT_SMILES.search(str(c))]
    return (inchi[0] if inchi else None), (smiles[0] if smiles else None)


def _candidate_call_cols(cols: list[str]) -> list[str]:
    cands = [c for c in cols if RE_CALL.search(str(c)) and not RE_PRED.search(str(c))]

    def rank(c: str) -> tuple[int, int]:
        s = str(c).lower()
        family = 0 if "bind" in s else 1 if ("agonist" in s or "antagonist" in s) else 2
        return (family, len(s))

    return sorted(cands, key=rank)


def _looks_binary(series: pd.Series) -> bool:
    toks = set(series.astype(str).str.strip().str.lower().unique()) - {"nan", "", "na", "none", "-"}
    return bool(toks & _ACTIVE) and bool(toks & _INACTIVE)


def select_measured_ar_call(
    tables: list[tuple[str, pd.DataFrame]],
    *,
    force_table: str | None = None,
    force_call_col: str | None = None,
    force_struct_col: str | None = None,
) -> tuple[ComparaSelection, pd.DataFrame]:
    """Pick the measured AR table + (struct, call) columns; exclude predicted columns.

    Raises :class:`NoMeasuredCallError` if no table has a structure column and a binary,
    non-predicted AR call column.
    """
    for name, df in tables:
        if force_table and force_table not in name:
            continue
        cols = [str(c) for c in df.columns]
        inchi_c, smiles_c = _struct_cols(cols)
        if force_struct_col and force_struct_col in cols:
            inchi_c = force_struct_col
        if not (inchi_c or smiles_c):
            continue
        call_cols = (
            [force_call_col]
            if (force_call_col and force_call_col in cols)
            else _candidate_call_cols(cols)
        )
        for c in call_cols:
            if c in df.columns and not RE_PRED.search(str(c)) and _looks_binary(df[c]):
                dist = {
                    str(k): int(v)
                    for k, v in df[c].astype(str).str.lower().value_counts().head(8).items()
                }
                return (
                    ComparaSelection(
                        table_name=name,
                        struct_inchi_col=inchi_c,
                        struct_smiles_col=smiles_c,
                        call_col=c,
                        value_distribution=dist,
                    ),
                    df,
                )
    raise NoMeasuredCallError(
        "no measured (non-predicted) binary AR call column found in the CoMPARA archive; "
        f"tables inspected: {[n for n, _ in tables]}"
    )


# ---------------------------------------------------------------------- SDF (real format)

#: Prediction files inside the archive — NEVER read as labels (predictions, not measured).
RE_PRED_FILE = re.compile(r"predset|qsar[-_ ]?ready|prediction", re.I)

#: The five coverage modes. ``functional_modulation`` is the MAIN endpoint.
MODES = ("binding", "agonist", "antagonist", "functional_modulation", "broad_any_activity")
#: broad_any_activity mixes binding with function — a diagnostic, never a registered endpoint.
DIAGNOSTIC_ONLY_MODES = frozenset({"broad_any_activity"})
_FUNCTIONAL_MODES = ("agonist", "antagonist")  # functional_modulation = agonist ∪ antagonist
_BROAD_MODES = ("binding", "agonist", "antagonist")
#: Each single-mode SDF carries its measured label in this exact SD field.
_CLASS_FIELD = {
    "binding": "BindingClass",
    "agonist": "AgonistClass",
    "antagonist": "AntagonistClass",
}


class SdfRecord(NamedTuple):
    """One measured molecule from a single-mode SDF."""

    inchikey: str | None
    label: int | None  # from <Mode>Class: 1.0 -> 1, 0.0 -> 0, else None
    casrn: str | None
    dsstox: str | None
    source: str


class SdfModeResult(BaseModel):
    """The measured labels for a requested mode (after collapse + union)."""

    model_config = ConfigDict(extra="forbid")

    mode: str
    diagnostic_only: bool
    labels: dict[str, int]  # InChIKey -> 0/1
    n_positive: int
    n_negative: int
    n_conflicts: int
    source_sdfs: list[str]


def _class_to_binary(value: object) -> int | None:
    """Map a ``<Mode>Class`` value to 1 (active) / 0 (inactive) / None. Strictly the class
    field — float ``1.0``/``0.0``; anything else (blank, AUC text) drops."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f == 1.0:
        return 1
    if f == 0.0:
        return 0
    return None


def _mode_from_filename(name: str) -> str | None:
    low = Path(name).name.lower()
    if "antagonist" in low:  # check antagonist BEFORE agonist (substring)
        return "antagonist"
    if "agonist" in low:
        return "agonist"
    if "binding" in low:
        return "binding"
    return None


def _collect_sdf_blobs(raw: bytes, depth: int = 0) -> dict[str, bytes]:
    """Recurse the (possibly nested) archive and return ``{sdf_filename: bytes}``.

    Descends into nested ``.zip`` (Data.zip -> Data/AR_data.zip -> *.sdf) and EXCLUDES
    prediction files by name (``Predset_*`` / ``*_QSAR-ready_*``)."""
    out: dict[str, bytes] = {}
    if depth > 4:
        return out
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            base = Path(info.filename).name
            if RE_PRED_FILE.search(base):  # never read predictions as labels
                continue
            data = zf.read(info)
            low = info.filename.lower()
            if low.endswith(".zip"):
                out.update(_collect_sdf_blobs(data, depth + 1))
            elif low.endswith(".sdf"):
                out[base] = data
    return out


def is_sdf_archive(source: Path | str | bytes) -> bool:
    """True if the archive contains SDF(s) (the real CoMPARA measured format)."""
    raw = source if isinstance(source, bytes) else Path(source).read_bytes()
    try:
        return bool(_collect_sdf_blobs(raw))
    except zipfile.BadZipFile:
        return False


def _read_sdf(data: bytes, class_field: str, source: str) -> list[SdfRecord]:
    """Read one single-mode SDF; InChIKey from the mol block (fallback InChI_Code_QSARr,
    then the precomputed ``InChI Key_QSARr`` field). RDKit imported lazily here only."""
    from rdkit import (
        Chem,  # noqa: PLC0415 - rdkit lives in endoscan_jobs, lazy at call time
        RDLogger,  # noqa: PLC0415
    )
    from rdkit.Chem import inchi as rd_inchi  # noqa: PLC0415

    RDLogger.DisableLog("rdApp.*")
    records: list[SdfRecord] = []
    for mol in Chem.ForwardSDMolSupplier(io.BytesIO(data)):
        if mol is None:
            continue
        props = mol.GetPropsAsDict()
        label = _class_to_binary(props.get(class_field))
        inchikey: str | None = None
        try:
            inchikey = rd_inchi.MolToInchiKey(mol) or None
        except Exception:  # noqa: BLE001 - fall back to the InChI field below
            inchikey = None
        if not inchikey:
            code = props.get("InChI_Code_QSARr")
            if isinstance(code, str) and code.startswith("InChI="):
                m2 = rd_inchi.MolFromInchi(code)
                if m2 is not None:
                    try:
                        inchikey = rd_inchi.MolToInchiKey(m2) or None
                    except Exception:  # noqa: BLE001
                        inchikey = None
        if not inchikey:
            pre = props.get("InChI Key_QSARr")  # literal space in the field name
            inchikey = str(pre).strip().upper() if pre else None
        casrn = str(props["casrn"]) if props.get("casrn") is not None else None
        dsstox = (
            str(props["dsstox_substance_id"])
            if props.get("dsstox_substance_id") is not None
            else None
        )
        records.append(
            SdfRecord(inchikey=inchikey, label=label, casrn=casrn, dsstox=dsstox, source=source)
        )
    return records


def extract_sdf_records(source: Path | str | bytes) -> dict[str, list[SdfRecord]]:
    """Extract per-mode measured records from the nested CoMPARA archive.

    Returns ``{"binding": [...], "agonist": [...], "antagonist": [...]}`` (prediction
    files excluded by name).
    """
    raw = source if isinstance(source, bytes) else Path(source).read_bytes()
    out: dict[str, list[SdfRecord]] = {"binding": [], "agonist": [], "antagonist": []}
    for fname, data in _collect_sdf_blobs(raw).items():
        mode = _mode_from_filename(fname)
        if mode is None:
            continue
        out[mode].extend(_read_sdf(data, _CLASS_FIELD[mode], fname))
    return out


def _collapse(records: list[SdfRecord]) -> tuple[dict[str, int], int]:
    """Intra-mode collapse to one label per InChIKey; a {0,1} disagreement is a conflict
    (excluded, recorded) — identical to the ER per-structure rule."""
    return collapse_one_label_per_structure(
        (r.inchikey, r.label) for r in records if r.inchikey and r.label is not None
    )


def _union(*resolved: dict[str, int]) -> dict[str, int]:
    """Union over already-collapsed per-mode label maps: a structure is POSITIVE if active
    (1) in ANY constituent mode, else NEGATIVE (it was tested in >=1 mode and inactive).
    A cross-mode 1-vs-0 is a union positive, NOT a conflict."""
    keys: set[str] = set().union(*[set(d) for d in resolved]) if resolved else set()
    out: dict[str, int] = {}
    for k in keys:
        out[k] = 1 if any(d.get(k) == 1 for d in resolved) else 0
    return out


def labels_for_mode(records_by_mode: dict[str, list[SdfRecord]], mode: str) -> SdfModeResult:
    """Apply the mode rule to per-mode records and return the measured label map.

    ``functional_modulation`` = agonist ∪ antagonist (binding EXCLUDED);
    ``broad_any_activity`` = binding ∪ agonist ∪ antagonist (DIAGNOSTIC-ONLY).
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; one of {MODES}")

    if mode in ("binding", "agonist", "antagonist"):
        labels, conflicts = _collapse(records_by_mode.get(mode, []))
        used = (mode,)
    elif mode == "functional_modulation":
        per = {m: _collapse(records_by_mode.get(m, [])) for m in _FUNCTIONAL_MODES}
        labels = _union(*[per[m][0] for m in _FUNCTIONAL_MODES])
        conflicts = sum(per[m][1] for m in _FUNCTIONAL_MODES)
        used = _FUNCTIONAL_MODES
    else:  # broad_any_activity
        per = {m: _collapse(records_by_mode.get(m, [])) for m in _BROAD_MODES}
        labels = _union(*[per[m][0] for m in _BROAD_MODES])
        conflicts = sum(per[m][1] for m in _BROAD_MODES)
        used = _BROAD_MODES

    sources = sorted({r.source for m in used for r in records_by_mode.get(m, []) if r.source})
    return SdfModeResult(
        mode=mode,
        diagnostic_only=mode in DIAGNOSTIC_ONLY_MODES,
        labels=labels,
        n_positive=sum(1 for v in labels.values() if v == 1),
        n_negative=sum(1 for v in labels.values() if v == 0),
        n_conflicts=conflicts,
        source_sdfs=sources,
    )
