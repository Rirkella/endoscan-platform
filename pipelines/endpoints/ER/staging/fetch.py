"""Network fetchers + archive inspection for the ER staging layer.

Downloads ONLY from the approved locators in ``registry/data/sources.yaml``. This
is the explicit offline prep layer: the toolchain never imports it, and
``RealDownloadAdapter`` stays a stub. The network fetchers (``requests``) run only in
the cloud; the pure archive-extraction / delimiter-sniffing / table-reading helpers
(``extract_cerapp_archives``, ``find_tabular_candidates``, ``read_tabular``,
``inspect_cerapp_archives``) are unit-tested in CI on a synthetic zip.
"""

from __future__ import annotations

import csv
import zipfile
from collections.abc import Sequence
from pathlib import Path

_DELIMITED_SUFFIXES = {".csv", ".tsv", ".txt"}
_EXCEL_SUFFIXES = {".xlsx", ".xls"}
_DOC_NAME_TOKENS = ("readme", "license", "licence", "changelog", "notice")


def fetch_url(url: str, dest: Path, *, chunk: int = 1 << 20) -> Path:
    """Stream-download an approved URL to ``dest`` (cloud only)."""
    import requests  # noqa: PLC0415 — staging-only, not a core dependency

    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        with dest.open("wb") as handle:
            for part in resp.iter_content(chunk_size=chunk):
                handle.write(part)
    return dest


def pubchem_mapping_for_casrns(casrns: Sequence[str]) -> list[dict]:
    """Build CASRN -> InChIKey/CID/SMILES rows via PubChem PUG REST (cloud only).

    Returns rows shaped for ``pubchem.normalize_mapping``. Runs only in the Colab
    run (network). Unmapped CASRNs are skipped. Not imported by the test suite.
    """
    import requests  # noqa: PLC0415 — staging-only, not a core dependency

    base = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name"
    props = "InChIKey,CanonicalSMILES"
    rows: list[dict] = []
    for casrn in casrns:
        url = f"{base}/{casrn}/property/{props}/JSON"
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code != 200:
                continue
            prop = resp.json()["PropertyTable"]["Properties"][0]
        except Exception:
            continue
        rows.append(
            {
                "input_id": str(casrn),
                "input_id_type": "CASRN",
                "inchikey": prop.get("InChIKey"),
                "cid": prop.get("CID"),
                "smiles": prop.get("CanonicalSMILES"),
                "mapping_confidence": "exact",
            }
        )
    return rows


def figshare_file_urls(article_id: str) -> list[tuple[str, str]]:
    """Return ``[(filename, download_url)]`` for a figshare article (cloud only).

    Hits the public figshare API ``GET /v2/articles/{article_id}`` and reads
    ``files[].download_url``. Not imported by the test suite.
    """
    import requests  # noqa: PLC0415 — staging-only, not a core dependency

    resp = requests.get(f"https://api.figshare.com/v2/articles/{article_id}", timeout=60)
    resp.raise_for_status()
    return [(f["name"], f["download_url"]) for f in resp.json().get("files", [])]


def fetch_cerapp_experimental(
    dest_dir: Path, article_ids: Sequence[str], *, gaftp_url: str | None = None
) -> list[Path]:
    """Resolve + download the CERAPP EXPERIMENTAL files programmatically (cloud only).

    Tries the figshare API first (the ``article_ids`` parsed from the approved
    figshare locators), downloading every file each article exposes. If that yields
    nothing and ``gaftp_url`` is given, streams the gaftp mirror instead. Raises if
    neither resolves (the notebook then offers a single manual-upload fallback).
    Returns the local file paths. Not imported by the test suite.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    try:
        for article_id in article_ids:
            for name, download_url in figshare_file_urls(article_id):
                paths.append(fetch_url(download_url, dest_dir / name))
    except Exception:
        paths = []
    if not paths and gaftp_url:
        name = Path(gaftp_url.rstrip("/")).name or "cerapp_gaftp"
        paths = [fetch_url(gaftp_url, dest_dir / name)]
    if not paths:
        raise RuntimeError(
            "Could not resolve CERAPP experimental files from figshare or the gaftp "
            "mirror; use the clearly-marked manual-upload fallback cell."
        )
    return paths


def _looks_like_doc(path: Path) -> bool:
    """True for readme/license/changelog-style docs (ignored even with a .txt ext)."""
    stem = Path(path).stem.lower()
    return any(token in stem for token in _DOC_NAME_TOKENS)


def _sniff_delimiter(sample: str) -> str:
    """Sniff comma vs tab (etc.); a CERAPP .txt may be tab- OR comma-delimited."""
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        header = sample.splitlines()[0] if sample.splitlines() else ""
        return "\t" if header.count("\t") > header.count(",") else ","


def read_tabular(path: Path):
    """Read one tabular file; return ``(DataFrame, how)`` or ``None`` if not readable.

    - csv/tsv/txt: delimiter is **sniffed** (never assumed by extension).
    - xlsx/xls: only if ``openpyxl`` is importable (NOT a hard dep) — else ``None``.
    - readme/license docs, non-tabular extensions, and unparseable files -> ``None``.
    """
    import pandas as pd  # noqa: PLC0415 — staging-only

    path = Path(path)
    suffix = path.suffix.lower()
    if _looks_like_doc(path):
        return None
    if suffix in _EXCEL_SUFFIXES:
        try:
            import openpyxl  # noqa: F401, PLC0415 — optional, not a hard dep
        except ImportError:
            return None
        try:
            return pd.read_excel(path), "xlsx"
        except Exception:
            return None
    if suffix in _DELIMITED_SUFFIXES:
        try:
            with path.open(encoding="utf-8", errors="replace", newline="") as handle:
                sample = handle.read(8192)
            if not sample.strip():
                return None
            sep = _sniff_delimiter(sample)
            return pd.read_csv(path, sep=sep), repr(sep)
        except Exception:
            return None
    return None


def extract_cerapp_archives(
    paths: Sequence[Path], work_dir: Path, *, _max_depth: int = 5
) -> list[Path]:
    """Extract any ``.zip`` among ``paths`` under ``work_dir`` (recursively for nested zips).

    Returns the roots to scan: an extraction dir per zip, the original path for non-zips.
    Bounded depth guards against pathological nested archives. Downloaded-but-not-yet-
    extracted is exactly what this fixes — it never silently drops the archive contents.
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    roots: list[Path] = []
    for path in paths:
        path = Path(path)
        if path.suffix.lower() == ".zip":
            dest = work_dir / f"{path.stem}_extracted"
            dest.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(path) as archive:
                archive.extractall(dest)  # trusted figshare/CI source
            roots.append(dest)
        else:
            roots.append(path)
    for _ in range(_max_depth):
        pending = [
            nested
            for root in roots
            if Path(root).is_dir()
            for nested in Path(root).rglob("*.zip")
            if not (nested.parent / f"{nested.stem}_extracted").exists()
        ]
        if not pending:
            break
        for nested in pending:
            dest = nested.parent / f"{nested.stem}_extracted"
            dest.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(nested) as archive:
                archive.extractall(dest)
    return roots


