# Oxidative-stress vertical slice

This is a real workflow design, not a claim that EndoScan currently has an oxidative-stress dataset or model. Exact datasets and label semantics must be discovered and approved during implementation; no accession is fabricated here.

## Endpoint draft

The administrator enters a draft name, biological definition, intended transcriptomic claim, excluded claims, organism/context constraints and success criteria. A required endpoint-definition approval freezes version 1 before discovery. The draft must state that EndoScan predicts a response-defined oxidative-stress endpoint from transcriptomic signatures and does not claim general toxicity, clinical relevance or regulatory validation.

## Stage contract

| State/stage | Inputs | Responsible component | Outputs/artifacts | Approval, retry and UI |
|---|---|---|---|---|
| `DRAFT` | admin fields | human + validation tool | endpoint-definition v1 | Mandatory definition approval; editable form with scope warnings |
| `DISCOVERING_DATA` | approved definition, term set | Dataset Discovery Agent | search plan, cited candidate metadata, query trace | Retry model/network safely; pause after a bounded search; live trace summary, not chain-of-thought |
| `AWAITING_DATASET_APPROVAL` | candidate artifacts | Dataset Evaluation Agent then human | comparison, recommendation, limitations | Human approve/reject/revise/alternative; comparison table with accessions, controls, licence, sample estimates and provenance |
| `CURATING_DATA` | approved candidate | deterministic fetch/metadata/sample tools + Scientific Curation Agent | checksummed raw metadata, inspection report, proposed inclusion/exclusion and label rule | Network retries by idempotency key; pause allowed; source/artifact browser |
| `AWAITING_LABEL_APPROVAL` | curation proposal | human scientist | immutable label-rule decision | Mandatory; show exact evidence, ambiguity and excluded samples |
| `RESOLVING_IDENTITIES` | approved rows, compound fields | deterministic identity tools | mapping table, coverage, conflicts | Unresolved/ambiguous conflicts create human approvals; no silent choice |
| `AUDITING_DATASET` | label/signature/mapping artifacts | generalized existing core tools | candidate table, dataset card, quality report/gate | Automatic calculation; retry only from pinned inputs; render observed values and thresholds |
| `AUDITING_LEAKAGE` | candidate table/split plan | deterministic leakage tool | compound-overlap audit, split manifest | Any leakage blocks; show offending compounds and groups |
| `AWAITING_TRAINING_APPROVAL` | all approved artifacts, passing gate | human | authorization bound to artifact-set hash | Mandatory; no training button until guards pass |
| `TRAINING` | pinned table/config/seed | worker using current grouped training code | candidate models, fitted artifacts, logs | Cooperative cancel; idempotent rerun under new attempt; progress and resource use |
| `EVALUATING` | candidate predictions/models | current evaluation/calibration tools | nested metrics, CIs, calibration, selection report, limitations inputs | Automatic; weak evidence is an honest result, not a retry trigger |
| `AWAITING_SCIENTIFIC_APPROVAL` | evaluation and card drafts | Evaluation/Audit Agent then human | accept/reject/revision decision | Mandatory model acceptance; side-by-side floors, CIs, instability, scope and proposed status |
| `REGISTERING` | accepted candidate + separate registration approval | checksum-verifying publisher | versioned model set, registry entry, publish event | Mandatory registration approval; atomic/idempotent publish; no agent permission |
| `COMPLETED` | published entry | orchestrator | final timeline and product link | Verify `/endpoints`, model library and inference smoke; immutable completion summary |

Failure moves to `FAILED` with `failed_from_state`, normalized error and retryability. `PAUSED` retains the original state. A rejected dataset returns to `DRAFT` or discovery with a new endpoint-definition/search version. A label revision returns to curation. A rejected model may return to training only with a new approved config; thresholds cannot be lowered inside a run.

## Concrete implementation path

