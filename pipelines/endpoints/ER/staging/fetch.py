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
import re
import zipfile
from collections.abc import Sequence
from pathlib import Path

_DELIMITED_SUFFIXES = {".csv", ".tsv", ".txt"}
_EXCEL_SUFFIXES = {".xlsx", ".xls"}
_DOC_NAME_TOKENS = ("readme", "license", "licence", "changelog", "notice")


def _http_download(url: str, dest: Path, *, chunk: int = 1 << 20) -> tuple[str, str]:
    """Stream-download ``url`` to ``dest`` following redirects (cloud only).

    Returns ``(resolved_url, content_type)``. Raises on a non-200 status. Validation
    of the *content* is the caller's job (see ``validate_artifact``).
    """
    import requests  # noqa: PLC0415 — staging-only, not a core dependency

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=300, allow_redirects=True) as resp:
        resp.raise_for_status()
        resolved_url = resp.url
        content_type = resp.headers.get("Content-Type", "")
        with dest.open("wb") as handle:
            for part in resp.iter_content(chunk_size=chunk):
                handle.write(part)
    return resolved_url, content_type


def looks_like_html(path: Path) -> bool:
    """True if the file's first bytes start with ``<!doctype`` / ``<html`` (a web page)."""
    with Path(path).open("rb") as handle:
        head = handle.read(512).lstrip().lower()
    return head.startswith(b"<!doctype") or head.startswith(b"<html")


def is_valid_zip(path: Path) -> bool:
    """True only for a real ZIP container (and not an HTML page saved as ``.zip``)."""
    return zipfile.is_zipfile(path) and not looks_like_html(path)


def validate_artifact(
    path: Path, *, kind: str = "any", resolved_url: str = "", content_type: str = ""
) -> Path:
    """Validate a downloaded artifact; on rejection DELETE it and raise a clear error.

    ``kind``: ``"zip"`` requires a real ZIP, ``"tabular"`` requires it parse as a table,
    ``"any"`` only rejects HTML. HTML landing pages are always rejected.
    """
    path = Path(path)
    if looks_like_html(path):
        path.unlink(missing_ok=True)
        raise ValueError(
            f"downloaded file is HTML, not data (resolved URL {resolved_url!r}, "
            f"content-type {content_type!r}) — the resolver hit a landing page."
        )
    if kind == "zip" and not zipfile.is_zipfile(path):
        path.unlink(missing_ok=True)
        raise ValueError(
            f"downloaded file is not a valid ZIP (resolved URL {resolved_url!r}, "
            f"content-type {content_type!r})."
        )
    if kind == "tabular" and read_tabular(path) is None:
        path.unlink(missing_ok=True)
        raise ValueError(
            f"downloaded file does not parse as a table (resolved URL {resolved_url!r}, "
            f"content-type {content_type!r})."
        )
    return path


def fetch_url(url: str, dest: Path, *, chunk: int = 1 << 20) -> Path:
    """Stream-download an approved URL to ``dest`` and reject HTML error pages (cloud only)."""
    resolved_url, content_type = _http_download(url, dest, chunk=chunk)
    return validate_artifact(dest, kind="any", resolved_url=resolved_url, content_type=content_type)


def download_cerapp_file(url: str, dest: Path, *, http=None) -> Path:
    """Download one CERAPP artifact to ``dest`` with stale-cache revalidation + validation.

    Validates by extension (``.zip`` -> real ZIP, else a parseable table). A pre-existing
    cached file is re-validated and **re-downloaded if invalid** (e.g. an HTML page saved
    as ``.zip`` by a prior failed run) — never silently reused. ``http`` is injectable
    for tests; it must write ``dest`` and return ``(resolved_url, content_type)``.
    """
    dest = Path(dest)
    fetcher = http or _http_download
    kind = "zip" if dest.suffix.lower() == ".zip" else "tabular"
    if dest.exists():
        try:
            return validate_artifact(dest, kind=kind, resolved_url="(cached)")
        except ValueError:
            pass  # validate_artifact already deleted the stale/invalid cache
    resolved_url, content_type = fetcher(url, dest)
    return validate_artifact(dest, kind=kind, resolved_url=resolved_url, content_type=content_type)


def parse_gctx_dims(name: str) -> tuple[int, int] | None:
    """Parse ``n<sig>x<genes>`` from a GSE92742 Level-5 ``.gctx`` filename.

    e.g. ``GSE92742_..._Level5_COMPZ.MODZ_n473647x12328.gctx.gz`` -> ``(473647, 12328)``
    (n_signatures, n_genes). Returns ``None`` if the pattern is absent. Pure string
    parse — lets Stop 1 report the EXPECTED gctx shape WITHOUT downloading the file.
    """
    match = re.search(r"_n(\d+)x(\d+)\.gctx", name)
    return (int(match.group(1)), int(match.group(2))) if match else None


def is_valid_gctx(path: Path) -> bool:
    """True if ``path`` opens as an HDF5 GCTx with readable ROW/COL id metadata."""
    import h5py  # noqa: PLC0415 — staging/dev dependency, not an endoscan_core runtime dep

    path = Path(path)
    if not path.is_file():
        return False
    try:
        with h5py.File(path, "r") as handle:
            handle["/0/META/ROW/id"].shape[0]
            handle["/0/META/COL/id"].shape[0]
        return True
    except Exception:
        return False


