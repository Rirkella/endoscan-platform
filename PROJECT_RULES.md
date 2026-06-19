# EndoScan — Project Rules (Constitution)

These rules are **non-negotiable** and stable. Every contributor, human or AI, must follow them.
CLAUDE.md and all issues defer to this file. If anything elsewhere conflicts with this file, **this file wins**.
Do not change these rules without an explicit human decision recorded in a PR.

## 1. Scientific identity

1.1 EndoScan is **transcriptomics-first**.
1.2 The core prediction logic is, and must remain: **transcriptomic signature → endocrine risk**.
1.3 Base models are trained on **transcriptomic features**, never on chemical fingerprints/descriptors as the primary input.
1.4 EndoScan is **not** QSAR-first and is **not** a direct **SMILES-to-toxicity** predictor. Do not implement that, in any form, under any framing.
1.5 A SMILES-to-transcriptomics module may exist **later, only as an experimental upstream step** that produces a (predicted) signature feeding the *same* transcriptomic risk models. It never connects SMILES directly to risk.

## 2. Architecture

2.1 All scientific logic lives in `packages/endoscan_core`. It is framework-free and independently testable.
2.2 The FastAPI service stays **thin**: routing, schemas, file/job handling only. It imports `endoscan_core`; it contains no science.
2.3 The Builder Agent **orchestrates tested tools from `endoscan_core`**. It reimplements no science.
2.4 The **model/dataset registry is the contract** between dataset construction, training, and serving. Training and serving both go through it.

## 3. Data discovery and the human-in-the-loop gate

3.1 Dataset construction ingests data ONLY from sources approved in `registry/data/sources.yaml`; the construction tools never pull from the open web. `registry/data/sources.yaml` is the approved, curated allow-list of usable sources — intentionally NOT the universe of all possible sources.

3.1a (Future, propose-only) A separate Source Discovery mode may search for and evaluate candidate public sources (relevance, license, provenance, identifier coverage, endpoint coverage, likely overlap) and emit source proposals for human review. It MUST NOT ingest data into dataset construction, MUST NOT be used for training, and MUST NOT write to `registry/data/sources.yaml`. A source enters the allow-list only by explicit human approval of a proposal. This mode is NOT implemented yet.
3.2 Dataset *discovery and assembly* may run automatically. **Training and registration are gated**: they proceed only when automated quality thresholds pass **and** (optionally) a human approves.
3.3 The gate is a hard boundary. Never bypass it, weaken its thresholds, or trigger training before a `pass` verdict.
3.4 ER and TR are built **through the dataset-construction toolchain** (the same code path the agent uses), never by ad-hoc manual assembly.

## 4. Scientific correctness

4.1 Train/test splits are **compound-level** (group by compound) to prevent leakage. Row-level/random splits are forbidden for endpoint models.
4.2 Every label carries its **assay source and confidence**. Conflicting labels must be resolved and recorded.
4.3 `input_type` for every registered endpoint is `transcriptomics` (enforced at the schema level).
4.4 Predictions carry a **confidence** and an **applicability-domain** flag.

## 5. Claims, documentation, safety

5.1 **Never** claim regulatory-grade validation. EndoScan is a pre-screening / prioritization tool, not a regulatory, clinical, or diagnostic instrument.
5.2 Every report **must include a limitations + disclaimer section**.
5.3 Every registered endpoint must eventually have a **model card**; every constructed dataset a **dataset card**.

## 6. Integrity

6.1 **Never fabricate data, hardcode results, or weaken/skip assertions to make a test or quality gate pass.** If data is insufficient or a result is genuinely negative, fail honestly and report it.
6.2 If a task is ambiguous or blocked, stop and ask the human in the PR. Do not guess on scientific or architectural decisions.

## 7. Tooling baseline

7.1 Python packaging/deps via `uv`; lint+format via `ruff`; tests via `pytest`; binary model artifacts via `DVC` (not raw git).
7.2 Backend is **FastAPI**. Frontend is **React/Vite/Tailwind/TypeScript**.
7.3 **No Streamlit. No notebook-only deliverables** — notebooks are for exploration/demos only and are never imported by application code.

## 8. Legacy

8.1 The hackathon repository is a **scientific reference and proof-of-concept only**. Do **not** migrate its structure, and do not attempt to access it during implementation. If you believe you need something from it, ask the human.
