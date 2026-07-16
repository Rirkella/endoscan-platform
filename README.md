# EndoScan

**Transcriptomics-first platform for endocrine-toxicology pre-screening.**

EndoScan transforms chemical-induced gene-expression signatures into interpretable endocrine-risk
profiles across multiple receptor pathways. Instead of predicting toxicity from chemical structure, it
predicts from the **biological response** a compound induces — `transcriptomic signature → endocrine risk`.

> **Scope & disclaimer.** EndoScan is a **pre-screening and prioritization** tool. It is **not** a
> regulatory, clinical, or diagnostic instrument, and it does **not** provide regulatory-grade validation.
> Every report includes an explicit limitations section. Results help prioritize which compounds warrant
> further experimental testing — not to confirm safety or toxicity.

> The full, extended vision lives in [`docs/project_vision.md`](docs/project_vision.md). This README is the
> concise overview.

---

## Product vision

EndoScan is not a single-receptor model — it is a **platform for toxicological intelligence** with two
complementary layers: a user-facing **screening platform** that turns a transcriptomic signature into an
interpretable endocrine-risk report, and an internal **agentic model factory** that expands the platform to
new endpoints by semi-automating dataset discovery, construction, validation, and documentation. The
long-term goal is a scalable toxicogenomics infrastructure where each new endpoint is added faster, more
reproducibly, and with transparent model cards.

## Target users

EndoScan is a B2B / B2B-research tool, not a consumer product. Primary users: **cosmetics R&D** teams
screening ingredient risk early; **pharma/biotech safety** teams prioritizing compounds; **CROs** running a
cheap pre-screen before expensive assays; **academic toxicology labs** interpreting perturbation data; and
**regulatory / NAMs research groups** exploring non-animal toxicology workflows.

## What it does — and does not

**Does:** predict endocrine-disruption risk for receptor endpoints (starting ER and TR) from a
transcriptomic signature; **explain** each prediction biologically (top SHAP genes, pathway interpretation,
similar signatures); and **expand** to new endpoints via the agentic factory.

**Does not:** act as a QSAR / direct **SMILES-to-toxicity** predictor (a SMILES-to-transcriptomics step may
be added later, *only* as experimental upstream feeding the same transcriptomic models); replace
experimental validation; or behave as a black box.

## Input modes

1. **Transcriptomics upload** (core MVP) — the user uploads a gene-expression / differential-expression /
   L1000-style signature.
2. **Compound lookup** — enter a compound (name / CID / InChIKey / SMILES); the platform checks for an
   existing public perturbation signature.
3. **SMILES experimental mode** (later, clearly marked low-confidence) — predicts a proxy signature when no
   experimental transcriptomics exist, then runs the *same* transcriptomic risk models.

## User-facing workflow

`transcriptomic input → preprocessing → endpoint-specific models → risk profile → SHAP/gene explanation →
pathway interpretation → similar signatures → report`. The user uploads a signature (or selects a compound),
the engine runs registered endpoint models, the explainability layer adds biological interpretation, and a
structured report is generated for download.

## Risk dashboard & report outputs

The **dashboard** shows a global endocrine-risk profile plus per-endpoint cards (risk score, confidence,
model status, top genes/pathways, applicability warning). The downloadable **report** expands this into:
executive summary, input info, global + endpoint-level risk, gene-level explanation, pathway interpretation,
similar signatures and compounds, confidence/applicability, suggested follow-up assays, and a mandatory
**limitations** section.

## Agentic model factory

New endpoints are added through an **agent-assisted dataset-construction toolchain** that the Builder Agent
orchestrates: `select trusted sources → retrieve labels → retrieve LINCS/L1000 signatures → map compounds →
compute overlap → build candidate table → dataset quality report → [QUALITY GATE + optional human approval]
→ train → model card → register`. Discovery and assembly are automated; **training and registration are
gated** by quality thresholds and optional human approval. Sources come **only** from an allow-list — no
open-web scraping. ER and TR are built through this toolchain, not by hand.

## Technical architecture

The central principle: **one framework-free science library (`packages/endoscan_core`) that both the API and
the agent import.** The registry is the contract between training and serving.

