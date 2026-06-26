"""Coverage diagnostic job — the notebook's logic as a first-class, tested job.

Flow: obtain the CoMPARA EXPERIMENTAL archive (local ``--source-zip`` or network) ->
parse + select the measured AR call (``compara``) -> derive InChIKeys + collapse
(``identity``) -> load LINCS metadata (local ``--lincs-dir`` or network) -> per-cell-line
overlap + gate-floor verdict via the PURE ``endoscan_core.diagnostics.coverage`` -> write
report + markdown via Storage. Measured 10321697/10321994 only; consensus 10322012
excluded. LINCS METADATA only (no gctx). No network in CI (fixtures via --source-zip /
--lincs-dir).
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pandas as pd

from endoscan_core.diagnostics import GateFloors, build_coverage_report

from ..compara import (
    MODES,
    extract_sdf_records,
    extract_tables,
    is_sdf_archive,
    labels_for_mode,
    select_measured_ar_call,
    to_binary,
)
from ..identity import collapse_one_label_per_structure, inchikey_from
from ..runner import JobContext, JobError, JobOutcome

DEFAULT_MODE = "functional_modulation"  # the MAIN endpoint: agonist ∪ antagonist

# Candidate cell-line contexts (UPPERCASE LINCS cell_id). VCaP/LNCaP are androgen-
# responsive; MCF7/A549 are what ER used; the broad set adds PC3.
DEFAULT_PROBE_LINES = ["VCAP", "LNCAP", "MCF7", "A549", "PC3"]
DEFAULT_CONTEXTS: dict[str, list[str]] = {
    "VCaP (androgen)": ["VCAP"],
    "VCaP+LNCaP (androgen)": ["VCAP", "LNCAP"],
    "MCF7/A549 (ER-style)": ["MCF7", "A549"],
    "broad (VCaP+MCF7+A549+PC3)": ["VCAP", "MCF7", "A549", "PC3"],
}

# CoMPARA EXPERIMENTAL articles (figshare). Consensus predictions (10322012) excluded.
COMPARA_EXPERIMENTAL_ARTICLES = [10321697, 10321994]
COMPARA_CONSENSUS_PREDICTION_ARTICLE = 10322012
LINCS_SIG_INFO_URL = "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE92nnn/GSE92742/suppl/GSE92742_Broad_LINCS_sig_info.txt.gz"
LINCS_PERT_INFO_URL = "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE92nnn/GSE92742/suppl/GSE92742_Broad_LINCS_pert_info.txt.gz"


def _norm_ik(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip().upper()
    return None if s in {"", "-666", "NAN", "NA", "RESTRICTED"} else s


def _read_tsv(path: Path) -> pd.DataFrame:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as handle:  # type: ignore[operator]
        return pd.read_csv(handle, sep="\t", low_memory=False)


def _find_lincs_file(lincs_dir: Path, stem: str) -> Path:
    for name in (f"{stem}.txt.gz", f"{stem}.txt", f"{stem}.tsv", f"{stem}.csv.gz", f"{stem}.csv"):
        candidate = lincs_dir / name
        if candidate.is_file():
            return candidate
    raise JobError(f"LINCS file for {stem!r} not found under {lincs_dir}")


def _load_compara_bytes(ctx: JobContext) -> bytes:
    source_zip = ctx.options.get("source_zip")
    if source_zip:
        data = Path(source_zip).read_bytes()
        ctx.log("compara_source", mode="local", path=str(source_zip), bytes=len(data))
        return data
    # Network path (server only; never exercised in CI). Best-effort figshare fetch.
    import requests  # noqa: PLC0415 - network dep, only when no local zip is provided

    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0 (EndoScan jobs)"})
    for art in COMPARA_EXPERIMENTAL_ARTICLES:
        url = f"https://api.figshare.com/v2/articles/{art}"
        try:
            meta = sess.get(url, timeout=120)
            ctx.log("figshare_api", article=art, status=meta.status_code)
            if meta.status_code != 200:
                continue
            for f in meta.json().get("files", []):
                if any(t in f["name"].lower() for t in ("pred", "consensus", "qsar", "model")):
                    continue
                r = sess.get(f["download_url"], timeout=600)
                ctx.log(
                    "figshare_download", name=f["name"], status=r.status_code, bytes=len(r.content)
                )
                if r.status_code == 200 and r.content[:2] == b"PK":
                    return r.content
        except Exception as exc:  # noqa: BLE001 - network failure -> JobError below
            ctx.log("figshare_error", article=art, error=str(exc))
    raise JobError(
        "could not fetch a CoMPARA experimental archive from figshare; pass --source-zip "
        "with a staged Data.zip (server runs have network; CI uses the fixture)."
    )


def _load_lincs(ctx: JobContext) -> tuple[pd.DataFrame, pd.DataFrame]:
    lincs_dir = ctx.options.get("lincs_dir")
    if lincs_dir:
        d = Path(lincs_dir)
        sig = _read_tsv(_find_lincs_file(d, "sig_info"))
        pert = _read_tsv(_find_lincs_file(d, "pert_info"))
        ctx.log("lincs_source", mode="local", dir=str(d), sig_rows=len(sig), pert_rows=len(pert))
        return sig, pert
    import requests  # noqa: PLC0415 - network dep, only when no local dir is provided

    sess = requests.Session()
    out = Path(ctx.storage.local_path("raw/lincs") or "/tmp/lincs")  # type: ignore[arg-type]
    out.mkdir(parents=True, exist_ok=True)
    paths = {}
    for stem, url in (("sig_info", LINCS_SIG_INFO_URL), ("pert_info", LINCS_PERT_INFO_URL)):
        dest = out / f"{stem}.txt.gz"
        if not dest.exists():
            r = sess.get(url, timeout=600)
            ctx.log("lincs_download", stem=stem, status=r.status_code, bytes=len(r.content))
            if r.status_code != 200:
                raise JobError(f"LINCS {stem} fetch failed: HTTP {r.status_code}")
            dest.write_bytes(r.content)
        paths[stem] = dest
    return _read_tsv(paths["sig_info"]), _read_tsv(paths["pert_info"])


def coverage_job(ctx: JobContext) -> JobOutcome:
    target = ctx.target

    # 1) CoMPARA experimental archive -> measured AR labels (SDF real format; table fallback).
    raw = _load_compara_bytes(ctx)
    ctx.storage.put_bytes(f"raw/{target}/compara.zip", raw)

    if is_sdf_archive(raw):
        # Real CoMPARA format: SDFs with <Mode>Class measured labels. Default mode is the
        # MAIN endpoint functional_modulation (agonist ∪ antagonist; binding EXCLUDED).
        mode = ctx.options.get("mode") or DEFAULT_MODE
        if mode not in MODES:
            raise JobError(f"unknown mode {mode!r}; one of {MODES}")
        records = extract_sdf_records(raw)
        ctx.log(
            "compara_sdf",
            binding=len(records["binding"]),
            agonist=len(records["agonist"]),
            antagonist=len(records["antagonist"]),
        )
        result = labels_for_mode(records, mode)
        labels, conflicts = dict(result.labels), result.n_conflicts
        source_table = ", ".join(result.source_sdfs)
        source_call = mode + (" [DIAGNOSTIC-ONLY]" if result.diagnostic_only else "")
        ctx.log(
            "sdf_labels",
            mode=mode,
            diagnostic_only=result.diagnostic_only,
            pos=result.n_positive,
            neg=result.n_negative,
            conflicts=conflicts,
            sdfs=result.source_sdfs,
        )
        if not labels:
            raise JobError(f"no clean AR labels for mode {mode!r}")
    else:
        # Legacy TABLE fallback (e.g. a CSV/XLSX archive).
        tables = extract_tables(raw)
        ctx.log("compara_tables", names=[n for n, _ in tables])
        if not tables:
            raise JobError("no SDF or tabular measured data found in the CoMPARA archive")
        selection, df = select_measured_ar_call(
            tables,
            force_table=ctx.options.get("force_table"),
            force_call_col=ctx.options.get("force_call_col"),
            force_struct_col=ctx.options.get("force_struct_col"),
        )
        pairs: list[tuple[str, int]] = []
        for _, row in df.iterrows():
            label = to_binary(row.get(selection.call_col))
            if label is None:
                continue
            ik = inchikey_from(
                row.get(selection.struct_inchi_col), row.get(selection.struct_smiles_col)
            )
            if ik:
                pairs.append((ik, label))
        labels, conflicts = collapse_one_label_per_structure(pairs)
        source_table, source_call = selection.table_name, selection.call_col
        ctx.log("labels", n=len(labels), conflicts=conflicts)
        if not labels:
            raise JobError("no clean AR labels derived from the measured table")

    # 3) LINCS metadata -> per-line trt_cp profiled InChIKeys.
    sig, pert = _load_lincs(ctx)
    pert_to_ik: dict[str, str] = {}
    for pid, key in zip(pert["pert_id"].astype(str), pert["inchi_key"], strict=False):
        nk = _norm_ik(key)
        if nk:
            pert_to_ik[pid] = nk
    trt = sig[sig["pert_type"].astype(str) == "trt_cp"].copy()
    trt["_CELL"] = trt["cell_id"].astype(str).str.upper()

    line_signature_counts = {
        line: int((trt["_CELL"] == line).sum()) for line in DEFAULT_PROBE_LINES
    }
    line_to_ik: dict[str, set[str]] = {}
    for line in DEFAULT_PROBE_LINES:
        pids = set(trt.loc[trt["_CELL"] == line, "pert_id"].astype(str))
        line_to_ik[line] = {pert_to_ik[p] for p in pids if p in pert_to_ik}
    context_inchikeys = {
        name: set().union(*(line_to_ik.get(line, set()) for line in lines)) if lines else set()
        for name, lines in DEFAULT_CONTEXTS.items()
    }

    # 4) Pure coverage math + verdict vs the gate floors (default mirrors quality_gates.yaml).
    floors = GateFloors()
    report = build_coverage_report(
        target=target,
        labels=labels,
        n_conflicts=conflicts,
        context_inchikeys=context_inchikeys,
        contexts=DEFAULT_CONTEXTS,
        line_signature_counts=line_signature_counts,
        floors=floors,
        source_table=source_table,
        source_call_column=source_call,
    )

    # 5) Persist report (json + human markdown) through Storage.
    report_key = f"reports/coverage/{target}/{ctx.run_id}.json"
    ctx.storage.put_text(report_key, report.model_dump_json(indent=2))
    ctx.storage.put_text(f"reports/coverage/{target}/{ctx.run_id}.md", _render_markdown(report))

    verdict = "PASS" if report.any_context_passes else "failed_qc"
    summary = (
        f"{target} coverage: {verdict}; {report.n_structures} structures "
        f"(pos={report.n_positives}, neg={report.n_negatives}); rec={report.recommended_context}"
    )
    return JobOutcome(report_key=report_key, summary=summary, verdict=verdict)


def _render_markdown(report) -> str:
    lines = [
        f"# AR/{report.target} coverage diagnostic",
        "",
        f"- source table: `{report.source_table}` | call column: `{report.source_call_column}`",
        f"- consensus-prediction article {COMPARA_CONSENSUS_PREDICTION_ARTICLE} NOT used",
        f"- clean structures: {report.n_structures} (pos={report.n_positives}, "
        f"neg={report.n_negatives}); conflicts excluded: {report.n_conflicts}",
        f"- floors: min_overlap={report.floors.min_overlap}, "
        f"min_compounds_per_class={report.floors.min_compounds_per_class}",
        "",
        "| context | overlap | pos | neg | prevalence | verdict | why |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in report.contexts:
        lines.append(
            f"| {c.name} | {c.overlap} | {c.positives} | {c.negatives} | "
            f"{c.prevalence:.1%} | {c.verdict} | {c.reason} |"
        )
    lines += ["", f"**Recommended:** {report.recommended_context} — {report.recommendation_note}"]
    return "\n".join(lines) + "\n"
