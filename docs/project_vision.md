# EndoScan — Project Vision (Extended)

This document is the full vision reference. For binding rules see [`../PROJECT_RULES.md`](../PROJECT_RULES.md);
for the concise overview see [`../README.md`](../README.md); for the work plan see
[`implementation_issues.md`](implementation_issues.md).

> All architecture in this document uses the **final package-based architecture**
> (`packages/endoscan_core`, thin `services/api`, `apps/web`, `agent/`, `registry/`, `models/`). Any earlier
> flat `backend/ ml/ transcriptomics/` layout from initial brainstorming is **superseded** and must not be used.

---

## 1. Overview

EndoScan is a transcriptomics-first AI platform for preliminary toxicological risk screening, focused first
on endocrine disruption. Its premise: chemical toxicity should be evaluated not only from a molecule's
structure but from the **biological response** the molecule induces. Rather than predicting endocrine risk
directly from SMILES (as classical QSAR would), EndoScan uses **transcriptomic signatures as the central
predictive layer**.

In the first version, a user uploads transcriptomic data or analyzes a compound for which a public
perturbation signature exists. EndoScan predicts endocrine-disruption risk across several endpoints —
beginning with estrogen receptor (ER) and thyroid-related (TR) mechanisms, then androgen receptor (AR) and
beyond — and returns an interpretable report with endpoint-level risk scores, confidence, gene-level
explanations, pathway interpretation, similar signatures, and suggested follow-up.

The platform's key innovation is an **agentic model-building pipeline**: instead of hand-building one model
per receptor, a Builder Agent orchestrates a tested toolchain that discovers data from a curated allow-list,
connects transcriptomic signatures with toxicology labels, harmonizes compound identifiers, builds a
candidate dataset, runs quality gates, and — after the gate and optional human approval — trains, validates,
documents, and registers a new endpoint module.

A future SMILES-to-transcriptomics module will let the platform infer a likely signature from structure when
experimental data are unavailable. Even then, the **core risk layer remains transcriptomics-based**.

## 2. Scientific rationale

Classical computational toxicology follows `SMILES / descriptors → toxicity endpoint`. Useful, but
structurally driven and hard to interpret biologically. EndoScan follows
`chemical exposure → transcriptomic response → endocrine risk`. The biological assumption: endocrine
disruptors induce characteristic gene-expression changes carrying mechanistic information about receptor
activation, pathway perturbation, stress, compensatory responses, and downstream effects. EndoScan therefore
trains endpoint-specific models on **transcriptomic features**, not molecular fingerprints.

Endocrine disruption is the first focus because it is biologically meaningful, industrially relevant, and
naturally multi-endpoint. The endocrine risk panel: ER, AR, thyroid (TR), AhR, PPAR activity, with future
extension toward GR, PR and additional nuclear-receptor pathways.

## 3. Product positioning

EndoScan is a **transcriptomics-first agentic toxicology platform for rapid pre-screening of endocrine
disruption**. It predicts risk from chemical-induced gene-expression signatures and provides mechanistic
interpretation via explainable AI and pathway analysis. Its agentic factory continuously expands the platform
to new endpoints by building validated transcriptomics-to-risk modules from curated public datasets.

**It IS:** a preliminary screening / prioritization tool; a decision-support platform; a toxicogenomics
interpretation engine; a way to reduce how many compounds need expensive downstream testing; a reproducible
AI infrastructure for endpoint-specific toxicology models.

**It is NOT:** a regulatory replacement; a clinical/diagnostic tool; a fully validated commercial platform at
MVP stage; a simple QSAR predictor; a black box; a system that guarantees toxicity or safety.

## 4. Target users

EndoScan is B2B / B2B-research. Primary profiles: **cosmetics R&D** (early ingredient/formulation risk);
**pharma & biotech safety** (early liability identification in prioritization); **CROs** (pre-screen before
expensive assays); **academic toxicology labs** (interpret perturbation data); **regulatory / NAMs research
groups** (non-animal toxicology workflows); and **product startups** (chemical, cosmetic, food, agri, biotech)
needing early prioritization without internal toxicology infrastructure.

