# CLAUDE.md — Instructions for Claude Code

You are the implementation agent for **EndoScan**, a clean, product-grade rebuild of a validated
research platform. You are starting with **no prior conversation context**. Read this file fully,
then `PROJECT_RULES.md`, then `README.md`, then the current issue — **before writing any code**.

---

## What EndoScan is (one paragraph)

EndoScan is a **transcriptomics-first** platform for endocrine-toxicology pre-screening. Its core logic
is **transcriptomic signature → endocrine risk**: models are trained on gene-expression signatures, not
on chemical structure. New endpoints (receptors) are added by an **agent-assisted dataset-construction
toolchain** that discovers data from a trusted allow-list, builds a candidate training set, runs quality
gates, and — only after the gate passes and a human approves — trains and registers a model. This is a
clean reimplementation; a prior hackathon proved feasibility but is **reference only**.

---

## Hard rules (condensed — full canonical list is in `PROJECT_RULES.md`, which governs)

- **Transcriptomics-first.** Core logic is `transcriptomic signature → endocrine risk`. Never implement direct SMILES-to-toxicity. SMILES-to-transcriptomics is experimental/upstream only, and only later.
- **Science lives in `packages/endoscan_core`** (framework-free, tested). **FastAPI is thin.** **The agent only orchestrates tested `endoscan_core` tools.**
- **Registry is the contract** between dataset construction, training, and serving.
- **Source discovery uses only `registry/data/sources.yaml`. No open-web scraping.**
- **Training and registration are gated** by quality thresholds + optional human approval. **Never bypass or weaken the gate.**
- **Compound-level splits only** (no leakage). `input_type` is always `transcriptomics`.
- **Reports always include limitations.** Every endpoint gets a model card; every dataset a dataset card.
- **Never claim regulatory-grade validation.** No Streamlit. No notebook-only logic.
- **Never fabricate data or hardcode/loosen tests or gates to make them pass.** Fail honestly; ask when blocked.

---

## Execution protocol (follow exactly)

1. **One issue at a time, in order:** M0 → M1 → M2 → M3 → M4 → …  The issues are in `docs/implementation_issues.md`.
2. **One issue = one branch = one PR.** Do **not** begin the next issue.
3. After completing an issue, **stop and wait for human review/merge.** Do not auto-advance.
4. Obey each issue's **"What should NOT be done"** section as strictly as its scope.
5. If anything is **ambiguous, blocked, or under-specified**, stop, write your questions in the PR description, and wait. Do not guess on scientific or architectural choices.
6. Keep PRs focused on the single issue. No drive-by refactors of unrelated code.

## Definition of done (per issue)

- All acceptance criteria in the issue are met.
- All tests/checks listed in the issue pass locally.
- `ruff check` + `ruff format --check` + `pytest` pass; CI is green.
- A PR is opened describing what was done, decisions made, and any open questions. Then **stop**.

---

## Data handling for M0–M4 (important)

- **No large/live downloads.** Do not fetch real ToxCast/Tox21/CERAPP/CoMPARA/LINCS datasets in these milestones.
- Use **small bundled fixtures** under `tests/fixtures/`. Real source-download adapters are **stubbed/TODO** and wired up in a later, dedicated issue.
- When you create fixtures, **define and document their schema explicitly** (column names, ID types such as CID/InChIKey/SMILES/perturbagen, label encoding) and **flag the schema in the PR for human review** — do not silently invent shapes.
- "Build ER/TR through the toolchain" means **run the toolchain code path on fixtures**, human-stepped. The LLM orchestrator that automates this is a later milestone (M5); ER (M3) does not require it.

---

## Repository architecture (target)

```
endoscan/
├── pyproject.toml                # uv workspace; deps single source of truth
├── packages/endoscan_core/       # ALL science logic, framework-free, tested
│   └── endoscan_core/{ingest,transcriptomics,features,training,inference,
│                       explain,reporting,registry,datasets}/
├── services/api/                 # FastAPI — thin; imports endoscan_core
├── apps/web/                     # React/Vite/Tailwind/TS frontend
├── agent/                        # builder_agent.py + tools/ (thin wrappers) + prompts/
├── pipelines/endpoints/{ER,TR}/  # config-driven runners (NOT notebooks)
├── registry/                     # TEXT metadata, git-versioned
│   ├── data/{sources.yaml,quality_gates.yaml,dataset_cards/}
│   └── models/{endpoints.json,model_cards/}
├── models/                       # binary artifacts — DVC-tracked, not raw git
├── docs/  notebooks/  reports/   # docs; exploration-only notebooks; example outputs
└── legacy/                       # README pointing to hackathon repo (reference only)
```

## Coding conventions

- Python ≥ 3.11, type hints, small single-purpose functions, docstrings on public functions.
- `uv` for env/deps; `ruff` for lint+format; `pytest` for tests; tests live under `tests/` mirroring the package.
- Pydantic for schemas/contracts. Pure functions where possible — no hidden global state.
- No science logic outside `packages/endoscan_core`. No `sys.path` hacks — rely on the installed package.

## Current status

Starting fresh. **The first task is Issue 1 (M0): repository scaffolding, tooling, and CI.**
Do not implement M1+ yet.
