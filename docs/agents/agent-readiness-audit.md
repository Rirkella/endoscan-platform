# EndoScan agent-readiness audit

Status: implementation-ready design audit, 2026-07-17. This document describes the merged `main` tree at merge commit `7791534251c2a06a3135738160999be514a54105`. It does not claim that the proposed agent platform already exists.

## Executive finding

EndoScan already has a credible deterministic endpoint-building kernel and a registry-driven serving product. It does **not** yet have a real LLM agent system. The code under `agent/` is a useful, tested file-backed orchestrator: it sequences pure tools, records one quality gate, mints an approval token, and calls the training pipeline. It has no model provider, prompts, agent loop, retrieval, durable database, admin API, worker queue, pause/resume, source-discovery capability, or admin UI.

The first credible vertical slice should preserve the scientific kernel and add a database-backed workflow around it. LLMs may search, compare, extract and summarize evidence, but they must never calculate scientific metrics in prose, edit the allow-list, authorize training, or publish a registry entry. Those actions belong to typed deterministic tools plus explicit human approvals.

## Repository architecture

### Frontend

- `apps/web/src/main.tsx` declares the React Router routes: `/`, `/analyze`, `/analyze/:analysisId`, `/projects`, `/library`, `/library/:id`, and `/explore`.
- `apps/web/src/App.tsx` is the workspace shell. Navigation is static except that endpoint content is data-driven.
- `apps/web/src/pages/Analyze.tsx` implements upload/catalogue/reference preparation, validation, `/analyze`, result tabs and cached restore.
- `apps/web/src/pages/ModelLibrary.tsx` and `ModelEvidence.tsx` consume `/endpoints`; a newly registered endpoint appears without endpoint-specific frontend code.
- `apps/web/src/pages/Explore.tsx` consumes the committed reference maps and catalogue.
- `apps/web/src/pages/Projects.tsx` manages browser-local guest analyses.
- `apps/web/src/session/guestAnalysis.ts` uses IndexedDB with an in-memory fallback and session-scoped IDs. This is user-result persistence, not backend workflow persistence.
- `apps/web/src/api/client.ts` is the typed HTTP client. There are no admin methods.
- `apps/web/vite.config.ts` proxies `/api` to `127.0.0.1:8000`; production uses `VITE_API_BASE_URL`.

### Serving API

`services/api/endoscan_api/app.py:create_app` creates a read-only FastAPI app, warms registered models and mounts:

| Route | Module | Existing purpose |
|---|---|---|
| `GET /`, `GET /health` | `routes/health.py` | runtime/model/PubMed capabilities |
| `GET /endpoints`, `GET /endpoints/{id}` | `routes/endpoints.py` | registry-derived library/detail |
| `POST /predict`, `POST /explain` | `routes/inference.py` | core inference and explanation |
| `POST /signatures/parse` | `routes/signatures.py` | upload parsing and feature compatibility |
| `POST /analyze` | `routes/analyze.py` | fan-out over selected registered endpoints |
| `GET /catalogue/v1/*` | `routes/catalogue.py` | measured-signature catalogue |
| `GET/POST /explore/*` | `routes/explore.py` | committed UMAP/support-vector retrieval and placement |
| `POST /interpret/biological-response` | `routes/interpret.py` | deterministic ranked enrichment |
| `POST /interpret/pathways` | `routes/interpret.py` | deterministic Reactome over-representation |
| `POST /interpret/literature` | `routes/interpret.py` | bounded PubMed search with strict filtering |

There are no `/admin` routes, authentication, roles, DB session, workflow service, event stream or background-worker API. `services/api/pyproject.toml` calls the service read-only.

### Registry and serving contract

