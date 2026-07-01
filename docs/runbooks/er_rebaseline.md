# ER re-baseline rebuild — server runbook (Route B)

The original ER `model.pkl` + staged `lincs.parquet` are unrecoverable (DVC pointers to a
dead machine-local OneDrive remote). This runbook rebuilds ER **from real sources** through
the proven, seeded pipeline as an **honest re-baseline** — documented, not a silent
substitution. **The rebuilt metrics WILL differ from the original recorded values** (the
original extraction params weren't recorded); that is the accepted re-baseline.

This is a **server** procedure (heavy: 22 GB gctx slice + train). This repo PR ships only
the **prerequisites** — the ER `recipe.yaml`, the concrete staged `config.real.yaml`, and
this runbook. **It does NOT un-freeze ER, does NOT run the build, and does NOT commit
rebuilt artifacts.** Those land in a **second, reviewed PR** after the server run.

## What is already present (repo)
- `data/staged/er/cerapp.csv` — real CERAPP experimental **functional** ER calls (7,219 rows), committed.
- `data/staged/er/pubchem.csv` — CASRN→InChIKey mapping (7,219 rows), committed.
- `pipelines/endpoints/ER/run.py` — the generic, seeded pipeline runner.
- `pipelines/endpoints/ER/{recipe.yaml, config.real.yaml}` — added by this PR (staged, `approved: false`).
- `registry/data/quality_gates.yaml` — the strict gate (UNCHANGED).

## What is missing (re-extract on the server)
- `data/staged/er/lincs.parquet` — the fused MCF7/A549 signature matrix.

## Route B — re-extract `lincs.parquet` (proven CERAPP + gctx path)

Use the existing, unit-tested staging transforms in `pipelines/endpoints/ER/staging/`
(`stage_er.py` + `condition.py` + `gctx.py`) — the exact path that built the original ER.
Do **not** use `extract_signatures` here (its label step is CoMPARA-SDF-specific; generalizing
it for CERAPP is a separate later consolidation, deliberately not bundled into this
integrity-sensitive rebuild).

On the server (LINCS `sig_info`/`gene_info`/`pert_info` + the Level-5 gctx present):

```python
# scratch conversion — server only, not committed as app code
import pandas as pd
from pipelines.endpoints.ER.staging import stage_er   # assemble_sig_meta, select_landmark_genes, build_lincs_parquet

sig_info  = pd.read_csv("<LINCS>/GSE92742_Broad_LINCS_sig_info.txt",  sep="\t", low_memory=False)
gene_info = pd.read_csv("<LINCS>/GSE92742_Broad_LINCS_gene_info.txt", sep="\t", low_memory=False)
pert_info = pd.read_csv("<LINCS>/GSE92742_Broad_LINCS_pert_info.txt", sep="\t", low_memory=False)

# CERAPP-labelled InChIKeys = the labelled compound set (functional call already in cerapp.csv)
cerapp = pd.read_csv("data/staged/er/cerapp.csv")
labelled = set(cerapp.loc[cerapp["label"].notna(), "inchikey"].astype(str))

pert_to_ik = {str(p): str(k) for p, k in zip(pert_info["pert_id"], pert_info["inchi_key"]) if str(k)}

# ONE signature per (compound, cell) over MCF7/A549 whose compound is CERAPP-labelled
sig_meta = stage_er.assemble_sig_meta(
    sig_info, pert_to_ik, labelled,
    sig_id_col="sig_id", pert_id_col="pert_id", cell_id_col="cell_id",
    dose_col="pert_dose", time_col="pert_time",   # NUMERIC columns (avoid the string/idose collision)
    cell_lines=("MCF7", "A549"),
)
gene_ids, feature_names = stage_er.select_landmark_genes(
    gene_info, landmark_flag_col="pr_is_lm", gene_id_col="pr_gene_id", gene_symbol_col="pr_gene_symbol",
)
stage_er.build_lincs_parquet(
    sig_meta, gctx_path="<LINCS>/GSE92742_...Level5....gctx",
    landmark_gene_ids=gene_ids, feature_names=feature_names,
    out_path="data/staged/er/lincs.parquet",
)
```

> NOTE — dose/time column: pass the **numeric** `pert_dose`/`pert_time` columns (post the
> PR #35 collision lesson), so the ~10 µM/24 h condition preference is operative. The
> original run's exact column wiring is unrecorded — this is one reason the rebuild will not
> hash-match the original.

## Memory assessment on the 3.7 GB server (Route B, non-batched `staging/gctx.py`)

`slice_gctx_landmark` does a **partial HDF5 read**, never the full ~23 GB matrix. Estimated peak:
- `handle["/0/META/COL/id"][:]` decode — all ~473,647 signature ids → Python strings + dict ≈ **50–150 MB**.
- `sig_info` full pandas read (~474k rows) ≈ **200–400 MB** (the dominant metadata term).
- The hyperslab `dset[sig_sorted, :]`: ER overlap ≈ 959 compounds; condition-selection keeps
  ≤1 signature per (compound, cell) over {MCF7, A549} → **≲ ~1,900 signatures**. Block =
  ~1,900 × 12,328 genes × 4 B ≈ **~94 MB** (float32); gene-subselect to 978 → ~7 MB fused.
- Training (~959 × 978 fused) + nested CV over 5 models + bootstrap ≈ a few hundred MB.

**Verdict: FITS comfortably — estimated peak ≈ 0.5–0.9 GB, well under 3.7 GB** (reference:
the AR batched run peaked at 422 MiB; ER unbatched adds a single ~94 MB block plus the
same-order metadata).

**One caveat (CANNOT fully confirm from repo — the real gctx's HDF5 chunk layout isn't in
CI):** the non-batched slicer reads ~1,900 scattered rows in a **single** `dset[sig_sorted, :]`
call; if the on-disk chunking amplifies scattered-row reads, transient RSS could exceed the
~94 MB result. Even 5–10× amplification stays ≲ 1 GB. **Action:** monitor RSS during the
slice; if it approaches the ceiling, STOP and fall back to the batched `extract_signatures`
slicer (Route A) rather than risk an OOM. As assessed, no fallback is expected to be needed.

## Rebuild + register (SECOND PR — after this one; NOT done here)

The registration path is guarded: ER is `frozen: true`, so `register_or_update_endpoint`
raises `FrozenEndpointError`. The re-baseline therefore requires a **deliberate, reviewed
un-freeze**, in this exact order (all in the second PR, never leaving ER transiently
un-frozen across PRs):

1. **Gate first (safe, no registration):** `agent.start_build(<er recipe>)` → dataset gate →
   `awaiting_approval` (PASS) or `failed_qc`. Human reviews the gate verdict + dataset card.
2. **Un-freeze (integrity checkpoint):** a human edits `registry/models/endpoints.json`,
   ER `frozen: true → false`. **Reviewed edit — never an automatic flip in the pipeline.**
3. **Promote:** `approve(...)` → `promote(...)` → `run_pipeline` trains + evaluates (per-fold
   + bootstrap CI + evidence-based `_status_for`, PR #37) and `register_or_update_endpoint`
   overwrites ER (now non-frozen), preserving `created_at`, stamping `updated_at`.
4. **Document + commit (Option A):** commit the rebuilt ~1.5 MB `model.pkl` directly (stop
   DVC-tracking it: remove `model.pkl.dvc`, un-ignore the path), plus the new `metrics.json`
   and the re-baselined `model_card.md` — which must state plainly: *"Rebuilt YYYY-MM-DD from
   source (CERAPP labels + LINCS MCF7/A549 re-extracted from the real gctx) through the
   reproducible gated pipeline, after the original model binary became unavailable. Metrics
   reflect this rebuild and may differ from the original recorded values."*
5. **Re-freeze (same PR):** ER `frozen: false → true`. Verify model ↔ feature_schema (978
   genes) and metrics ↔ card before finalizing.

**Expected status (report whatever it honestly earns):** ER had ≈73 positives (≥ the
`MIN_POS_TOTAL=30` / `MIN_POS_PER_FOLD=5` min-evidence gate) but AUROC/AUPRC around the
original 0.74/0.26 → the CI lower bounds likely fall short of the 0.75/0.50 floors → **stays
`experimental`**. Do not pre-judge; the rebuilt metrics may differ.

## What must NOT change
`registry/data/quality_gates.yaml` thresholds; the gate/approval/chokepoint logic; the
evidence-based status logic; AR's entry. Only ER's artifacts/metrics/card + the `frozen`
flag (un-freeze→re-freeze) change — deliberately, in the second reviewed PR.

Original reference: `docs/runbooks/er_phase2_colab.md`, `docs/er_real_data_phase2.md`.
