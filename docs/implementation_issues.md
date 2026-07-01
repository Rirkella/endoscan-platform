# EndoScan — Implementation Issues (M0 → M9)

> Clean-repository implementation plan. Execute **one issue at a time**, each as a single PR, reviewed and
> merged before the next begins. Governing rules: [`../PROJECT_RULES.md`](../PROJECT_RULES.md).
> Agent operating manual: [`../CLAUDE.md`](../CLAUDE.md). Vision: [`project_vision.md`](project_vision.md).

**Cross-cutting non-negotiable rules (apply to every issue):**
- EndoScan is **transcriptomics-first**: core logic is `transcriptomic signature → endocrine risk`.
- All science logic lives in `packages/endoscan_core`. The API stays thin; the agent calls tested core tools.
- SMILES-to-transcriptomics is **experimental/upstream only**. **No direct SMILES-to-toxicity. Ever.**
- Source discovery uses the allow-list in `registry/data/sources.yaml` — **no open-web scraping**.
- Training and registration are **gated** by quality thresholds and optional human approval.
- Every registered endpoint must eventually have a **model card**. Reports must always include **limitations**.
- **Never** claim regulatory-grade validation. No Streamlit. No notebook-only deliverables.
- **Never fabricate data or hardcode/loosen tests or gates to pass them.** Fail honestly; stop and ask when blocked.

**Data scope for M0–M4:** no live/large downloads. Use small bundled fixtures under `tests/fixtures/`. Real
source-download adapters are stubbed/TODO and wired in a later dedicated issue. When you create fixtures,
**document their schema explicitly and flag it in the PR for human review** — do not silently invent shapes.

---

## Status snapshot — merged reality (as of PR #44)