- `registry/models/endpoints.json` is the endpoint index. It currently registers ER and AR.
- `packages/endoscan_core/endoscan_core/registry/schema.py:EndpointEntry` enforces uppercase IDs and `input_type="transcriptomics"`.
- `registry/store.py:list_endpoints`, `get_endpoint`, `register_endpoint`, `register_or_update_endpoint`, `update_status`, and `load_model` implement the JSON store.
- Registration validates artifact presence. Re-registration preserves `created_at`, stamps `updated_at`, and refuses a frozen endpoint.
- `models/{ER,AR}/` holds `model.pkl`, `feature_schema.json`, `metrics.json`, model/dataset cards, model-selection reports, and `explore/` artifacts.
- `services/api/endoscan_api/routes/endpoints.py` and `apps/web/src/pages/ModelLibrary.tsx` read this contract dynamically.

The current write is not transactional: artifacts are written first and `endpoints.json` is rewritten in place. A production publisher needs immutable candidate artifacts, checksum verification, a DB approval, and an atomic publish step.

### Data sources, staging and provenance

- `registry/data/sources.yaml` is the approved allow-list. It contains ToxCast, Tox21, CERAPP, CoMPARA, LINCS and PubChem metadata/locators. Construction cannot consume an unapproved source.
- `packages/endoscan_core/endoscan_core/datasets/sources.py` validates the allow-list and enforces it at tool boundaries.
- `datasets/adapters/staged_adapter.py:StagedSourceAdapter` reads reviewed local CSV/Parquet extracts. `download_adapter.py:RealDownloadAdapter` is still a stub.
- ER staging lives in `pipelines/endpoints/ER/staging/`: fetch/validation, CERAPP parsing, InChI/CASRN identity, LINCS metadata selection, GCTX slicing, 10 uM/24 h condition selection, MCF7/A549 fusion, and review bundle generation.
- AR heavy jobs live in `packages/endoscan_jobs/endoscan_jobs/`: CoMPARA measured-SDF parsing, identity derivation, coverage diagnostics, LINCS extraction and exploration-map generation.
- `packages/endoscan_jobs/endoscan_jobs/storage.py` defines a traversal-safe `Storage` protocol and `LocalStorage` namespaces (`raw`, `curated`, `artifacts`, `logs`, `reports`). No object-storage implementation exists.
- `packages/endoscan_jobs/endoscan_jobs/runner.py` provides structured JSONL events and result records, but no queue, locking, heartbeat, cancellation or restart recovery.

### Reference, pathway and literature capabilities

- `jobs/build_explore_map.py` creates a seeded UMAP, exact original-space support vectors and domain-distance manifest from curated 978-gene signatures.
- `jobs/enrich_reference_identities.py` performs a reviewed, rate-limited PubChem enrichment and atomically writes a versioned identity artifact.
- `services/api/endoscan_api/explore_store.py` serves exact support rows; it never reconstructs a vector from 2-D coordinates.
- `data/reactome/reactome_pathways.json`, `reactome_store.py`, `pathways.py` and `biological_response.py` support deterministic enrichment.
- `data/literature/endpoint_concepts.v1.json`, `literature_concepts.py` and `literature.py:LiteratureService` support bounded PubMed retrieval and in-process caching. This is not a general RAG index and is not durable across restarts.

### CLI, jobs, tests and deployment

- `python -m endoscan_jobs list|run` is defined in `packages/endoscan_jobs/endoscan_jobs/cli.py`.
- Registered jobs are `coverage`, `extract_signatures`, and `build_explore_map` in `runner.py:_load_jobs`. Identity enrichment has its own module CLI.
- `pipelines/endpoints/ER/run.py` has a direct `--config` CLI.
- `Dockerfile` builds only the heavy-job runtime. `docker-compose.yml` remains a commented stub.
- `.github/workflows/ci.yml`, `web.yml`, `docker.yml` are active checks. `jobs.yml` is manual and inert until a self-hosted `endoscan` runner exists.
- The repository has about 70 Python test files and 15 frontend Vitest suites. The relevant boundary tests are under `tests/agent`, `tests/datasets`, `tests/staging`, `tests/training`, `tests/registry`, `tests/jobs`, `tests/inference`, `services/api/tests`, and `apps/web/src/test`.

## Exact ER/AR construction trace