## 5. User-facing platform

A clean scientific SaaS-style web app. The user immediately understands three actions: upload a transcriptomic
signature, predict endocrine-disruption risk, and generate an interpretable report. Main surfaces: landing
page, transcriptomics screening, compound lookup, (experimental) SMILES mode, risk dashboard, full report,
endpoint library, internal endpoint-builder dashboard, and documentation. The landing page avoids overclaiming
and frames EndoScan as early-stage pre-screening, not final regulatory decision-making.

## 6. Input modes

1. **Transcriptomics upload (core MVP).** CSV of gene expression — e.g. `gene, log2FC, padj`; a
   compound/cell-line/dose/time matrix; or a precomputed L1000-style signature vector. This is the native
   input of the core models.
2. **Compound lookup.** Enter name / PubChem CID / InChIKey / SMILES; the platform checks for an existing
   public perturbation signature. If found → run the risk models; if not → offer experimental SMILES mode.
3. **SMILES experimental mode (later).** Predict a proxy signature from structure, then run the **same**
   transcriptomic risk models. Clearly marked lower-confidence. SMILES is only an upstream producer of a
   signature; it never connects directly to risk.

## 7. Risk dashboard

Shows the global endocrine-risk profile and endpoint-level scores (e.g. ER High, AR Medium, TR Low, AhR
experimental). Each endpoint card carries: risk score, confidence, model status (`validated_mvp`,
`experimental`, `low-confidence`, `insufficient input data`), input type, top contributing genes/pathways, an
applicability warning, and a link to the detailed explanation.

## 8. Full report

The report is one of the strongest parts of the product, written for a scientist/toxicologist. Sections:
executive summary; input information; global endocrine-risk profile; endpoint-level risk table; gene-level
explanation; pathway-level interpretation; similar transcriptomic signatures; similar known compounds;
external toxicological evidence (as supporting evidence, not final truth); confidence and applicability
domain; suggested follow-up experiments; and a mandatory **limitations and disclaimer** section. Output as
HTML, downloadable PDF, or JSON.

## 9. Technical architecture

Five conceptual layers — frontend, thin backend API, transcriptomics prediction engine, agentic model factory,
and the model/data registry — realized in the package-based layout:

```
endoscan/
├── packages/endoscan_core/        # ALL science logic (framework-free, tested)
│   └── endoscan_core/
│       ├── ingest/                # loaders, label parsing, compound harmonization
│       ├── transcriptomics/       # preprocessing, gene mapping, signature scoring,
│       │                          #   pathway enrichment, smiles_to_signature (experimental)
│       ├── features/              # landmark/signature/pathway features, embeddings
│       ├── training/              # train + evaluate endpoint (compound-level splits)
│       ├── inference/             # predict from registry
│       ├── explain/               # SHAP, pathway interpretation, similar signatures
│       ├── reporting/             # structured report assembly (incl. limitations)
│       ├── registry/              # read/write API — the contract
│       └── datasets/              # the dataset-construction toolchain (agent's tools)
├── services/api/                  # FastAPI — thin; routing/schemas/jobs; imports core
├── apps/web/                      # React / Vite / Tailwind / TypeScript
├── agent/                         # Builder Agent orchestrator + thin tool wrappers + prompts
├── pipelines/endpoints/{ER,TR}/   # config-driven runners (not notebooks)
├── registry/                      # TEXT metadata, git-versioned
│   ├── data/                      #   sources.yaml (allow-list), quality_gates.yaml, dataset_cards/
│   └── models/                    #   endpoints.json, model_cards/
├── models/                        # binary artifacts — DVC-tracked
└── docs/ notebooks/ reports/ legacy/
```

Invariants: science lives only in `endoscan_core`; the API is thin and imports core; the agent orchestrates
tested core tools and reimplements no science; the registry is the single contract between construction,
training, and serving.

## 10. Prediction engine

