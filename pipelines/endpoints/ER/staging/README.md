# ER staging layer (offline prep — cloud-run, never the laptop)

This directory is the **explicit offline prep layer** that fetches from the
**approved** locators in `registry/data/sources.yaml` and writes small **staged
local files** that the toolchain then reads via `StagedSourceAdapter`. It is
**separate from the toolchain**: the pipeline never downloads, and
`RealDownloadAdapter` stays a stub.

## What runs where
- **CI (this repo):** the pure transforms are unit-tested on tiny fixtures /
  a synthetic `.gctx` — `gctx.py` (h5py slicer), `condition.py` (10 µM/24 h
  selection + MCF7/A549 early fusion), `cerapp.py` (experimental-call parser),
  `pubchem.py` (mapping normalizer). **No real data, no network.**
- **Cloud (Colab, operator-run):** `notebooks/colab_run_er_phase2_clean.ipynb` is a
  **four-stop** operator runbook (NOT "Run all"); see `docs/runbooks/er_phase2_colab.md`.
  A **Preflight** cell verifies a **read-only,
  single-repo** fine-grained PAT (Colab Secret `GH_PAT_RO`) before anything else;
  the clone is **read-only and token-safe** (PAT supplied via `GIT_ASKPASS`, never
  in the URL/argv/`.git/config`/output) and the notebook **never pushes to GitHub**.
  The operator only pastes confirmed values into the Stop 2 / Stop 3 CONFIG cells and
  sends outputs to Claude Chat between stops: Stop 1 fetch+inspect, Stop 2 stage +
  **gate only** (training hard-wired off), Stop 3 train, Stop 4 DVC-push the binaries
  to Drive + download a **review bundle** (text artifacts + `.dvc` pointers +
  proposed `endpoints.json`) that Claude Code turns into the review PR. `fetch.py`
  downloads the approved sources; `stage_er.py`'s tested helpers build
  `data/staged/er/`; `bundle.py` assembles the Stop-4 handoff bundle.

## Key rules
- **CERAPP = EXPERIMENTAL ER activity calls only** — never the consensus-model
  *predictions* for the ~32k chemicals (that would make EndoScan mimic a
  structure-based QSAR model; transcriptomics-first violation). See `cerapp.py`.
- **Condition rule:** prefer 10 µM / 24 h; if a compound+cell lacks it, fall back
  to the nearest available dose/time (documented order) — compounds are **not**
  dropped (ER-labelled compounds are scarce). See `condition.py`.
- **Early fusion:** mean MCF7+A549 landmark profiles → one **978-vector per
  compound**; `group = compound` (InChIKey). Hackathon late fusion is the noted
  alternative. **FLAGGED for confirmation.**
- **Imbalance:** the models use `class_weight="balanced"` (GB via per-fit
  `sample_weight`) — handled in `endoscan_core.training`, not here.

## gctx reader (h5py only — no cmapPy, no NumPy<2)
- **`h5py` is the sole gctx reader**, used identically in CI (synthetic `.gctx`) and
  the real Colab run. There is **no cmapPy** and **no `numpy<2` pin** — cmapPy needs
  the removed `numpy.string_` and would downgrade Colab's NumPy 2 against its
  preinstalled numpy-2 pandas/h5py/pyarrow (an ABI break). Dropping it removes that
  failure mode and means CI tests the exact reader the real run uses.
- **`gctx.slice_gctx_landmark`** auto-detects matrix orientation (matches each matrix
  dimension to `len(ROW/id)` vs `len(COL/id)`; LINCS dims differ so it is
  unambiguous), validates every requested id is present (clear error otherwise), and
  does a **partial HDF5 read** of only the requested signature slices — never the full
  matrix. `h5py` is a dev/staging-only dependency (CI), never an `endoscan_core`
  runtime dependency.

## Storage / DVC (cloud)
OneDrive-local is not cloud-reachable; for the cloud run use a **Colab-mounted
Google Drive path as a DVC *local* remote** (no creds in the repo; path in
git-ignored `.dvc/config.local`), e.g. `dvc remote add --local er_gdrive
/content/drive/MyDrive/endoscan-dvc`. `gdrive://` (OAuth) is the documented
alternative. DVC-track `data/staged/er/` + `models/ER/model.pkl`; git-track the
scripts, `sources.yaml`, small label/mapping CSVs, `metrics.json`, cards, the
`model_selection` report, the `endpoints.json` entry, and `.dvc` pointers.

## Phase 2b is a separate, reviewed step
The cloud run produces the real ER + artifacts; a human reviews metrics, the
scorecard, leakage, and cards (no overclaim) **before** the `endpoints.json`
entry lands in its own PR.