1. Reuse `registry/data/sources.yaml` only after a human-approved source proposal. Discovery itself cannot write this file.
2. Add source-specific staging adapters under `endoscan_jobs`, based on the ER/AR patterns; do not add live network access to `endoscan_core`.
3. Generalize `PipelineConfig` out of `pipelines/endpoints/ER/run.py` so oxidative stress is not routed through an ER-named module.
4. Reuse `label_retriever` only after adding an explicitly reviewed parser for the approved source. Do not shoehorn oxidative-stress calls into CERAPP/CoMPARA semantics.
5. Reuse canonical full-InChIKey mapping, LINCS signature retrieval, overlap, candidate table, compound-group splitting, quality report and strict gate.
6. Reuse grouped nested evaluation, scorecard selection, uncertainty, data/model cards and registry schema. Endpoint-specific claim wording and source references must come from approved inputs.
7. Register a new uppercase endpoint ID selected by the human (for example a reviewed `OXSTRESS` token); the design does not pre-approve the ID or biological definition.

## Stable but honest demo artifacts

- **Live:** create the draft; run one bounded discovery query against an allowlisted metadata source; display tool calls and citations; human-select a candidate; approve one review card; trigger a deterministic stage; approve registration; show the product endpoint.
- **Cached:** candidate metadata and cited document snapshots from a previously successful identical query. The UI labels cache time, checksum and `live/cached` status. Cache fallback is allowed only when the live service fails or exceeds a short deadline.
- **Precomputed:** downloaded raw data, identity mappings, curated 978-gene matrix, candidate-table audit, leakage report, trained candidate models and evaluation. Each is produced beforehand by the same tools and pinned to checksums. The demo resumes a prepared run at approval checkpoints; it never presents precomputed training as happening live.
- **Never faked:** accession existence, matched-control evidence, labels, counts, identity coverage, leakage, metrics, model status and registration.

## Ten-minute JMLC run of show

| Time | Demonstration | What it proves |
|---|---|---|
| 0:00–1:00 | Problem and scientific boundary | Adding an endpoint is a governed data/model process, not a chatbot answer |
| 1:00–2:00 | Current Analyze and Model Library | Existing registry-driven product and transparent limitations |
| 2:00–2:40 | `/admin/endpoints`: create oxidative-stress draft | Typed scope, ownership and human definition |
| 2:40–4:10 | Start discovery; show bounded live query and cited candidates | Real model/tool activity, budgets, sources and fallback status |
| 4:10–5:00 | Compare candidates and approve one | Human scientific control and immutable evidence |
| 5:00–6:00 | Resume prepared run; inspect curation, labels, identity and leakage artifacts | Deterministic tools do calculations; conflicts are explicit |
| 6:00–6:40 | Approve training; run a short validation step, then open precomputed candidate | Honest separation of live orchestration and precomputed heavy training |
| 6:40–8:00 | Evaluation summary and limitations | Nested grouped metrics, CIs, calibration, model stability and weak-model refusal |
| 8:00–9:00 | Scientific and registration approvals | Two distinct human decisions and checksum-bound publication |
| 9:00–10:00 | Open Model Library and Analyze | Endpoint appears through the real registry/API; timeline and trace remain auditable |

Visible demo metrics: candidate count, citation coverage, identity coverage, positives/negatives, duplicate/conflict rates, leakage verdict, nested AUROC/AUPRC/balanced accuracy/Brier with CIs, selected model, tool calls, latency, token/cost budget, artifact hashes and approval actors.

Fallbacks:

- External metadata failure: after a fixed deadline, load the checksummed cached response and display `cached because upstream unavailable`.
- Model latency/failure: show the last completed discovery run, then retry once or continue with a fake-provider rehearsal only outside the claimed live segment.
- Worker/training delay: start a fast deterministic validation command, then explicitly switch to the precomputed artifact set from the same pinned inputs.
- Registration failure: keep the candidate accepted but not published; do not edit the registry manually during the demo.

The console must visibly prove state transitions, tool names, source citations, artifact checksums, budgets, approvals and the fact that deterministic computations—not LLM prose—produced the scientific numbers.