> **This section is the authoritative status.** The numbered issue specs further below are the
> ORIGINAL plans, kept for provenance and annotated with a status banner where reality has moved
> on. Treat this as a **point-in-time snapshot** (taken at the merge of PR #44); verify against the
> merge history via the cited PRs rather than trusting it to stay current.

**Milestones**

| Milestone / feature | Status | Evidence |
|---|---|---|
| M0 — scaffolding, tooling, CI | ✅ complete | Issue 1 |
| M1 — model/dataset registry contract | ✅ complete | Issue 2 |
| M2 — dataset-construction toolchain | ✅ complete | Issue 3 |
| M3 — ER endpoint via the toolchain | ✅ complete (ER `experimental`) | Issue 4 |
| M4 — inference + explainability (+ `LimitationsBlock`) | ✅ complete | Issue 5 |
| M5 — Builder Agent + approval chokepoint | ✅ complete | PR #27 |
| M5.5 — job-runner (`endoscan_jobs`) | ✅ complete | `coverage` + `extract_signatures` jobs; memory-efficient LINCS gctx slicing |
| CoMPARA SDF parser + the five modes | ✅ complete | `functional_modulation` = agonist ∪ antagonist, **binding excluded** |
| Evidence-based, sample-aware status logic | ✅ complete | PR #37 — grouped-bootstrap CI + min-evidence gate (`MIN_POS_TOTAL=30`, `MIN_POS_PER_FOLD=5`, CI-lower-bound rule) |
| Registry re-registration + frozen-endpoint protection | ✅ complete | PR #38 — `register_or_update_endpoint`, `FrozenEndpointError` |
| M6 — second endpoint (AR) through the gated agent | ✅ complete (AR `experimental`) | PRs #36, #40 |
| ER recovery / re-baseline (proven reproduction) | ✅ complete | PRs #39, #41 |
| M7 — thin serving API (read-only FastAPI) | ✅ complete | PR #42 |
| M8 — web frontend (data-driven React SPA) | ✅ complete | PRs #43, #44 |
| M9 — report engine + docs + MVP packaging | ⏳ not yet implemented | — |

**Hardening milestones (unroadmapped when the original plan was written, now recorded above):**
the job-runner (`endoscan_jobs`), the CoMPARA parser + five-mode label logic, the evidence-based
status logic (PR #37), and registry re-registration + frozen protection (PR #38). These were built
between M5 and M8 and are the machinery behind the current endpoint statuses.

**Current endpoints (snapshot — verify against the cited PRs):**

- **ER** (Estrogen Receptor) — `experimental`, **frozen**, serve-ready from a **committed** `model.pkl`
  (Option A, no DVC). It is a **proven byte-identical REPRODUCTION** of the lost original (md5
  `5bed2e0d…`): the original binary was unavailable (DVC remote dead), deterministically regenerated
  from source (committed CERAPP experimental-functional labels + re-extracted LINCS MCF7/A549
  signatures, seed 0), through the reproducible gated pipeline — **point metrics unchanged** from the
  original record. Landed via a deliberate un-freeze → re-register → re-freeze (PRs #39, #41). This is
  a faithful reproduction, **not** a re-baseline with changed numbers.
- **AR** (Androgen Receptor) — `experimental`, not frozen, serve-ready from a **committed** `model.pkl`
  (Option A). Built through the gated Builder Agent over CoMPARA (`functional_modulation`, binding
  excluded, VCaP/LINCS context); it was **honestly demoted** `validated_mvp` → `experimental` under
  the evidence-based CI rule (PRs #36, #40).
- Both are `experimental` for the **same honest reason**: under the evidence-based logic (PR #37) their
  95% CI lower bounds miss the `validated_mvp` floors (thin positive counts), even where a point
  estimate looks higher. **Thresholds and the gate were never weakened** to change this.
- **Serving:** both load from the committed binary via the M7 API (`/predict` + `/explain`); ER is
  explained with **TreeSHAP**, AR with **linear_coefficient** attribution. The registry stays **one row
  per endpoint** (the variant-forward-compatible shape lives in the API response, not the registry).

**Note on reporting (M4 vs M9):** M4 shipped the framework-free inference path and the structured
`LimitationsBlock` attached to every prediction/explanation (the no-overclaiming guarantee) — **not**
the full downloadable report engine. The report engine + one-command fresh-clone demo packaging (M9)
remains open.

---

## Issue 1 — M0: Repository scaffolding, tooling, and CI

**1. Title** · `M0 — Repository scaffolding, tooling, and CI`

**2. Goal** · Stand up a clean monorepo skeleton where `endoscan_core` is an installable package, with linting, testing, and green CI, so every later issue builds on a stable, reproducible foundation.

**3. Background / why this matters** · This is a clean reimplementation of a validated hackathon concept. Before any science or agent logic we need correct boundaries (one installable core package that both the API and the agent import), reproducible dependency management, and automated checks. Getting packaging and CI right now prevents `sys.path` hacks and dependency drift, and locks in the architecture everyone codes against.

**4. Exact scope**
- Create the full monorepo directory skeleton (empty, with `.gitkeep` placeholders).
- Root `pyproject.toml` managed by `uv`, declaring `endoscan_core` as a workspace/editable package.
- `packages/endoscan_core/` importable, with its own `pyproject.toml` and `__init__.py` exposing `__version__`.
- Tooling: `ruff` (lint + format), `pytest`, `pre-commit`, optional `mypy`.
- GitHub Actions workflow running `ruff` + `pytest` on push and PR.
- `.gitignore` (ignore `models/` binaries, raw data, `.env`, caches), `.env.example`, `LICENSE`, root `README.md` (already provided), and the context files (already provided).
- `dvc init` (config only — no artifacts) so `models/` binaries are DVC-tracked, not in git.
- One trivial passing smoke test.

**5. Files / folders to create**
```
pyproject.toml   .pre-commit-config.yaml   .gitignore   .env.example   docker-compose.yml
.github/workflows/ci.yml
packages/endoscan_core/pyproject.toml
packages/endoscan_core/endoscan_core/__init__.py
tests/test_smoke.py
registry/data/.gitkeep    registry/models/.gitkeep
models/.gitkeep           pipelines/.gitkeep
services/api/.gitkeep     apps/web/.gitkeep    agent/.gitkeep
docs/.gitkeep             notebooks/.gitkeep   reports/.gitkeep
legacy/README.md          # pointer to the hackathon repo (reference only)
.dvc/                     # from `dvc init`
```

**6. Acceptance criteria**
- `uv sync` installs cleanly; `python -c "import endoscan_core; print(endoscan_core.__version__)"` works.
- `ruff check .` and `ruff format --check .` pass; `pytest` passes.
- CI is green on a PR. DVC initialized; `models/` git-ignored but DVC-tracked.

**7. Tests / checks** · `tests/test_smoke.py` imports `endoscan_core` and asserts `__version__`; CI runs ruff + pytest green.

**8. What should NOT be done** · No science, registry, API, agent, data, models, or pipelines. No frontend implementation, no Streamlit, no notebooks with logic. Empty, correct skeleton only.

**9. Labels** · `M0` · `foundations` · `infrastructure` · `setup`

**10. Dependencies** · None.

---

## Issue 2 — M1: Model & dataset registry contract

**1. Title** · `M1 — Model & dataset registry contract`

**2. Goal** · Define and implement the registry that is the contract between dataset construction, training, and serving — a typed schema plus a thin read/write API — proven with a round-trip on dummy fixtures. No real models yet.

**3. Background / why this matters** · The registry is the single contract linking training and serving; both the API and the agent depend on it. Defining it before any training or serving prevents rework and lets every later component target a stable interface. The `status` lifecycle encodes the human-in-the-loop gate; constraining `input_type` to `transcriptomics` enforces transcriptomics-first at the schema level.

**4. Exact scope**
- Pydantic schema for a registry entry: `endpoint_id`, `biological_target`, `input_type` (**must be** `transcriptomics`), `model_path`, `feature_schema_path`, `metrics_path`, `explainer_path`, `model_card_path`, `dataset_card_path`, `status`, `version`, `created_at`, `source_refs`.
- `status` enum: `candidate` → `dataset_ready` → `under_review` → `validated_mvp` / `experimental` / `failed_qc` / `deprecated`.
- On-disk layout: `registry/models/endpoints.json` index; per-endpoint artifacts under `models/{ENDPOINT_ID}/`.
- Registry API in `endoscan_core/registry/`: `list_endpoints()`, `get_endpoint(id)`, `register_endpoint(entry)`, `update_status(id, status)`, `load_model(id)`.
- Validation: an entry cannot reach `validated_mvp` unless every referenced artifact path exists.
- Templates: `model_card.md`, `dataset_card.md`, `endpoint_config.yaml`.

**5. Files / folders**
```
packages/endoscan_core/endoscan_core/registry/{__init__.py,schema.py,store.py}
packages/endoscan_core/endoscan_core/registry/templates/{model_card.md,dataset_card.md,endpoint_config.yaml}
registry/models/endpoints.json        # {"endpoints": []}
registry/models/model_cards/.gitkeep
tests/registry/test_registry_roundtrip.py
tests/fixtures/registry/              # dummy model.pkl + json stubs (schema documented in PR)
```

**6. Acceptance criteria**
- Register a dummy endpoint (fixture files), read it back via `get`/`list`, update its status.
- Schema **rejects** `input_type != "transcriptomics"`.
- Promotion to `validated_mvp` **fails** if any referenced artifact path is missing.
- Templates exist and render with placeholder fields.
- Any fixture schema introduced is **documented and flagged in the PR for human review**.

**7. Tests / checks** · Round-trip `register → get → list → update_status`; validation (missing-file blocks `validated_mvp`; invalid `input_type`/`status` rejected).

**8. What should NOT be done** · No real training, data, dataset-construction tools, API, or agent. Dummy fixtures only; no real source access or model logic beyond the abstraction + a pickled stub.

**9. Labels** · `M1` · `registry` · `core` · `contract`

**10. Dependencies** · Issue 1 (M0).

---

## Issue 3 — M2: Data-source allow-list and dataset-construction toolchain

**1. Title** · `M2 — Data-source allow-list and dataset-construction toolchain`

**2. Goal** · Implement the deterministic, individually-tested toolchain that, for a given receptor, selects trusted sources, retrieves labels and transcriptomic signatures, maps compounds, computes overlap, builds a candidate training table, produces a dataset quality report + dataset card, and emits a quality-gate verdict — all from a registered allow-list, with **no training triggered**.

**3. Background / why this matters** · This is the substrate of the Builder Agent and the core value (and risk) of EndoScan: endpoint expansion must be agent-assisted from the start. But an orchestrator can only sequence tools that exist and are tested, so these are built first as pure `endoscan_core` data-engineering functions (the agent in M5 sequences them). The allow-list keeps discovery reproducible and forbids open-web scraping. The quality gate is the architectural line before any training. Implements steps 1–8 of the builder workflow.

**4. Exact scope** (each item a separate, tested function)
- `registry/data/sources.yaml` allow-list: per entry `id`, `name` (ToxCast, Tox21, CERAPP, CoMPARA, LINCS …), `type` (`labels` | `signatures` | `mapping`), `access_method`, `provenance`, `version`.
- `source_selector(target)` → candidate sources **from the allow-list only**.
- `label_retriever(target, sources)` → labels + assay source + label confidence.
- `signature_retriever(compounds|target, sources)` → LINCS/L1000 signatures + metadata (cell line, dose, time).
- `compound_mapper(...)` → harmonize CID / InChIKey / SMILES / perturbagen IDs.
- `overlap_computer(...)` → **compound-level** intersection of labels ∩ signatures + counts.
- `candidate_table_builder(...)` → `X` (transcriptomic features), `y` (labels), metadata, **compound-level split plan**.
- `dataset_quality_report(...)` → overlap counts, class balance, duplicate signatures, label conflicts/confidence, metadata coverage, leakage check → writes a **dataset card**.
- `quality_gates(report)` → pass/fail against configurable thresholds (min compounds/class, max duplicate rate, min metadata coverage, compound-level split feasible).
- Source access via **adapters** so real downloads can be added later; for this issue use small bundled fixture extracts so tests are deterministic and offline. **Document each fixture schema and flag it in the PR for human review.**

**5. Files / folders**
```
registry/data/sources.yaml   registry/data/quality_gates.yaml   registry/data/dataset_cards/.gitkeep
packages/endoscan_core/endoscan_core/datasets/__init__.py
packages/endoscan_core/endoscan_core/datasets/{sources.py,source_selector.py,label_retriever.py,
    signature_retriever.py,compound_mapper.py,overlap.py,candidate_table.py,quality_report.py,quality_gates.py}
packages/endoscan_core/endoscan_core/datasets/adapters/   # source + fixture-backed adapters
tests/datasets/                                           # one test per tool
tests/fixtures/datasets/                                  # small offline extracts (schemas documented)
```

**6. Acceptance criteria**
- Given a target receptor + the allow-list, the toolchain produces candidate table + dataset quality report + dataset card + gate verdict — reproducibly and **offline** (fixtures).
- `source_selector` returns **only** sources present in `sources.yaml` and rejects/excludes anything else.
- `overlap_computer` and `candidate_table_builder` use **compound-level** grouping (no compound in both proposed split groups).
- `quality_gates` returns `fail` when thresholds aren't met and `pass` when they are.
- Fixture schemas documented and flagged for human review.

**7. Tests / checks** · Unit test per tool on fixtures; allow-list enforcement; compound-level no-leakage test; gate pass + fail cases.

**8. What should NOT be done** · **No training, registration, inference, SHAP, API, or LLM/agent orchestration** (that is M5). No live large-scale downloads (stub real adapters). No SMILES-to-transcriptomics. The gate only **emits a verdict** — it must not trigger training.

**9. Labels** · `M2` · `datasets` · `agent-tools` · `core` · `data-quality`

**10. Dependencies** · Issues 1 (M0), 2 (M1).

---

## Issue 4 — M3: ER endpoint built through the toolchain (human-in-the-loop)

> **Status — COMPLETE; ER registered as `experimental` (NOT `validated_mvp`).** ER is the
> reference endpoint the rest of the roadmap builds on — it must NOT be re-described as
> validated. **The rationale for `experimental` has since evolved:** the original run recorded
> balanced accuracy 0.579 < a 0.60 floor; under the later **evidence-based, sample-aware status
> logic (PR #37)** the endpoint is `experimental` because its 95% CI lower bounds miss the
> `validated_mvp` floors on thin positives. ER's binary was subsequently **lost (dead DVC remote)
> and deterministically reproduced byte-identical** from source, then committed serve-ready via a
> deliberate un-freeze (PRs #39, #41). See the **Status snapshot** at the top for the current
> reality; the `validated_mvp`/`experimental` wording below describes the gate *logic*, not a claim.

**1. Title** · `M3 — ER endpoint built end-to-end through the toolchain (human-in-the-loop)`

**2. Goal** · Produce the first real registered endpoint — Estrogen Receptor (ER) — entirely via the M2 toolchain plus a **gated** training step, proving the agentic data-construction path works end to end.

**3. Background / why this matters** · ER is the canonical reference endpoint and the proof that the dataset-construction toolchain yields a trainable, leakage-safe dataset. It validates the **tools**; the LLM orchestrator/agent loop is validated later with TR (M6). Training runs only after the quality gate passes and (optionally) a human approves — establishing the gated pattern every future endpoint follows. **In this milestone the toolchain is run through a config-driven pipeline, human-stepped — you do NOT build the LLM Builder-Agent orchestrator here (that is M5).**

**4. Exact scope**
- A config-driven pipeline runner `pipelines/endpoints/ER/` (a **runner, not a notebook**) that calls the M2 tools to build the ER candidate table + quality report + dataset card.
- After gate pass / human approval: training in `endoscan_core/training/` — start with **elastic-net logistic regression + gradient boosting**, selected by cross-validation, using the **compound-level split** from M2.
- Evaluation: AUROC, AUPRC, balanced accuracy, F1, calibration, confusion matrix; write `metrics.json`.
- Generate ER `model_card.md` from the M1 template (sources, counts, features, model type, metrics, limitations, recommended / not-recommended use).
- Register ER via the M1 registry API → `models/ER/` artifacts + `endpoints.json` entry; status `validated_mvp` if metrics clear a set threshold, else `experimental`.
- Features start from LINCS landmark / differential expression — **transcriptomic only**.

**5. Files / folders**
```
pipelines/endpoints/ER/{config.yaml,run.py}
packages/endoscan_core/endoscan_core/training/{__init__.py,train_endpoint.py,evaluate_endpoint.py,splits.py}
packages/endoscan_core/endoscan_core/features/{__init__.py,landmark_features.py}
models/ER/   # model.pkl, feature_schema.json, metrics.json, model_card.md, dataset_card.md (DVC-tracked)
registry/models/endpoints.json   # ER entry added
tests/training/   tests/features/
```

**6. Acceptance criteria**
- Running the ER pipeline reproducibly builds the dataset, passes the gate, trains, evaluates, registers ER.
- ER appears in `endpoints.json` with valid artifact paths and a model card.
- Compound-level split enforced (leakage test passes). `metrics.json` written; status reflects threshold logic.
- Training does **not** run unless the gate verdict is `pass` and the approval flag is set.

**7. Tests / checks** · Feature-builder test on fixtures; compound-level no-leakage test; end-to-end pipeline test on a small fixture dataset producing a registered (fixture) ER entry; gate-blocks-training test (failing gate ⇒ training not invoked).

**8. What should NOT be done** · No inference/serving API (M4/M7), SHAP/explainability (M4), LLM agent orchestration (M5), or second endpoint (M6). No SMILES; no AutoML zoo (only the two model types); no PDF report. Do not bypass the gate or claim regulatory-grade validation.

**9. Labels** · `M3` · `endpoint-ER` · `training` · `pipeline` · `core`

**10. Dependencies** · Issues 1, 2, 3.

---

## Issue 5 — M4: Inference and explainability for registered endpoints

**1. Title** · `M4 — Inference and explainability for registered endpoints`

**2. Goal** · Given a new transcriptomic signature, load a registered endpoint (ER) from the registry, predict endocrine risk with confidence + applicability, produce a biological explanation (SHAP top genes + light pathway interpretation), and assemble a structured report object that **always includes limitations**.

**3. Background / why this matters** · This closes the `transcriptomic signature → endocrine risk` core loop on the serving side, kept framework-free in `endoscan_core` so both the API (M7) and any later component reuse one code path. Explanations are **biological** (genes/pathways) — EndoScan's differentiator over structure-only QSAR. The mandatory limitations section enforces the no-overclaiming rule.

**4. Exact scope**
- `endoscan_core/inference/predict_endpoint.py`: load model + feature schema from the registry; align an incoming signature to the schema (handle missing/extra genes); output probability, calibrated score, confidence, applicability-domain flag.
- `endoscan_core/explain/`: SHAP values → top positive/negative genes; short endpoint-specific interpretation string; optional light pathway-level scoring; optional nearest-neighbour similar-signature lookup (flagged).
- `endoscan_core/reporting/`: assemble a structured report object — input summary, endpoint risk, gene-level explanation, pathway interpretation, confidence/applicability, suggested follow-up, **limitations + disclaimer**. Output structured object + markdown/JSON (**no PDF yet**).
- Input is a **transcriptomic signature only**.

**5. Files / folders**
```
packages/endoscan_core/endoscan_core/inference/{__init__.py,predict_endpoint.py,signature_alignment.py}
packages/endoscan_core/endoscan_core/explain/{__init__.py,shap_explainer.py,similar_signatures.py,pathway_interpreter.py}
packages/endoscan_core/endoscan_core/reporting/{__init__.py,report_builder.py,sections.py}
packages/endoscan_core/endoscan_core/reporting/templates/report.md
tests/inference/   tests/explain/   tests/reporting/
```

**6. Acceptance criteria**
- A sample signature for ER returns a structured prediction (probability, calibrated score, confidence, applicability flag) + SHAP top genes + interpretation.
- The assembled report object **always** contains a non-empty limitations/disclaimer section.
- Missing-gene handling works: alignment degrades gracefully and flags lowered confidence.
- All logic lives in `endoscan_core` — **no FastAPI here**.

**7. Tests / checks** · Inference test on a fixture signature against the (fixture) ER model; alignment test with missing genes; explanation test (SHAP returns ranked genes); report test (limitations section always present and non-empty).

**8. What should NOT be done** · No FastAPI/HTTP (M7), frontend (M8), PDF rendering (M9), agent (M5), second endpoint, or SMILES. No regulatory-grade claims. Keep it framework-free.

**9. Labels** · `M4` · `inference` · `explainability` · `reporting` · `core`

**10. Dependencies** · Issues 1, 2, 3 (needs a registered ER model). Issue 4 strongly recommended complete.

---

## Roadmap to MVP (M5 → M9)

> **MVP definition.** EndoScan's MVP must demonstrate **BOTH**: **(1)** a registered endpoint
> used for inference + explainability (done for ER in M4); **AND (2)** the agent-assisted
> build pipeline reproducing the endpoint-construction workflow over deterministic, tested
> tools — with gates, provenance, cards, and **HUMAN APPROVAL**. The differentiator is
> **reproducible endpoint-building**, not a UI over one classifier.
>
> M0–M4 are complete (history above); ER is registered as `experimental`. **Status update
> (see the Status snapshot at the top): M5 (PR #27), M6/AR (PRs #36, #40), M7 (PR #42), and M8
> (PRs #43, #44) are now COMPLETE and merged; M9 (report engine + packaging) is not yet
> implemented.** The issue specs below are the original plans, each annotated with its status.
> The same rules applied to each and still apply to M9: one issue = one branch = one PR; stop
> after each for review; never weaken or bypass the gate (PROJECT_RULES §3.3), never fabricate
> (§6.1), never claim regulatory-grade validation.

---

## Issue 6 — M5: Builder Agent orchestrator + approval gate

> **Status — COMPLETE (PR #27).** The Builder Agent sequences the tested M2 tools and stops at the
> quality gate behind a structural human-approval token; it reimplements zero science. The
> unroadmapped hardening that grew around it — the `endoscan_jobs` job-runner (M5.5), the CoMPARA
> parser + five-mode label logic, the evidence-based status logic (PR #37), and registry
> re-registration + frozen protection (PR #38) — is recorded in the Status snapshot at the top.

**1. Title** · `M5 — Builder Agent orchestrator + approval gate`

**2. Goal** · An orchestrator (e.g. `agent/builder_agent.py`) that, given a **build recipe** for an endpoint, **SEQUENCES the existing tested M2 tools** — `source_selector` → `label_retriever` → `signature_retriever` → `compound_mapper` → `overlap_computer` → `candidate_table_builder` → `dataset_quality_report` (writes the dataset card) → `quality_gates` verdict — and records build status. It **REIMPLEMENTS ZERO SCIENCE**: it only calls tested `endoscan_core` tools. It **STOPS at the quality gate** and requires an explicit **HUMAN APPROVAL token** before any train/register; on approval it triggers the existing M3 `run_pipeline` (train/evaluate/card/register). It must **NEVER lower thresholds**, **NEVER register without metrics + dataset card + model card + limitations**, and must **preserve provenance / `source_refs`**.

**3. Background / why this matters** · Agent-assisted endpoint expansion is the thesis (PROJECT_RULES §2): the science already exists as individually-tested M2 tools and the M3 gated pipeline, so M5 is **pure orchestration** — it adds no science, only a deterministic call-sequence plus the approval boundary. It re-expresses the §3.2/§3.3 hard boundary at the orchestration layer (no training before a `pass` verdict and a human approval). `agent/` is currently an empty placeholder.

**4. Non-goals** · No new science; no threshold changes; no UI (M8); no API (M7); no forcing a validated endpoint; no second-endpoint data work (M6). (Source *discovery* stays out — PROJECT_RULES §3.1a is propose-only and unimplemented.)

**5. Modules / files likely touched**
```
agent/{builder_agent.py,build_status.py,approval.py,recipes/<ENDPOINT>.yaml}   # new
agent/prompts/.gitkeep                                                          # placeholder
packages/endoscan_core/...        # imported AS-IS (no edits)
pipelines/endpoints/ER/run.py     # run_pipeline imported AS-IS (no edits)
tests/agent/                      # orchestration + boundary tests on fixtures
```

**6. Acceptance criteria**
- Runs on **fixtures** in CI; a dry-run / candidate build for a recipe produces a build status + artifacts + dataset quality report + gate verdict.
- The agent **STOPS awaiting approval** at the gate; both the **approval** and **rejection** paths are exercised.
- No endpoint needs to validate in M5 — a candidate build that reaches the gate is sufficient.
- Provenance / `source_refs` are carried through into any proposed registry entry.

**7. Tests / checks** (the load-bearing ones)
- The agent **CANNOT** proceed to train/register without an explicit **approval token** — *structural, not a default*: a test proves it **raises/blocks** when approval is absent.
- It never writes a registry entry missing **metrics / dataset card / model card / limitations**.
- It never proceeds on a **FAILED** gate verdict.
- Thresholds are read from `registry/data/quality_gates.yaml` and are **never mutated** by the agent.

**8. Must-not-change** · `endoscan_core` science; the quality gate; the frozen ER model/status/artifacts; M4's limitations guarantees.

**9. Checkpoint / review criteria before merge** · Zero science reimplemented (agent only sequences tested tools); the gate→approval boundary is **structural** (blocks without a token); approval **and** rejection paths tested; CI green on fixtures.

**10. Labels** · `M5` · `agent` · `orchestration` · `gate` · `human-in-the-loop`

**11. Dependencies** · Issues 1–5 (M2 tools + gate, M3 pipeline, M1 registry, M4 limitations).

---

## Issue 7 — M6: Prove the thesis — agent builds a SECOND endpoint candidate

> **Status — COMPLETE (PRs #36, #40); second endpoint = AR (Androgen Receptor).** Built through the
> gated agent over CoMPARA (`functional_modulation`, binding excluded, VCaP/LINCS context). It was
> **honestly demoted** `validated_mvp` → `experimental` under the evidence-based CI rule (thin
> positives; CI lower bounds miss the floors) and committed serve-ready from a committed binary
> (Option A). No bar was lowered. See the Status snapshot for AR's current reality.

**1. Title** · `M6 — Prove the thesis: agent builds a SECOND endpoint candidate`

**2. Goal** · Run the **M5 orchestrator** on a second candidate (e.g. **AR via CoMPARA**, or another feasible CERAPP-style target — **candidate TBD, decided at milestone start**). The point is **reproducing the BUILD WORKFLOW with minimal manual work**, NOT forcing a validated endpoint. If the data supports it → train/register **at the status the gate yields** (`experimental` or `validated_mvp`). If the data fails the gate → mark **`failed_qc`** with a clear dataset card + gate report. **BOTH outcomes are acceptable and successful** if honest and reproducible. State explicitly: **a documented `failed_qc` is a VALID, valuable outcome** — it proves the gate works on new data — and must not create pressure to lower any bar.

**3. Background / why this matters** · This is the actual demonstration of the differentiator — reproducible endpoint-building over a *new* target through the agent, not by hand. CERAPP/CoMPARA-style sources are already in the allow-list family; the one real data run uses the **staged adapter** (no live CI downloads), consistent with M3.

**4. Non-goals** · No threshold lowering to force a pass; no manual science outside the tools; no API/frontend changes; no retraining ER.

**5. Modules / files likely touched**
```
agent/recipes/<ENDPOINT>.yaml                 # the second-endpoint build recipe
models/<ENDPOINT>/                            # registered artifacts IF it clears the gate (DVC-tracked)
registry/models/endpoints.json                # second-endpoint entry (experimental | validated_mvp | failed_qc)
registry/data/dataset_cards/<ENDPOINT>.md     # dataset card (written either way)
tests/agent/                                  # second-endpoint build + outcome tests
# agent (M5) and endoscan_core used AS-IS
```

**6. Acceptance criteria**
- A second candidate is produced **primarily through the agentic pipeline** (not by hand).
- A **dataset quality report + gate verdict** exist; **human approval** is recorded.
- If registered, it appears in the endpoint library with **NO API/frontend code changes**.
- If rejected, the **`failed_qc`** path is documented (dataset card + gate report) and tested.

**7. Tests / checks** · The build runs through the agent; the register path (valid entry; cards + limitations present) **or** the `failed_qc` path (status `failed_qc`; dataset card + gate report written) is tested; ER artifacts/status unchanged; thresholds unchanged.

**8. Must-not-change** · ER (model/status/artifacts); the gate thresholds; the agent's M5 guarantees.

**9. Checkpoint / review criteria before merge** · The build ran through the agent (not by hand); the outcome (register **or** `failed_qc`) is honest, gated, and reproducible; provenance/cards present; no bar was lowered to manufacture a pass.

**10. Labels** · `M6` · `endpoint-2` · `agentic-build` · `thesis` · `data-quality`

**11. Dependencies** · Issue 6 (M5). M2 tools, M3 pipeline, M1 registry.

---

## Issue 8 — M7: Thin FastAPI service

> **Status — COMPLETE (PR #42).** Shipped as a **read-only** thin FastAPI over inference + registry:
> `GET /health`, `GET /endpoints`, `GET /endpoints/{id}`, `POST /predict`, `POST /explain` — serving
> BOTH endpoints (ER via TreeSHAP, AR via linear_coefficient), honesty-first responses (the full
> `LimitationsBlock` on every result), and a variant-forward-compatible detail contract. **Scope
> note:** the builder-admin routes (launch/status/approve/reject) in the original spec below were
> **not** built — M7 landed as a read-only serving surface only; agent-admin-over-HTTP remains
> possible future work.

**1. Title** · `M7 — Thin FastAPI service`

**2. Goal** · Serve inference/explainability for registered endpoints (`GET /endpoints`, `POST /predict`, `POST /explain`) **and** minimal **builder-admin** routes (launch build, check build status, view dataset quality report / gate verdict, approve/reject). **Thin**: NO science/business logic outside `endoscan_core` + the M5 agent.

**3. Background / why this matters** · The API must stay thin (PROJECT_RULES §2): it wraps the M4 inference package and the M5 agent. The approve/reject routes **expose** the gate; they do not bypass it. This is the first network surface over the two MVP halves.

**4. Non-goals** · No UI (M8); no auth/accounts; no hosting; no model logic; no second-endpoint work.

**5. Modules / files likely touched**
```
services/api/{app.py,routers/inference.py,routers/builder.py,models.py,errors.py}   # new
tests/api/                                                                            # route tests on fixtures
# endoscan_core.inference + agent imported AS-IS
```

**6. Acceptance criteria**
- `curl` → prediction + explanation + **limitations** works on **FIXTURE_ER**.
- Build **launch / status / approve / reject** works on fixtures.
- DVC/model-unavailable errors are clean **4xx/5xx** (M4's `ModelArtifactUnavailableError` mapped, not a crash).
- **Limitations on EVERY** predict/explain response. No retrain / endpoint change.

**7. Tests / checks** · Route tests on fixtures (predict/explain carry limitations; `/endpoints` lists the library); builder-admin launch/status/approve/reject; an approve test proving approval is **routed through the agent's gate**; an approve test proving the approve route **CANNOT trigger training on a build whose gate verdict is FAILED** — human approval is necessary but NOT sufficient; a passing gate remains required (PROJECT_RULES §3.3). The API exposes approve/reject but cannot override a failed gate. An error-mapping test (model-unavailable → clean 4xx/5xx, not a 500 traceback).

**8. Must-not-change** · `endoscan_core`; the frozen ER endpoint; M4 limitations guarantees; the agent's approval boundary (the API exposes approve/reject, it does **NOT** bypass the gate).

**9. Checkpoint / review criteria before merge** · Routes are thin (no science in the API layer); limitations on every response; approval routed through the gate and **CANNOT override a failed verdict** — a human can approve a PASSING build to proceed, never approve past a gate failure; CI green on fixtures.

**10. Labels** · `M7` · `api` · `fastapi` · `serving` · `thin`

**11. Dependencies** · Issues 5 (M4 inference), 6 (M5 agent), 1 (M1 registry).

---

## Issue 9 — M8: Frontend (demo UI)

> **Status — COMPLETE (PRs #43, #44).** Data-driven React + Vite + TypeScript + Tailwind SPA:
> endpoint library + detail (limitations front-and-center, CI shown alongside each point metric),
> and an Analyze "try-it" flow with **4 real, verified demo signatures** (JSON paste + picker;
> profiles reproduced against current main; no fabricated/random signatures) → predict/explain with
> honesty-first, no-overclaim copy and the correct attribution method label per endpoint. **Scope
> note:** the internal **builder dashboard** in the original spec below was **not** built — M8 landed
> as the screening/analysis UI over the read-only M7 API. Signature **upload** (CSV/Excel) and
> compound lookup are deferred (see Deferred section).

**1. Title** · `M8 — Frontend (demo UI)`

**2. Goal** · A user flow (paste/upload signature → prediction dashboard → explanation → limitations → report) **and** an internal **builder dashboard** (build status, dataset quality report, gate verdict, approve/reject). **Demo UI, not production.**

**3. Background / why this matters** · The demo surface that makes both MVP halves tangible. It consumes the M7 API only; the endpoint library is data-driven, so a registered M6 endpoint appears with no frontend change. Limitations are a first-class, non-hideable UI element (no-overclaiming, §5).

**4. Non-goals** · No auth; no hosting; no production hardening; no science; no model logic.

**5. Modules / files likely touched**
```
apps/web/   # Vite/React/Tailwind/TS: result view, limitations panel, builder dashboard,
            # API client, bundled example signature
apps/web/tests/   # component/e2e (demo) checks
```

**6. Acceptance criteria**
- Browser demo works for **ER**.
- A second endpoint from M6 (if registered) appears **AUTOMATICALLY** from the endpoint library (no frontend code change).
- The builder dashboard works on a fixture/demo build.
- **Limitations VISIBLE and not hideable**; the builder dashboard shows the gate verdict / `failed_qc` **honestly**; no regulatory/diagnostic claims; **scope-of-claim shown verbatim**.

**7. Tests / checks** · Component/e2e (demo): result view renders limitations + verbatim scope; builder dashboard renders the gate verdict including `failed_qc`; the endpoint library list is API-driven; a smoke/e2e run against a demo build.

**8. Must-not-change** · The M7 API contract; `endoscan_core`; the frozen ER endpoint.

**9. Checkpoint / review criteria before merge** · Limitations prominent + accurate; ER works end-to-end; the builder dashboard shows gate results honestly (incl. `failed_qc`); no science in the frontend.

**10. Labels** · `M8` · `frontend` · `demo-ui` · `react`

**11. Dependencies** · Issue 8 (M7). M6 (if a second endpoint exists).

---

## Issue 10 — M9: Report engine + docs + MVP packaging

> **Status — NOT yet implemented.** The honesty layer exists (M4's `LimitationsBlock` on every
> result; the M8 UI surfaces limitations + verbatim scope), but the shareable downloadable **report
> engine** and the one-command fresh-clone **demo packaging** are still open. This is the main
> remaining MVP item.

**1. Title** · `M9 — Report engine + docs + MVP packaging`

**2. Goal** · A shareable **report** (prediction, score, top genes, limitations, provenance, endpoint status, model/dataset card links); **README + architecture diagram + demo walkthrough + example data + one-command local run**; portfolio/demo-ready.

**3. Background / why this matters** · Packages the MVP for a fresh-clone demo of both halves (inference on ER + an agentic build), reusing existing reporting. Framing is honest and up front: experimental pre-screening, **not** regulatory-grade (§5).

**4. Non-goals** · No cloud hosting / CI-CD / production Docker (a simple local run is fine); no second-endpoint science; no new model logic.

**5. Modules / files likely touched**
```
packages/endoscan_core/endoscan_core/reporting/   # report generator (reuse docx/pdf skills)
README.md   docs/walkthrough.md   docs/architecture.*   examples/   scripts/run_demo.*
tests/reporting/                                  # report-completeness checks
```

**6. Acceptance criteria**
- A fresh clone runs the demo locally with **one documented command**.
- A user runs ER inference and sees/downloads a **report**.
- The builder demo can **launch / monitor / approve** a candidate build.
- Docs state clearly: **experimental pre-screening, NOT regulatory-grade**; cards + limitations linked and consistent.

**7. Tests / checks** · Report-generator test (all sections present: prediction, top genes, **limitations**, provenance, endpoint status, card links); a docs/CI check that the one-command demo path is exercised on fixtures; a link/consistency check across cards + limitations.

**8. Must-not-change** · Everything upstream (core, frozen ER, the M7 API contract, the agent's guarantees).

**9. Checkpoint / review criteria before merge** · Fresh-clone one-command demo works; the report is honest + complete; the framing is experimental and up front.

**10. Labels** · `M9` · `reporting` · `docs` · `packaging` · `mvp`

**11. Dependencies** · Issues 8 (M7), 9 (M8). M4 reporting/limitations.

---

## Deferred features (RECORDED, not started)

Captured so they are not lost. **None of these is in progress** — this is a backlog, not a plan of
record. Recording a feature here is not approval to build it; each remains a future milestone gated by
the usual rules.

**Endpoints & variants**
- **Variant schema formalization.** An *endpoint* is a biological question; a *variant* is a
  context-specific model (cell lines / dose / time). The M7 API already exposes a
  **variant-forward-compatible** response shape and the registry is deliberately **one row per
  endpoint**. Formalize a variant schema only **after ≥2 real variants exist** — not before.
- **VCaP + A549 mixed-context AR variant** — the platform-scalability comparison; the
  `extract_signatures` job is already parameterized for the cell-line set, so this is a data run + a
  second AR variant, not new machinery.
- **TR (thyroid receptor) — unresolved source-definition milestone.** There is **no allow-listed
  experimental TR source** today; blocked on a ToxCast/Tox21 assay-selection decision (which assays
  define the functional TR label). Must be resolved before a TR endpoint can be built through the gate.
- More endpoints beyond ER/AR/TR.

**Input & data-entry**
- **Molecule-name → signature lookup / compound search** — name → CID/InChIKey resolution,
  open-signature indexing, cell-line/dose/time selection, and explicit **"no open signature found"**
  handling. Future milestone (kept out of M8).
- **CSV/Excel signature upload** — needs format validation + UX decisions; follow-up (a minimal CSV
  paste-alternative may be proposed separately if near-zero-cost).
- **Compatibility checker** — does an uploaded signature match an endpoint's training context
  (cell lines / platform / normalization) before predicting?

**Explainability**
- **Linear-model attribution is DONE** — AR uses `linear_coefficient` (PR #42). *Additional*
  attribution methods would only be future work **if new model families appear** (the
  `AttributionMethod` protocol stays open for that). Also still deferred: pathway/GO enrichment over
  the attributed gene set; DEDuCT / similar-compound contextualization; nearest-neighbour
  similar-signature lookup.

**Serving, deployment & storage**
- **Production API CORS + hosting** for a public demo URL (M8 uses a Vite dev proxy; the API is
  read-only and CORS-free in dev).
- **A proper artifact registry / object-storage DVC remote** once models grow beyond the current
  ~≤3 MB *commit-the-binary-directly* (Option A) approach.
- Batch prediction + batch reporting.

**Autonomy & governance**
- **Data-scout / source-card / autonomous endpoint expansion** — remains **propose-only** per
  PROJECT_RULES (§3.1a); recorded as future, kept as-is (no open-web scraping; allow-list only).

**Quality**
- Regulatory-grade validation / larger-N datasets to legitimately reach `validated_mvp` (must be
  earned through the unchanged gate, never by lowering a bar).