def find_tabular_candidates(roots: Sequence[Path]) -> list[Path]:
    """Recursively collect candidate tabular files (by extension) under ``roots``."""
    exts = _DELIMITED_SUFFIXES | _EXCEL_SUFFIXES
    out: list[Path] = []
    for root in roots:
        root = Path(root)
        files = sorted(root.rglob("*")) if root.is_dir() else [root]
        out.extend(f for f in files if f.is_file() and f.suffix.lower() in exts)
    return out


def _classify_columns(columns) -> str:
    """Soft note: distinguish experimental-activity tables from consensus/predicted ones."""
    cols = " ".join(str(c).lower() for c in columns)
    if "consensus" in cols or "predict" in cols or "qsar" in cols:
        return "consensus/predicted? — do NOT use as labels"
    experimental = ("activity", "active", "agonist", "antagonist", "binding", "reference")
    if any(token in cols for token in experimental):
        return "experimental-activity? — candidate labels"
    return "unclassified"


def inspect_cerapp_archives(paths: Sequence[Path], work_dir: Path) -> list[dict]:
    """Extract CERAPP archives, find EVERY tabular file, print + return a manifest.

    Does NOT auto-select a file — the operator/Claude Chat picks the experimental table
    at Stop 1. Returns one dict per readable table: ``rel`` (relative to ``work_dir``),
    ``path``, ``rows``, ``columns``, ``kind``, ``readable``. An empty list means no
    readable table was found (only then should the caller fall back to manual upload).
    """
    work_dir = Path(work_dir)
    roots = extract_cerapp_archives(paths, work_dir)
    manifest: list[dict] = []
    for path in find_tabular_candidates(roots):
        try:
            rel = str(Path(path).relative_to(work_dir))
        except ValueError:
            rel = str(path)
        result = read_tabular(path)
        if result is None:
            if path.suffix.lower() in _EXCEL_SUFFIXES:
                try:
                    import openpyxl  # noqa: F401, PLC0415

                    note = "unreadable .xlsx"
                except ImportError:
                    note = "skipped .xlsx (openpyxl not installed; not a hard dep)"
            elif _looks_like_doc(path):
                note = "skipped (readme/license/doc)"
            else:
                note = "not a readable table"
            print(f"-- {rel}: {note}")
            continue
        df, how = result
        kind = _classify_columns(df.columns)
        print(f"== {rel}: {len(df)} rows | delimiter={how} | {kind}")
        print(f"   columns: {list(df.columns)}")
        manifest.append(
            {
                "rel": rel,
                "path": str(path),
                "rows": int(len(df)),
                "columns": list(df.columns),
                "kind": kind,
                "readable": True,
            }
        )
    if not manifest:
        print(
            "No readable tabular file found in the CERAPP archives after extraction — "
            "use the manual-upload fallback cell (1d)."
        )
    return manifest


def clue_io_landmark_signatures(sig_ids: Sequence[str], api_key: str):
    """TRY-FIRST optimization: fetch per-``sig_id`` Level-5 landmark vectors.

    The clue.io Data API surface for per-signature MODZ landmark values is
    historically gated and UNVERIFIED from the build environment. The Colab run
    may attempt it; on any failure the caller falls back to the GEO gctx slice
    (the supported backbone). Kept as a stub so 2a ships without depending on it.
    """
    raise NotImplementedError(
        "clue.io per-sig_id landmark fetch is the try-first optimization; implement/attempt "
        "it in the Colab run with a free key, and fall back to the gctx slice on failure."
    )