### Common construction and training path

1. **Configuration** — `pipelines/endpoints/{ER,AR}/config*.yaml` is parsed by `run.py:load_config` into `PipelineConfig`. `recipe.yaml` points the current builder at the shared runner.
2. **Approved sources** — `datasets/sources.py:load_sources` reads `registry/data/sources.yaml`; `source_selector.py:source_selector` selects label/signature/mapping sources applicable to the target.
3. **Staged reads** — `run.py:make_adapter` creates `StagedSourceAdapter` (real) or `FixtureSourceAdapter` (tests).
4. **Labels** — `label_retriever.py:label_retriever` returns a typed `LabelSet` with assay source, confidence, raw call and recorded conflicts.
5. **Signatures** — `signature_retriever.py:signature_retriever` returns `SignatureSet`. The real path consumes one fused InChIKey-keyed row per compound with 978 gene features.
6. **Identity** — `compound_mapper.py:compound_mapper` maps CASRN/PERT_ID to full InChIKey via staged PubChem rows, records unmapped/ambiguous mappings, and never silently chooses a conflict.
7. **Overlap** — `overlap.py:overlap_computer` intersects canonical compounds with a label and signature.
8. **Feature matrix, labels and split plan** — `candidate_table.py:candidate_table_builder` builds `X`, `y`, metadata and a deterministic compound-group assignment. Conflicted labels are excluded.
9. **Quality and leakage** — `quality_report.py:dataset_quality_report` calculates overlap, class counts, duplicates, conflict rate, metadata coverage, compound leakage and split feasibility; it writes a dataset card. `quality_gates.py:quality_gates` compares this report with `registry/data/quality_gates.yaml`.
10. **Approval boundary** — `agent/builder_agent.py:start_build` stops at `awaiting_approval`; `approve` mints a token bound to the gate-report fingerprint; `promote` is the sole orchestrator chokepoint.
11. **Honest split/evaluation** — `run.py:run_pipeline` re-runs the same gate. It uses `training/nested_cv.py:nested_group_cv` by default; every inner and outer split is compound-group disjoint.
12. **Training/selection** — `training/model_selection.py:score_candidates` scores five sklearn candidates; `select_model` chooses the simplest candidate within tolerance; `training/train_endpoint.py:build_model` and `fit_balanced` refit on all data.
13. **Metrics/status** — `evaluate_endpoint.py:evaluate_endpoint` computes AUROC, AUPRC, balanced accuracy, F1, Brier and confusion matrix. `uncertainty.py` adds grouped-bootstrap CIs and evidence. `run.py:_status_for` applies the shared evidence-aware status rule.
14. **Artifacts** — `run_pipeline` writes `model.pkl`, feature schema, metrics, model-selection JSON/Markdown, dataset card and model card.
15. **Registry** — `registry/store.py:register_or_update_endpoint` writes the entry only after gate plus approval and artifact creation.
16. **API/product** — `app.py` warms the model; `/endpoints`, `/analyze`, `/predict` and `/explain` consume it. Model Library renders it automatically.

### ER-specific upstream path

`CERAPP measured functional rows -> staging/cerapp.py -> InChI-first identity with CASRN/PubChem fallback in staging/identity.py and staging/pubchem.py -> LINCS GSE92742 metadata -> staging/stage_er.py:assemble_sig_meta -> MCF7/A549 selection -> staging/condition.py:select_condition_signatures -> staging/gctx.py:slice_gctx_landmark -> early_fuse -> data/staged/er/{cerapp.csv,pubchem.csv,lincs.parquet}`.

ER uses `pipelines/endpoints/ER/config.real.yaml`, ten compound groups and strict shared gates. The committed model is a random forest, 959 usable compounds (73 positive), honest nested metrics in `models/ER/metrics.json`, and `experimental` status. ER is frozen in the registry.

### AR-specific upstream path