Pipeline: `raw transcriptomic file → format validation → gene-ID mapping → normalization → feature alignment
→ missing-gene handling → endpoint model inference → risk aggregation → report`. Supported feature types: raw
expression, differential expression, L1000 landmark features, pathway/gene-set scores, transcriptomic
embeddings. Each endpoint model maps `transcriptomic signature → probability of endpoint-associated endocrine
risk` (ER, AR, TR, AhR, PPAR …). **All base models are trained on transcriptomic features, never on SMILES
descriptors.**

## 11. SMILES-to-transcriptomics layer (future)

Role: `SMILES → predicted transcriptomic signature`, then the **same** endpoint risk models. Candidate
approaches include descriptor/fingerprint → L1000-style regression, chemical/transcriptomic embedding
prediction, pretrained molecular-model embeddings → signature, similarity retrieval from known perturbations,
or hybrid nearest-neighbor + regression. For the MVP this is experimental: use real transcriptomics when
available, otherwise a predicted proxy marked lower-confidence. **Transcriptomics remains the central
representation.**

## 12. Explainability & pathway interpretation

Explanations are **biological** — genes, gene sets, pathways, receptor-related response patterns — not only
chemical. Outputs: top positive/negative genes, pathway enrichment, SHAP plot, endpoint-specific
interpretation, and a confidence explanation. Pathway interpretation translates gene-level patterns into
mechanism (nuclear-receptor signaling, stress responses, cell-cycle/inflammatory signatures), using sources
such as GO, KEGG, Reactome, MSigDB, or custom endocrine gene sets. A lightweight scoring module suffices for
the MVP; full enrichment comes later. This biological interpretability is EndoScan's main advantage over
classical QSAR.

## 13. Similarity & evidence engine

Compares an input signature against reference signatures to surface similar perturbations, known compounds
with similar responses, their endpoint labels, and similarity scores. If compound identity is known, it can
also show structurally similar compounds — but the **primary** similarity logic is transcriptomic, not
structural. Curated external toxicology annotations may appear as **supporting evidence**, never as final
truth.

## 14. Agentic model factory

The internal system that lets EndoScan scale. The MVP uses **one orchestrator agent with specialized tools** —
not many agents for show. The Builder Agent receives a task such as *"build a transcriptomics-based model for
androgen-receptor disruption,"* then sequences the dataset-construction toolchain and the
training/registration steps, **stopping at the quality gate** for automated thresholds plus optional human
approval.

