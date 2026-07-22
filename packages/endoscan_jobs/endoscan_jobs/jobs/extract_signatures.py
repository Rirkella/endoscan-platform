"""Extract a per-endpoint LINCS signature matrix — the memory-efficient server build path.

Promotes the original ER staging slice into a first-class, tested heavy-data job
that the durable workflow can use behind a reviewed tool boundary on a modest
(~3.7 GB RAM) server. It is a memory-efficient implementation of the SAME extraction — it
produces the SAME fused 978-gene-per-compound matrix and SAME ``compound_id`` contract the
frozen ER ``lincs.parquet`` uses; it changes no thresholds, gate logic, or the ER model.

Flow (select-then-slice — the big file is touched once, for a bounded region):
  CoMPARA labels -> labelled InChIKeys (same parser as the coverage job) ->
  LINCS metadata (bounded read) -> sig_meta overlap (the SINGLE source of the sig_id list) ->
  select_condition_signatures (metadata only) -> manifest cache check ->
  batched, chunk-cache-bounded h5py slice of ONLY the chosen sig_ids x 978 landmark genes ->
  early_fuse -> write curated/<TARGET>/signatures.parquet + slice_manifest.json via Storage.

Exit-code contract mirrors coverage: the job RAN (incl. an empty-overlap ``failed_qc``
outcome, or a cache reuse) -> exit 0; a fetch/parse/slice failure -> JobError -> exit 2.
No network and no real gctx in CI (fixtures: synthetic gctx + --source-zip/--lincs-dir).
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pandas as pd

from ..compara import (
    MODES,
    extract_sdf_records,
    extract_tables,
    is_sdf_archive,
    labels_for_mode,
    select_measured_ar_call,
    to_binary,
)
from ..fusion import (
    FUSION_PARAMS,
    assemble_sig_meta,
    early_fuse,
    select_condition_signatures,
    select_landmark_genes,
)
from ..gctx import DEFAULT_RDCC_NBYTES, DEFAULT_SIG_BATCH, slice_gctx_landmark
from ..identity import collapse_one_label_per_structure, inchikey_from
from ..runner import JobContext, JobError, JobOutcome
from .coverage import DEFAULT_MODE, _find_lincs_file, _load_compara_bytes, _load_lincs, _norm_ik

#: Default cell-line set when ``--cell-lines`` is unset (the ER-style context). The set is
#: PARAMETERIZED so the same endpoint can be extracted on different contexts (e.g. VCaP vs
#: VCaP+A549) for the platform comparison.
DEFAULT_CELL_LINES = ["MCF7", "A549"]

# Real GSE92742 gene_info columns (override via options if a different release is staged).
_GENE_LANDMARK_COL = "pr_is_lm"
_GENE_ID_COL = "pr_gene_id"
_GENE_SYMBOL_COL = "pr_gene_symbol"


def _parse_cell_lines(spec: str | None) -> list[str]:
    if not spec or not spec.strip():
        return list(DEFAULT_CELL_LINES)
    cells = [c.strip().upper() for c in spec.split(",") if c.strip()]
    if not cells:
        raise JobError(f"invalid --cell-lines {spec!r}; expected e.g. 'VCAP,A549'")
    return cells


def _derive_labels(ctx: JobContext, raw: bytes) -> tuple[dict[str, int], str]:
    """Labelled InChIKeys (same measured-AR logic as the coverage job; SDF, table fallback)."""
    if is_sdf_archive(raw):
        mode = ctx.options.get("mode") or DEFAULT_MODE
        if mode not in MODES:
            raise JobError(f"unknown mode {mode!r}; one of {MODES}")
        result = labels_for_mode(extract_sdf_records(raw), mode)
        if not result.labels:
            raise JobError(f"no clean AR labels for mode {mode!r}")
        return dict(result.labels), mode
    tables = extract_tables(raw)
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
    labels, _conflicts = collapse_one_label_per_structure(pairs)
    if not labels:
        raise JobError("no clean AR labels derived from the measured table")
    return labels, selection.call_col


def _load_gene_info(ctx: JobContext) -> pd.DataFrame:
    gene_info = ctx.options.get("gene_info")
    if gene_info:
        path = Path(gene_info)
    else:
        lincs_dir = ctx.options.get("lincs_dir")
        if not lincs_dir:
            raise JobError("gene_info is required (pass --gene-info or stage it under --lincs-dir)")
        path = _find_lincs_file(Path(lincs_dir), "gene_info")
    from .coverage import _read_tsv  # noqa: PLC0415 - reuse the gz-aware tsv reader

    return _read_tsv(path)


def _hash_ids(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted({str(i) for i in ids})).encode()).hexdigest()


def _cache_key(gctx_path: str, sig_ids: list[str], gene_ids: list[str], cells, mode: str) -> dict:
    """Identity of a slice: gctx (name,size) + sig-id set + landmark-gene set + cells + mode.

    Metadata-only — computed BEFORE any slice — so a matching cached parquet can be reused
    without re-reading the ~23 GB gctx (resumable + reproducible).
    """
    p = Path(gctx_path)
    return {
        "gctx_name": p.name,
        "gctx_size": p.stat().st_size if p.exists() else None,
        "sig_ids_sha256": _hash_ids(sig_ids),
        "gene_ids_sha256": _hash_ids(gene_ids),
        "cell_lines": sorted({str(c).upper() for c in cells}),
        "mode": mode,
        "fusion_params": FUSION_PARAMS,
    }


def extract_signatures_job(ctx: JobContext) -> JobOutcome:
    target = ctx.target
    cells = _parse_cell_lines(ctx.options.get("cell_lines"))
    curated_key = f"curated/{target}/signatures.parquet"
    manifest_key = f"curated/{target}/slice_manifest.json"

    # 1) Measured CoMPARA labels -> the labelled compound set (both classes).
    raw = _load_compara_bytes(ctx)
    ctx.storage.put_bytes(f"raw/{target}/compara.zip", raw)
    labels, mode = _derive_labels(ctx, raw)
    labelled = set(labels)
    ctx.log("labels", mode=mode, n_labelled=len(labelled))

    # 2) LINCS metadata (bounded read) + landmark genes.
    sig, pert = _load_lincs(ctx)
    gene_info = _load_gene_info(ctx)
    landmark_gene_ids, feature_names = select_landmark_genes(
        gene_info,
        landmark_flag_col=ctx.options.get("gene_landmark_col") or _GENE_LANDMARK_COL,
        gene_id_col=ctx.options.get("gene_id_col") or _GENE_ID_COL,
        gene_symbol_col=ctx.options.get("gene_symbol_col") or _GENE_SYMBOL_COL,
    )
    if not landmark_gene_ids:
        raise JobError("no landmark genes selected from gene_info (check the landmark flag column)")

    # 3) sig_meta overlap = the SINGLE source of the sig_id list (metadata only, no matrix).
    pert_to_ik: dict[str, str] = {}
    for pid, key in zip(pert["pert_id"].astype(str), pert["inchi_key"], strict=False):
        nk = _norm_ik(key)
        if nk:
            pert_to_ik[pid] = nk
    trt = sig[sig["pert_type"].astype(str) == "trt_cp"].copy()
    sig_meta = assemble_sig_meta(trt, pert_to_ik, labelled, cell_lines=cells)
    ctx.log("sig_meta", n_signatures=len(sig_meta), cell_lines=cells)

    if sig_meta.empty:
        # The job RAN but the chosen context has zero labelled-compound signatures: a valid
        # data outcome (failed_qc), not a runner error. Nothing to slice or persist.
        summary = f"{target} extract: failed_qc; 0 overlapping signatures in cells {cells}"
        ctx.log("empty_overlap", cells=cells)
        return JobOutcome(report_key=None, summary=summary, verdict="failed_qc")

    chosen = select_condition_signatures(sig_meta)  # one signature per (compound, cell)
    chosen_sig_ids = list(chosen["sig_id"].astype(str))
    n_compounds = int(chosen["compound_key"].nunique())
    ctx.log("condition_selected", n_chosen=len(chosen_sig_ids), n_compounds=n_compounds)

    # 4) Manifest cache check — reuse the curated parquet if an identical slice was made.
    gctx_path = ctx.options.get("gctx_path")
    key = _cache_key(str(gctx_path or ""), chosen_sig_ids, landmark_gene_ids, cells, mode)
    if ctx.storage.exists(manifest_key) and ctx.storage.exists(curated_key):
        try:
            cached = json.loads(ctx.storage.get_text(manifest_key)).get("cache_key")
        except Exception:  # noqa: BLE001 - a corrupt manifest just forces a re-slice
            cached = None
        if cached == key:
            ctx.log("cache_hit", curated_key=curated_key, n_compounds=n_compounds)
            summary = f"{target} extract: cached; {n_compounds} compounds reused (slice skipped)"
            return JobOutcome(report_key=curated_key, summary=summary, verdict="cached")

    # 5) Batched, chunk-cache-bounded slice of ONLY the chosen sig_ids x landmark genes.
    if not gctx_path or not Path(gctx_path).exists():
        raise JobError(
            "a local LINCS Level-5 .gctx is required to slice (pass --gctx-path); the server "
            "stages it, CI uses a synthetic fixture."
        )
    batch_size = int(ctx.options.get("slice_batch") or DEFAULT_SIG_BATCH)
    rdcc_nbytes = int(ctx.options.get("rdcc_bytes") or DEFAULT_RDCC_NBYTES)
    ctx.log("slice_start", n_sigs=len(chosen_sig_ids), batch_size=batch_size, rdcc=rdcc_nbytes)
    matrix = slice_gctx_landmark(
        Path(gctx_path),
        row_ids=landmark_gene_ids,
        col_ids=chosen_sig_ids,
        batch_size=batch_size,
        rdcc_nbytes=rdcc_nbytes,
    )
    # Reindex defensively so the positional ``feature_names`` labels can never be misassigned.
    matrix = matrix.reindex(index=list(landmark_gene_ids))
    expr = matrix.T  # (sig_id x genes), columns now in landmark order
    expr.columns = list(feature_names)

    # 6) Early-fuse -> one 978-vector per compound; write the FUSED compound_id contract.
    merged = chosen.merge(expr, left_on="sig_id", right_index=True)
    fused = early_fuse(merged, feature_names)
    fused = fused.rename(columns={"compound_key": "compound_id"})  # signature_retriever contract

    buf = io.BytesIO()
    fused.to_parquet(buf, index=False)
    ctx.storage.put_bytes(curated_key, buf.getvalue())
    manifest = {
        "target": target,
        "cache_key": key,
        "n_compounds": int(len(fused)),
        "n_signatures_sliced": len(chosen_sig_ids),
        "n_landmark_genes": len(feature_names),
        "cell_lines": cells,
        "mode": mode,
        "curated_key": curated_key,
    }
    ctx.storage.put_text(manifest_key, json.dumps(manifest, indent=2))
    ctx.log("extracted", curated_key=curated_key, n_compounds=int(len(fused)))

    summary = (
        f"{target} extract: extracted; {len(fused)} compounds x {len(feature_names)} genes "
        f"from {len(chosen_sig_ids)} signatures (cells {cells}, mode {mode})"
    )
    return JobOutcome(report_key=curated_key, summary=summary, verdict="extracted")