`CoMPARA measured SDFs (prediction files excluded) -> endoscan_jobs/compara.py:extract_sdf_records -> labels_for_mode(functional_modulation = agonist union antagonist; binding excluded) -> identity.py:inchikey_from/collapse_one_label_per_structure -> jobs/coverage.py coverage diagnostics -> jobs/extract_signatures.py -> fusion.py condition selection and VCaP-context fusion -> curated/AR/signatures.parquet -> staged as data/staged/ar/lincs.parquet plus compara.csv and pubchem.csv`.

AR then uses the same `pipelines/endpoints/ER/run.py`. The committed model is ridge logistic regression, 131 compounds (37 positive), honest nested metrics in `models/AR/metrics.json`, and `experimental` status.

## Step classification

| Stage | Class | Reason and boundary |
|---|---|---|
| Endpoint definition | C mandatory human | The claim, assay semantics and acceptable scope are scientific/product commitments. An agent may draft wording only. |
| Search strategy and candidate discovery | B agent-assisted | Iterative terminology and metadata interpretation benefit from an LLM; results remain proposals. |
| Metadata/download/checksum | A deterministic | Network clients, allowlists, hashes and schemas must compute facts. |
| Dataset relevance recommendation | B then C | Agent summarizes evidence; human selects the source and approves allow-list entry. |
| Sample/control interpretation | B then C | Agent flags ambiguity; scientist confirms inclusion/exclusion. |
| Label construction | A for an approved rule; C for the rule | Code applies a versioned rule. The LLM cannot label rows in prose. |
| Identity resolution/coverage/conflicts | A, with C on conflicts | RDKit/PubChem/table joins calculate; unresolved collisions require review. |
| Feature construction/fusion | A after C | Numeric transformation is deterministic; scientific context/fusion policy is approved first. |
| Duplicate/leakage/quality audits | A | These are reproducible calculations and testable invariants. |
| Training/calibration/evaluation | A after C authorization | Fixed code and configs compute outputs; a human authorizes costly/scientific scope. |
| Limitations/data-card/model-card draft | B over A artifacts | Agent may summarize only cited, structured artifacts; templates and validation constrain output. |
| Model acceptance and registration | C | A person accepts limitations and explicitly authorizes publish. |
| Registry write/product refresh | A after C | Atomic publisher verifies approval and artifact checksums. |
| Autonomous multi-agent debate, chat-first UI, vector search over metrics | D | Adds complexity or presentation theater without improving the first vertical slice. |

**Non-negotiable rule:** an LLM never returns a calculated coverage, identity rate, class count, leakage result, metric, calibration value or checksum as authoritative prose. It calls a typed tool; the UI cites the immutable artifact produced by that tool.

## Reusable deterministic tools

The complete machine-readable inventory is `docs/agents/tool-inventory.json`. High-value existing tools are:

- allow-list selection: `load_sources`, `source_selector`;
- staged metadata/data reads: `StagedSourceAdapter`;
- labels/signatures/identity: `label_retriever`, `signature_retriever`, `compound_mapper`;
- overlap/table/splits: `overlap_computer`, `candidate_table_builder`, `grouped_cv_splits`;
- dataset and leakage audit: `dataset_quality_report`, `quality_gates`;
- CoMPARA/ER staging: `labels_for_mode`, `assemble_evaluation_labels`, `assemble_sig_meta`, `slice_gctx_landmark`, `early_fuse`;
- training/evaluation: `nested_group_cv`, `score_candidates`, `select_model`, `fit_balanced`, `evaluate_endpoint`, `grouped_bootstrap_ci`;
- documentation/registration: `_render_model_card`, registry templates, `register_or_update_endpoint`;
- jobs/storage: `run_job`, `LocalStorage`, `coverage_job`, `extract_signatures_job`, `build_explore_map_job`.

Refactoring priorities are to remove dynamic runner loading, extract `PipelineConfig` and card rendering from `pipelines/endpoints/ER/run.py` into core modules, make candidate-table/artifact serialization explicit, split training from registration, and wrap each action with Pydantic request/result models and a permission declaration. Shell commands are not agent tools.