The toolchain (each a tested function in `endoscan_core/datasets/` and `endoscan_core` training/explain
modules; the agent's `tools/` are thin wrappers over them):

1. **Endpoint planner** — define the endpoint (target mechanism, receptor, priority, candidate sources).
2. **Transcriptomic signature retriever** — find/retrieve compound-induced LINCS/L1000 signatures + metadata.
3. **Toxicology label retriever** — endpoint labels with assay source and label confidence.
4. **Compound mapper** — harmonize name / CID / InChIKey / SMILES / CompTox / LINCS perturbagen IDs.
5. **Dataset harmonizer** — dedupe, resolve conflicting labels, filter low quality, align genes, build splits,
   prevent compound and (where possible) cell-line leakage.
6. **Transcriptomic feature builder** — ML-ready features (landmark expression, differential expression,
   pathway/gene-set scores, embeddings, similarity features).
7. **AutoML trainer** — train candidates (logistic/elastic-net, RF, gradient boosting, etc.) on **transcriptomic
   features**; produce a leaderboard and best model. (MVP starts with a small set, not the full zoo.)
8. **Validator** — AUROC, AUPRC, balanced accuracy, F1, calibration, confusion matrix, CV; checks for class
   imbalance, leakage, duplicate signatures, applicability domain → status `validated_mvp` / `experimental` /
   `failed_quality_control`.
9. **Explainability generator** — SHAP, top genes/gene-sets, pathway interpretation, endpoint summary.
10. **Model-card generator** — transparent documentation (definition, sources, counts, features, model type,
    metrics, validation, limitations, recommended/not-recommended use).
11. **Registry updater** — register a validated model so the endpoint appears in the library and is served.

**Source discovery is restricted to the allow-list in `registry/data/sources.yaml`** (ToxCast, Tox21, CERAPP,
CoMPARA, LINCS …) — no uncontrolled internet scraping — which keeps the workflow reproducible. Training and
registration never proceed before the gate passes.

## 15. Model & data registry

`registry/models/` holds `endpoints.json` (the index) and `model_cards/`. Each endpoint's artifacts live in
`models/{ENDPOINT}/` (model, metrics, feature schema, model card, training report, SHAP explainer,
calibration, config) and are DVC-tracked. Endpoint status is one of `candidate`, `dataset_ready`,
`under_review`, `validated_mvp`, `experimental`, `failed_qc`, `deprecated`. `registry/data/` holds the
`sources.yaml` allow-list, `quality_gates.yaml` thresholds, endpoint definitions, compound-mapping config, and
dataset cards. The registry is the **single contract** between dataset construction, training, and serving.

## 16. MVP scope

**Must-have:** working web app; transcriptomics upload; risk dashboard; ER + TR endpoint modules built through
the toolchain; the dataset-construction toolchain + quality gate; the model/data registry; report generation;
README and docs; a demo. **Strong but optional:** AhR/PPAR experimental modules; compound lookup; similar
signatures; basic pathway interpretation; SHAP plots. **Roadmap, not MVP:** a validated
SMILES-to-transcriptomics model; regulatory-grade validation; large-scale commercial API; full toxicological
coverage; production security/billing.

## 16a. Roadmap note — Source Discovery Agent (propose-only, post-M6)

A future milestone (after M6) may add a **Source Discovery Agent** that operates
in a strictly **propose-only** mode: it searches for and evaluates candidate
public data sources — capturing relevance, license, provenance, identifier
coverage, endpoint coverage, and likely overlap — and emits **source proposals**
for human review. A human approves a proposal before any source enters
`registry/data/sources.yaml`; discovery never ingests data into dataset
construction, is never used for training, and never writes to the allow-list, so
construction stays fully reproducible (PROJECT_RULES.md §3.1a). The proposal store
`registry/data/source_proposals/` and a `SourceProposal` schema will be added by
that future milestone — **not now**.

## 17. Business model

Value is **risk prioritization and biological interpretation**, not raw per-molecule compute. Potential models:
pay-per-report; batch screening; SaaS subscription; API access; CRO partnership; enterprise custom-endpoint
development. Early pricing stays flexible — before Shaker, the goal is validation with real users, not revenue.

## 18. Why this fits Shaker and ITMO

**Genopole Shaker.** EndoScan is early-stage, biotech-related, AI/software/data-driven, grounded in
computational biology and toxicogenomics, and connected to healthcare, cosmetics, and bioeconomy use cases. It
needs toxicology expertise, mentors, help designing validation experiments, industrial contacts, business-model
refinement, and support moving from prototype to startup.

**ITMO / AI Talent Hub.** EndoScan is an applied-AI engineering project combining AI agents, AutoML,
transcriptomics, toxicology prediction, explainable AI, pathway interpretation, full-stack web development, and
reproducible ML engineering — a deployed system that uses an agentic pipeline to build transcriptomics-based
toxicology models and expose them through an interpretable platform. It demonstrates AI system design and
product thinking, not only biology.

## 19. Key risks and how we handle them

- **Dataset-mapping risk** (linking signatures and labels for the same compound) → compound-identifier
  harmonization with documented mapping confidence.
- **Label-quality risk** (heterogeneous assay definitions) → store assay source and confidence per label;
  resolve and record conflicts.
- **Leakage risk** → compound-level splits and a reported validation strategy.
- **Transcriptomic variability** (cell line / dose / time) → retain metadata and restrict to comparable
  conditions where possible.
- **SMILES-to-transcriptomics uncertainty** → keep it experimental and upstream; transcriptomics stays the
  core input.
- **Overclaiming risk** → a mandatory limitations/disclaimer in every report; never claim regulatory-grade
  validation.

## 20. One-line summary

EndoScan transforms chemical-induced transcriptomic signatures into interpretable endocrine-risk reports, and
scales to new endpoints through a gated, agent-assisted model factory — while keeping transcriptomics, not
chemical structure, as its biological core.
