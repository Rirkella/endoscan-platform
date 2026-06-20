# ER real-data run — Phase 2 procedure (describe; do NOT run in CI)

Phase 1 (the PR that adds this file) ships the real-data-readiness **code**, tested
on fixtures in CI. Phase 2 is a **one-time, human-run** procedure that produces the
real ER artifacts and a committed registry entry. It is **not** run by Claude Code
and **not** run in CI.

**Where it runs.** On the **human's machine**, because the DVC remote is a local
folder inside a OneDrive-synced directory (DVC has no native OneDrive backend).
Claude Code delivers the reviewed Phase-1 code + this runbook; a human executes it.

No regulatory-grade claims are made anywhere. The quality gate + explicit human
approval are never bypassed.

---

## 1. Stage the data (local; no live downloads)

Place the real extracts under `data/staged/er/` (git-ignored; DVC-tracked):

| file | source | format | key columns (assumed; confirm before running) |
|---|---|---|---|
| `lincs.parquet` | LINCS L1000 GSE92742 Level 5 | Parquet | `sig_id, pert_id, cell_id, pert_dose, pert_time, <978 landmark genes>` |
| `toxcast.csv` | ToxCast / invitroDB | CSV | `dsstox_sid, casrn, assay_name, target, hitcall, ac50` |
| `tox21.csv` | Tox21 qHTS | CSV | `casrn, pubchem_cid, assay_name, target, activity` |
| `cerapp.csv` | CERAPP (ER consensus) | CSV | `casrn, inchikey, target, consensus_call, consensus_score` |
| `pubchem.csv` | PubChem / UniChem mapping | CSV | `input_id, input_id_type, inchikey, cid, smiles, mapping_confidence` |

The staged adapter resolves each source by `source.id` (`<id>.parquet` preferred,
else `<id>.csv`). The retriever parsers normalize these to the internal schema; if a
real column name differs, adjust the relevant parser (a small, documented change)
and confirm with a header sample first.

### LINCS `.gctx` → Parquet (one-off)

LINCS Level-5 ships as a GCToo `.gctx`. Convert it once to the tidy Parquet above.
Using `cmapPy` (install in a throwaway venv; it is NOT a project dependency):

```python
# scratch conversion — run once on the human's machine, not committed as app code
from cmapPy.pandasGEXpress.parse import parse  # pip install cmapPy

g = parse("GSE92742_Broad_LINCS_Level5.gctx", rid=LANDMARK_GENE_IDS)  # 978 landmark rows
sig = g.data_df.T                      # rows = signatures, cols = landmark genes
meta = g.col_metadata_df              # sig_id index + pert_id/cell_id/pert_dose/pert_time
out = meta.join(sig)
out = out.rename(columns={...})        # map LINCS names -> sig_id,pert_id,cell_id,pert_dose,pert_time
out.reset_index().to_parquet("data/staged/er/lincs.parquet", index=False)
```

Filter to ER-relevant perturbagens / the compounds you have labels for to keep it
small. Keep the landmark-gene column names stable (they become the model's
`feature_schema.json`).

## 2. Configure the run

Copy the template and edit locally (do not commit a filled real config with
`approved: true`):

```bash
cp pipelines/endpoints/ER/config.real.example.yaml pipelines/endpoints/ER/config.real.yaml
```

- `data.adapter: staged`, `data.staged_dir: data/staged/er`.
- `gate.thresholds_path: registry/data/quality_gates.yaml` (strict).
- `evaluation.mode: nested` (default; switch to `holdout` only if N is large).
- `training.validated_mvp_floors` — **finalize against the real positive
  prevalence** (AUPRC random baseline = prevalence). Starting values:
  `auroc 0.75`, `auprc 0.50`, `balanced_accuracy 0.65`, `brier ≤ 0.20`.
- `approval.approved: false` for now.

## 3. Configure the DVC remote (machine-local, OneDrive-synced)

DVC pushes to a **local folder inside the OneDrive-synced directory**; the OneDrive
desktop client syncs it to the cloud. The machine-specific path lives in
**git-ignored** `.dvc/config.local` — never commit a personal path to `.dvc/config`.

```bash
dvc remote add --local er_onedrive "/Users/<you>/OneDrive/endoscan-dvc"
dvc remote default --local er_onedrive
```

This is temporary early-stage storage; the remote URL can later be swapped for
S3/Azure with **no code change**. CI never configures or pulls this remote.

## 4. Run the pipeline (gate + approval enforced)

```bash
uv run python pipelines/endpoints/ER/run.py --config pipelines/endpoints/ER/config.real.yaml
```

First run with `approved: false` to inspect the gate verdict + the written dataset
card. If the gate **passes**, review it, then set `approved: true` and re-run. The
runner will:
- build the candidate table (compound-level split plan),
- compute the **honest** estimate (nested grouped CV; outer = honest, inner =
  selection; every split group-disjoint),
- score all five sklearn candidates and pick the simplest within tolerance,
- write `models/ER/{model.pkl, feature_schema.json, metrics.json, model_card.md,
  dataset_card.md, model_selection.json, model_selection.md}`,
- register an ER entry (`validated_mvp` if floors met on the honest estimate, else
  `experimental`).

`metrics.json` reports the **honest nested-outer estimate** for the chosen model —
never resubstitution/inner-CV — and the floors are checked against it.

## 5. Review BEFORE committing the registry entry

A human reviews `model_selection.{json,md}`, `metrics.json`, the dataset + model
cards, and the chosen status; approve (or override) the final model pick. Do not
proceed if anything looks off — fail honestly and report.

## 6. DVC-push binaries + git-commit the text contract

```bash
dvc add data/staged/er models/ER/model.pkl
dvc push
git add models/ER/model.pkl.dvc data/staged/er.dvc \
        models/ER/metrics.json models/ER/feature_schema.json \
        models/ER/model_card.md models/ER/dataset_card.md \
        models/ER/model_selection.json models/ER/model_selection.md \
        registry/models/endpoints.json
git commit -m "ER real endpoint (Phase 2): registered <status> with DVC artifacts"
```

Git-tracked: the `endpoints.json` entry, `metrics.json`, `feature_schema.json`,
both cards, the selection report, and the `.dvc` pointers. DVC-tracked: the binary
`model.pkl` and the staged extracts. Open this as its **own reviewed PR**.

## 7. CI stays fixture-only

CI never stages real data, never configures/pulls the DVC remote, and never
retrains. It runs `uv sync --frozen` + ruff + pytest on the fixtures exactly as for
every prior milestone.