## Missing P0 infrastructure

1. Backend authentication and an `admin` role.
2. SQLite-backed workflow, approval, artifact, agent-run and tool-call records with migrations.
3. An explicit durable state machine with optimistic locking, checkpoints, retries and cancellation.
4. A worker/executor boundary for heavy deterministic jobs.
5. Typed tool registry with scopes, timeouts, idempotency keys and artifact contracts.
6. Source-discovery clients that can **propose**, not ingest or mutate the allow-list.
7. One bounded Dataset Discovery/Evaluation agent with a provider-neutral interface and fake provider.
8. Immutable artifact store with checksums and a candidate-to-published promotion step.
9. `/admin/endpoints` APIs and workflow-control UI.
10. Evaluation fixtures, trace storage, prompt/model versioning, budget enforcement and prompt-injection defenses.

## Strict gap analysis

See `docs/agents/gap-analysis.json`. P0 is reserved for the real vertical slice: durable control plane, source proposal, typed tools, one useful agent, approvals, audit trace, training integration, atomic registration and admin review UI. Decorative chat, autonomous agent conversations and broad semantic search are not P0.

## Phased roadmap

### Phase 0 — architectural foundation

Deliver Pydantic/domain schemas, Alembic migrations, SQLite repositories, the state-transition service, immutable local artifacts, provider protocol plus fake provider, worker interface, admin read/control APIs and skeletal list/detail routes. Exit when restart recovery, optimistic locking, invalid transitions, cancellation and approval immutability pass tests. Demo value: a truthful controllable workflow shell.

### Phase 1 — first real agent

Deliver typed GEO/search/metadata tools, Dataset Discovery/Evaluation agent, OpenAI provider, structured output, local trace mirror, budgets, citations, candidate comparison and dataset approval. Exit when benchmark fixtures reject fabricated accessions, validate citations and pause for approval. Demo value: live, bounded discovery.

### Phase 2 — curation and dataset construction

Generalize endpoint configs and staging contracts; add inspection, label-proposal, identity, quality and leakage tools; add label/identity approvals. Exit when an oxidative-stress fixture reaches training approval with immutable artifacts and no prose calculations. Demo value: evidence-backed scientific review.

### Phase 3 — training and evaluation

Separate training from registration, execute existing grouped pipeline through the worker, persist metrics/calibration/model candidates, add evaluation summary and scientific approval. Exit when interruption/retry is idempotent and a weak model cannot advance. Demo value: real deterministic model evidence.

### Phase 4 — registration and product integration

Add endpoint candidate schema, checksum-verifying publisher, explicit registration approval, atomic registry update and serving reload/deploy procedure. Exit when a registered candidate appears through `/endpoints` and Model Library with rollback and tamper tests. Demo value: visible end-to-end product expansion.

### Phase 5 — retrieval, evals and presentation hardening

Add hybrid document retrieval where justified, the full benchmark, replay, cost/latency dashboard, precomputed demo run and external-service fallbacks. Exit when the 10-minute script succeeds offline except for an intentionally live discovery segment. Demo value: repeatability and operational proof.

## Validation of this audit

- Every item marked existing names a repository file and callable function/class.
- Dataset discovery, LLM agents, DB workflows, admin API/UI, authentication and durable queue are explicitly marked missing.
- Training and registry writes remain behind human decisions.
- No proposed agent has shell access, raw filesystem access or direct registry write permission.
- `AgentProvider` keeps OpenAI first and permits a later Anthropic adapter; tests use a deterministic fake.
- The design reuses the current registry-driven frontend rather than hard-coding oxidative stress.
- No optional production or prototype scaffold was added in this audit; documentation and machine-readable design artifacts are sufficient to validate the plan.

Related documents: `target-architecture.md`, `oxidative-stress-vertical-slice.md`, `admin-console-spec.md`, `agent-evaluation-plan.md`, `security-threat-model.md`, `workflow-state-machine.json`, `tool-inventory.json`, and `gap-analysis.json`.
