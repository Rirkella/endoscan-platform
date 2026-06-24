# ER Phase 2 — Colab operator runbook

Operator guide for `notebooks/colab_run_er_phase2_clean.ipynb`: the one-time real ER
run on staged LINCS + CERAPP data, in a Colab CPU runtime. You **paste reviewed values**
into the two CONFIG cells and run **one stop at a time**, copying the printed outputs to
Claude Chat at each STOP. You never edit Python — every cell calls merged, tested code.

This is a pre-screening / prioritization tool. **No regulatory-grade claims.**

## Before you start
- Set the Colab Secret **`GH_PAT_RO`** to a fine-grained, read-only, single-repo PAT
  (`Rirkella/endoscan-platform`, Contents: Read-only, short expiry).
- Use a standard CPU runtime. The only heavy step (Stop 2c) needs ~40 GB of free disk.

## Preflight + Setup (run once, top to bottom)
| Cell | What it does | Notes |
|------|--------------|-------|
| `Preflight)` | Verifies the PAT via the GitHub API | token is checked, never printed |
| `Setup-clone)` | Read-only clone + installs the numpy-2 stack + rdkit | rdkit is staging-only; ~2-3 min |
| `Setup-import)` | In-kernel `import endoscan_core` | must print `endoscan_core import OK` |
| `Setup-drive)` | Mounts Drive + configures the DVC local remote | approve the Drive prompt |

## Stop 1 — light inspect (~3-5 min)
Run **`1c)`**. It fetches + inspects `sig_info`/`gene_info`/`pert_info` and the CERAPP
experimental archives, prints the landmark count, and reports the **expected** gctx shape
from its filename. **It does not download the 19.9 GB gctx.** It halts if no readable
CERAPP table is found.

**Send Claude Chat:** the printed headers + row counts, the landmark count (`pr_is_lm`),
the CERAPP table manifest (with experimental-vs-consensus hint), and the expected gctx
shape. Wait for the confirmed column names + chosen CERAPP table.

## Stop 2 — labels, identity, join, guard, gate (CONFIG, then run 2a → 2b → 2c → 2d)
1. **`2a-config)`** — paste the column names + chosen `CERAPP_TABLE` Claude Chat gave you
   (`CERAPP_LABEL_MODE` stays `'binding'`). Values only, no logic.
2. **`2a)`** (~1-2 min) — reconstructs one ER label per compound and derives a full
   InChIKey per compound (InChI→InChIKey via rdkit; CASRN→PubChem only as a fallback).
   Writes `cerapp.csv` + `pubchem.csv`. Prints labels + identity-resolution counts.
3. **`2b)`** (~1 min) — joins CERAPP InChIKeys to LINCS `pert_info` (full key, `-666`
   skipped), filters MCF7/A549, and prints the per-hop counts + a full-vs-prefix key
   check. It ends with a **hard guard**.
4. **`2c)`** — the **heavy** step (**~20-40 min**: ~20 GB download, ~40 GB decompressed):
   downloads, slices, and fuses the gctx into `lincs.parquet` (`compound_id` + 978 genes).
5. **`2d)`** (~1-2 min) — runs the quality gate only; **training is impossible here**
   (`approved` is hard-coded `False`). Prints the gate summary + the dataset card.

### The guard (why 2c may be skipped)
`2b` reads `min_overlap` from `registry/data/quality_gates.yaml` and permits the gctx
download **only if** `sig_ids > 0` **and** `overlap >= min_overlap` **and** both class
counts (`pos`, `neg`) are `> 0`. If any fails, `2b` raises `SystemExit` naming the last
non-zero hop — **`2c` never downloads the 20 GB gctx**, so a join/identity problem is
fixed without wasting the download. `2c` re-asserts the same predicate at its top
(defense in depth) before fetching.

**Send Claude Chat:** CERAPP labels; identity resolved (+ from_inchi/fallback/unresolved);
matched LINCS InChIKeys; matched pert_ids; selected MCF7/A549 sig_ids; pre-heavy overlap +
class split; `lincs.parquet` shape + NaN check; the gate summary; the dataset card.

## Stop 3 — train (explicit human approval)
1. **`3a-config)`** — paste the metric floors Claude Chat gave you and set `APPROVED = True`.
2. **`3b)`** (a few minutes) — re-gates, then trains with the honest nested grouped-CV
   estimate + a simplicity-aware scorecard, and writes the artifacts + the proposed
   registry entry.

**Send Claude Chat:** `metrics.json`, `model_selection.json` / `.md`, `model_card.md`,
`dataset_card.md`, `feature_schema.json`, and the proposed `endpoints.json` entry.
**Do not push or commit yet.**

## Stop 4 — DVC push + review bundle (no GitHub push)
Run **`4)`**. It DVC-pushes the binaries to the Drive remote and downloads a small review
bundle (text artifacts + `.dvc` pointers + the proposed `endpoints.json`). **This stop
never pushes to GitHub and never runs git.** Hand the downloaded bundle to Claude Code,
which opens the review PR (text + pointers only); Claude Chat reviews the diff and you merge.

## If something looks wrong
Do not edit Python. Copy the failing cell's output to Claude Chat — the per-hop counts in
`2b` localize where compounds drop, and the guard message names the last non-zero hop.
