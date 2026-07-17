"""Parse an uploaded signature (JSON / CSV) into a ``{gene: value}`` mapping.

This layer ONLY turns bytes into a mapping — it does NOT validate the gene set (that is the
single authoritative validator, ``endoscan_core.inference.align_signature``, applied by the
route). The 400/422 boundary is crisp:

- ``MalformedUploadError`` (-> HTTP 400): the bytes cannot be turned into a signature at all —
  empty, undecodable, bad JSON/CSV structure, wrong columns, duplicate gene, or a cell whose
  text is not a parseable number ("row N value is not numeric").
- A value that IS parseable but non-finite (NaN/inf) passes through here and is caught downstream
  by ``align_signature`` -> HTTP 422 (invalid_signature).

Format dispatch is a registry ({json, csv, tsv}); an ``xlsx`` handler is an additive later entry.
The validated-signature output is format-independent. Unknown format -> ``MalformedUploadError``.

Stateless: callers pass decoded text; nothing is persisted.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass

from .limits import MAX_GENES, MAX_ROWS

GENE_COLUMN_ALIASES = {"gene", "gene_symbol", "symbol", "genes"}


class MalformedUploadError(Exception):
    """The upload cannot be parsed into a signature mapping (-> HTTP 400)."""


@dataclass
class ParsedTable:
    """A parsed upload. ``mapping`` is the single-sample signature; when a multi-column CSV is
    supplied without a chosen sample, ``mapping`` is None and ``samples`` lists the choices."""

    mapping: dict[str, float] | None
    samples: list[str] | None = None


def _to_number(raw: str, where: str) -> float:
    # float() accepts "nan"/"inf" — those are intentionally allowed through and rejected later by
    # align_signature (non-finite -> 422). Genuinely non-numeric text is a parse error (400).
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise MalformedUploadError(f"{where} value is not numeric: {raw!r}") from exc


def parse_json(text: str) -> ParsedTable:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedUploadError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise MalformedUploadError("JSON signature must be an object of gene -> number.")
    if not obj:
        raise MalformedUploadError("signature is empty.")
    if len(obj) > MAX_GENES:
        raise MalformedUploadError(f"signature exceeds the {MAX_GENES} gene limit.")
    mapping: dict[str, float] = {}
    for gene, value in obj.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise MalformedUploadError(f"value for gene {gene!r} is not a number.")
        mapping[str(gene)] = float(value)
    return ParsedTable(mapping=mapping)


def _parse_delimited(
    text: str, *, delimiter: str, format_name: str, sample: str | None = None
) -> ParsedTable:
    rows: list[list[str]] = []
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        if not any(cell.strip() for cell in row):
            continue
        rows.append(row)
        if len(rows) > MAX_ROWS + 1:  # header + bounded data rows
            raise MalformedUploadError(f"{format_name} exceeds the {MAX_ROWS} data-row limit.")
    if len(rows) < 2:
        raise MalformedUploadError(f"{format_name} is empty or has no data rows.")
    header = [h.strip() for h in rows[0]]
    lower = [h.lower() for h in header]

    gene_idx = next((i for i, h in enumerate(lower) if h in GENE_COLUMN_ALIASES), None)
    if gene_idx is None:
        raise MalformedUploadError("expected a gene column (e.g. 'gene'); none found.")
    value_cols = [i for i in range(len(header)) if i != gene_idx]
    if not value_cols:
        raise MalformedUploadError("expected columns gene,value; no value column found.")

    # Multi-column: report the sample names; require a choice (no silent wrong-column).
    if len(value_cols) > 1 and sample is None:
        return ParsedTable(mapping=None, samples=[header[i] for i in value_cols])

    if sample is not None:
        matches = [i for i in value_cols if header[i] == sample]
        if not matches:
            raise MalformedUploadError(f"sample column {sample!r} not found.")
        col = matches[0]
    else:
        col = value_cols[0]

    mapping: dict[str, float] = {}
    for n, row in enumerate(rows[1:], start=2):  # 1-based incl. header
        if len(row) <= max(gene_idx, col):
            raise MalformedUploadError(f"row {n} has too few columns.")
        gene = row[gene_idx].strip()
        if not gene:
            continue
        if gene in mapping:
            raise MalformedUploadError(f"duplicate gene {gene!r} at row {n}.")
        mapping[gene] = _to_number(row[col].strip(), f"row {n}")
    if not mapping:
        raise MalformedUploadError("no gene rows found.")
    if len(mapping) > MAX_GENES:
        raise MalformedUploadError(f"signature exceeds the {MAX_GENES} gene limit.")
    samples = [header[i] for i in value_cols] if len(value_cols) > 1 else None
    return ParsedTable(mapping=mapping, samples=samples)


def parse_csv(text: str, *, sample: str | None = None) -> ParsedTable:
    return _parse_delimited(text, delimiter=",", format_name="CSV", sample=sample)


def parse_tsv(text: str, *, sample: str | None = None) -> ParsedTable:
    return _parse_delimited(text, delimiter="\t", format_name="TSV", sample=sample)


_PARSERS = {"json": parse_json, "csv": parse_csv, "tsv": parse_tsv}


def parse_upload(fmt: str, text: str, *, sample: str | None = None) -> ParsedTable:
    """Dispatch to the parser for ``fmt`` (registry). Unknown format -> MalformedUploadError."""
    if not text.strip():
        raise MalformedUploadError("uploaded content is empty.")
    parser = _PARSERS.get(fmt)
    if parser is None:
        raise MalformedUploadError(
            f"unsupported format {fmt!r}; expected one of {sorted(_PARSERS)}."
        )
    if fmt in {"csv", "tsv"}:
        return _PARSERS[fmt](text, sample=sample)
    return parser(text)
