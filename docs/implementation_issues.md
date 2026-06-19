# EndoScan — Implementation Issues (M0 → M4)

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