```
endoscan/
├── packages/endoscan_core/   # ALL science logic, framework-free, tested
├── services/api/             # FastAPI — thin; calls endoscan_core
├── apps/web/                 # React / Vite / Tailwind / TypeScript frontend
├── agent/                    # Builder Agent — orchestrates tested core tools
├── pipelines/endpoints/      # config-driven endpoint runners (not notebooks)
├── registry/                 # data sources + model/dataset metadata (the contract)
├── models/                   # trained artifacts (DVC-tracked, not raw git)
└── docs/ notebooks/ reports/ legacy/
```

The FastAPI service contains no science; the Builder Agent reimplements no science — both call
`endoscan_core`. See [`PROJECT_RULES.md`](PROJECT_RULES.md) for the binding architectural rules.

### PubMed evidence configuration

The optional supporting-literature layer uses the official NCBI PubMed E-utilities API. Set
`NCBI_EMAIL` to a monitored contact address before starting the API; requests remain disabled with an
honest `unavailable` state when it is absent. `NCBI_TOOL` defaults to `endoscan`. `NCBI_API_KEY` is
optional and raises the enforced client limit from 3 to 10 requests per second. Results are cached and
concurrent identical lookups are deduplicated in the API process.
`GET /health` exposes a `pubmed` capability block so a deployment can distinguish an unconfigured
integration from a configured service before a user starts an analysis. The shared preview must be
started with a monitored `NCBI_EMAIL`; the checked-in `.env.example` documents the deployment input
without committing a personal contact address.

## Model / data registry

`registry/` holds **text metadata, versioned in git**: `registry/data/` (the `sources.yaml` allow-list,
quality-gate thresholds, dataset cards) and `registry/models/` (`endpoints.json` index + model cards).
Binary model artifacts live separately in `models/` (DVC-tracked); the registry *points to* them. The
registry is the single contract between dataset construction, training, and serving.

## Roadmap / MVP scope

**MVP (must-have):** working web app; transcriptomics upload; risk dashboard; ER + TR endpoints built
through the toolchain; the dataset-construction toolchain + gate; model/data registry; report generation;
docs. **Next:** Builder Agent orchestrator and a second endpoint built by it; compound lookup; richer
pathway interpretation; AhR/PPAR (experimental). **Roadmap, not MVP:** a validated SMILES-to-transcriptomics
model; regulatory-grade validation; large-scale commercial API; auth/billing. Work proceeds **issue by
issue** (M0 → M1 → …); see [`docs/implementation_issues.md`](docs/implementation_issues.md).

## Why this fits Shaker and ITMO

**Genopole Shaker:** an early-stage toxicogenomics project needing toxicology expertise, validation-experiment
design, and industrial contacts to move from a working prototype toward a startup.
**ITMO / AI Talent Hub:** a deployed applied-AI system combining an agentic pipeline, AutoML, explainable AI,
and full-stack engineering — demonstrating AI system design and product thinking, not only biology.

## Key risks & how we handle them

Dataset-mapping and label-quality risk → compound-identifier harmonization with recorded mapping/label
confidence. Leakage → **compound-level splits** and reported validation strategy. Transcriptomic variability
→ retained metadata and comparable cell-line/timepoint restriction where possible. SMILES uncertainty → kept
experimental and upstream only. Overclaiming → a mandatory limitations/disclaimer in every report and **no
regulatory-grade claims**.

## Status

Early, clean rebuild. A prior hackathon (D4GEN 2026, Genopole) proved feasibility — including the
LINCS/transcriptomics ↔ toxicology-label linkage for ER and TR — and is kept as **scientific reference only**
(`legacy/`), not migrated structurally.

## Getting started

Requires Python ≥ 3.11 and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync                 # install workspace + endoscan_core (editable)
ruff check . && ruff format --check .
pytest
```

(Full scaffolding is established in Issue M0.)

## How this repository is built

One issue at a time, each as a single PR, reviewed and merged before the next begins.

- [`PROJECT_RULES.md`](PROJECT_RULES.md) — non-negotiable scientific & architectural constitution. Read first.
- [`CLAUDE.md`](CLAUDE.md) — operating instructions and execution protocol for the AI implementation agent.
- [`docs/project_vision.md`](docs/project_vision.md) — the full extended product vision.
- [`docs/implementation_issues.md`](docs/implementation_issues.md) — the ordered implementation issues.

## License

See [`LICENSE`](LICENSE).
