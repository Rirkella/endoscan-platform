"""Parse a CoMPARA EXPERIMENTAL archive into tables and select the MEASURED AR call.

Pure ``zipfile`` + ``pandas`` (NO rdkit, NO network), so it is unit-testable against a
fixture zip mirroring the real ``Data.zip``. It extracts inner tables (.csv/.tsv/.xlsx)
and auto-selects the measured AR activity table + call column, EXCLUDING any
consensus/predicted column or file (``pred|consensus|qsar|model|score|...``). The
measured "binding" call is preferred (the canonical AR activity).

NOTE (server-wiring): the exact internal layout of the real 68 KB ``Data.zip`` (figshare
article 10321697) could not be inspected from the build sandbox (figshare/clowder are
egress-blocked). The selection is heuristic and self-reporting; the fixture mirrors the
plausible CoMPARA training-set shape and is to be confirmed against the real archive.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, ConfigDict

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