def download_gctx(url: str, gctx_path: Path, *, gz_path: Path | None = None, http=None) -> Path:
    """HEAVY: download + gunzip the Level-5 ``.gctx`` with cache validation (cloud only).

    Reuses an existing decompressed ``.gctx`` ONLY if it is a valid HDF5
    (``is_valid_gctx``) — a truncated/invalid cache from a prior run is discarded and
    re-fetched (the same don't-reuse-garbage policy as the zip/HTML validation). The
    downloaded ``.gz`` is rejected if it is HTML. ``http`` is injectable for tests.
    """
    import gzip  # noqa: PLC0415 — staging-only
    import shutil  # noqa: PLC0415 — staging-only

    gctx_path = Path(gctx_path)
    if is_valid_gctx(gctx_path):
        return gctx_path  # valid cache — skip the ~20 GB re-download ("resume")
    gctx_path.unlink(missing_ok=True)
    gz_path = Path(gz_path) if gz_path else gctx_path.with_name(gctx_path.name + ".gz")
    fetcher = http or _http_download
    resolved_url, content_type = fetcher(url, gz_path)
    validate_artifact(gz_path, kind="any", resolved_url=resolved_url, content_type=content_type)
    with gzip.open(gz_path, "rb") as src, gctx_path.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    if not is_valid_gctx(gctx_path):
        gctx_path.unlink(missing_ok=True)
        raise ValueError(
            f"decompressed gctx is not a valid HDF5 (resolved URL {resolved_url!r}, "
            f"content-type {content_type!r}) — truncated/invalid download."
        )
    return gctx_path


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


def figshare_files(article_id: str) -> list[dict]:
    """Return the figshare article's ``files[]`` list (cloud only).

    Hits the public figshare API ``GET /v2/articles/{article_id}`` and returns the raw
    ``files`` entries (each with ``name`` + ``download_url``).
    """
    import requests  # noqa: PLC0415 — staging-only, not a core dependency

    resp = requests.get(f"https://api.figshare.com/v2/articles/{article_id}", timeout=60)
    resp.raise_for_status()
    return resp.json().get("files", [])


def pick_figshare_data_files(files: Sequence[dict]) -> list[tuple[str, str]]:
    """Pick the DATA files from a figshare ``files[]`` list (never the article HTML page).

    Selects entries whose name ends in a data extension (``.zip``/``.csv``/``.tsv``/
    ``.txt``/``.xlsx``/``.xls``), ignoring PDFs and readme/license docs. Returns
    ``[(name, download_url)]`` (possibly several — the caller surfaces them).
    """
    data_exts = {".zip"} | _DELIMITED_SUFFIXES | _EXCEL_SUFFIXES
    out: list[tuple[str, str]] = []
    for entry in files:
        name = entry.get("name", "")
        suffix = Path(name).suffix.lower()
        if suffix in data_exts and not _looks_like_doc(Path(name)):
            out.append((name, entry.get("download_url", "")))
    return out


def _gaftp_zip_candidates(gaftp_url: str | None) -> list[str]:
    """Direct CERAPP archive URLs on the GAFTP mirror dir (validated after download)."""
    if not gaftp_url:
        return []
    base = gaftp_url.rstrip("/") + "/"
    return [base + name for name in ("TrainingSet.zip", "EvaluationSet.zip")]


def fetch_cerapp_experimental(
    dest_dir: Path,
    article_ids: Sequence[str],
    *,
    gaftp_url: str | None = None,
    http=None,
    figshare_lister=None,
) -> list[Path]:
    """Resolve + download the CERAPP EXPERIMENTAL archives, VALIDATING every artifact.

    Resolver order: (a) the GAFTP mirror's direct file URLs; (b) the figshare API
    ``files[]`` (the DATA file by extension — **never** the article HTML page). Each
    download is validated (HTML rejected, ``.zip`` must be a real ZIP) and stale invalid
    cached files are re-fetched. Raises a clear error only after BOTH resolvers fail.
    ``http`` / ``figshare_lister`` are injectable for tests. Cloud only.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    figshare = figshare_lister or figshare_files
    errors: list[str] = []

    # (a) GAFTP mirror first — direct file URLs.
    gaftp_paths: list[Path] = []
    for url in _gaftp_zip_candidates(gaftp_url):
        try:
            gaftp_paths.append(download_cerapp_file(url, dest_dir / Path(url).name, http=http))
        except Exception as exc:
            errors.append(f"gaftp {Path(url).name}: {exc}")
    if gaftp_paths:
        return gaftp_paths

    # (b) figshare API second — the DATA file, never the article HTML page.
    figshare_paths: list[Path] = []
    for article_id in article_ids:
        try:
            data_files = pick_figshare_data_files(figshare(article_id))
        except Exception as exc:
            errors.append(f"figshare {article_id}: listing failed: {exc}")
            continue
        if not data_files:
            errors.append(f"figshare {article_id}: no data file in files[]")
            continue
        for name, url in data_files:
            try:
                figshare_paths.append(download_cerapp_file(url, dest_dir / name, http=http))
            except Exception as exc:
                errors.append(f"figshare {name}: {exc}")
    if figshare_paths:
        return figshare_paths

    raise RuntimeError(
        "Could not obtain a VALID CERAPP archive from the GAFTP mirror or figshare "
        "(HTML/not-a-valid-ZIP or download failure). Details: "
        + " | ".join(errors)
        + " — use the manual-upload fallback cell (1d)."
    )


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
