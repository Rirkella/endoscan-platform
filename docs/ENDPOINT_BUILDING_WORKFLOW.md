# Endpoint Building Workflow

## Purpose

Endpoint Builder turns a reviewed biological question into a traceable candidate model. It is a governed workflow, not a free-running research agent. Durable state, versioned contracts, immutable evidence, bounded tools, deterministic data work, and explicit approvals separate proposals from actions.

## Lifecycle

| Stage | Principal contract or artifact | Exit condition |
|---|---|---|
| `APPROVED_SPECIFICATION` | Approved training-dataset specification and discovery scope | Exact specification version is approved |
| `DISCOVERY_PLANNING` | `DiscoveryPlan`, provider tasks, budgets | Plan validates against registered capabilities |
| `REGISTERED_PROVIDER_CAPABILITY_BLOCKED` | Capability finding/gap | Required provider becomes available or scope is revised |
| `DISCOVERING_SOURCE_CANDIDATES` | `SourceCandidateSet`, execution ledger | Each mandatory source/modality attempt has a terminal outcome |
| `HYDRATING_SOURCE_CANDIDATES` | `HydratedSourceSet` | Candidate metadata and access conditions are verified or gaps recorded |
| `COMPUTING_COMBINATION_COVERAGE` | `CombinationCoverageSet` | Deterministic join keys, counts, missingness, duplicates, and overlap computed |
| `GENERATING_ASSEMBLY_STRATEGIES` | `StrategyProposalSet` | Evidence-supported strategies and rejected alternatives materialized |
| `AWAITING_ASSEMBLY_STRATEGY_REVIEW` | Pending approval | Reviewer selects or requests revision of an exact proposal version |
| `ASSEMBLY_RECIPE_APPROVED` | Versioned assembly recipe and optional aggregation policy | Approval consumed transactionally |
| `ASSEMBLING_APPROVED_DATASET` | Dataset rows, validation report, hashes | Deterministic assembly succeeds or records a durable failure |
| `AWAITING_DATASET_APPROVAL` | Dataset manifest and preview | Reviewer accepts the exact dataset fingerprint |
| `AWAITING_TRAINING_APPROVAL` | Training/benchmark plan | Reviewer authorizes bounded compute |
| `TRAINING` | Candidate models and split records | All configured candidates finish or fail explicitly |
| `EVALUATING` | Benchmark metrics, diagnostics, limitations | Evaluation contract complete |
| `AWAITING_SCIENTIFIC_APPROVAL` | Model review package | Reviewer approves publication or requests a new benchmark |
| `REGISTERING` | Registry manifest and serving artifacts | Atomic registry write passes schema and checksum validation |
| `COMPLETED` | Published endpoint version | Registry entry is readable and event recorded |

The implementation also retains legacy and revision/failure states needed to read historical builds. State transitions are centralized in `contracts.py`, `state_machine.py`, `service.py`, and the semantics-v2 lifecycle services; UI labels do not define workflow truth.

### Stage responsibilities and validation

All happy-path stages below are exercised by both prepared offline demonstrations. Live/provider and scientific validation are reported separately in the [status matrix](STATUS_AND_ROADMAP.md#canonical-status-matrix).

| State | Input → purpose → output | Deterministic / optional agent work | Human/API action and failure behavior |
|---|---|---|---|
| `APPROVED_SPECIFICATION` | Approved specification/scope → freeze the claim → versioned specification artifact | Compiler/contract validation; an agent may draft before approval | Specification review API; reject/revise creates history, never edits approval |
| `DISCOVERY_PLANNING` | Specification/components → map needs to providers → `DiscoveryPlan` + ledger | Capability matching and budget reservation; agent may propose bounded search terms | Continue/authorize-source-discovery; missing capability can enter blocked state |
| `REGISTERED_PROVIDER_CAPABILITY_BLOCKED` | Capability finding → explain unavailable work → structured gap | Registry comparison only | Reviewer revises scope/provider configuration; no source call |
| `DISCOVERING_SOURCE_CANDIDATES` | Plan → execute mandatory typed searches → `SourceCandidateSet` | Reviewed transport, batching, parse, ledger; agents select/interpret within plan | Authorized continuation; partial/zero results persist, terminal errors stop |
| `HYDRATING_SOURCE_CANDIDATES` | Candidates → verify relevance/obtainability → `HydratedSourceSet` | Typed metadata fetch, stable-ID validation, merge; agents may request allowed hydration | Same discovery authorization; unmet dependencies become gaps, not invented calls |
| `COMPUTING_COMBINATION_COVERAGE` | Hydrated sets → measure possible joins → `CombinationCoverageSet` | Entirely deterministic joins/counts/conflicts/missingness | `calculate-combination-coverage`; validation failure is durable/revisable |
| `GENERATING_ASSEMBLY_STRATEGIES` | Coverage → compare defensible constructions → `StrategyProposalSet` | Deterministic feasibility plus optional agent synthesis | `generate-assembly-strategies`; insufficient evidence remains explicit |
| `AWAITING_ASSEMBLY_STRATEGY_REVIEW` | Proposals → select exact policy → pending approval | Read-only rendering/validation | `approve-assembly-strategy` or reject/revise; no assembly before approval |
| `ASSEMBLY_RECIPE_APPROVED` | Selected proposal/policies → pin execution → `AssemblyRecipe` | Deterministic recipe compilation and fingerprint | Approval consumed transactionally; changed inputs invalidate it |
| `ASSEMBLING_APPROVED_DATASET` | Recipe + immutable source artifacts → build rows → dataset bundle, quality report, Explore artifacts | Deterministic jobs only; no model synthesis | `run-approved-assembly`; job failure retains partial trace and supports revision |
| `AWAITING_DATASET_APPROVAL` | Bundle/preview/fingerprint → review data → dataset approval | Read-only quality and lineage views | `approve-dataset` or `review-dataset-revision`; fingerprint-bound decision |
| `AWAITING_TRAINING_APPROVAL` | Approved dataset + `BenchmarkPlan` → authorize compute | Deterministic plan/schema validation | `run-benchmark` is the explicit training authorization; rejection stops |
| `TRAINING` | Dataset/plan → fit candidates → model artifacts and split records | Deterministic configured training; no source/provider agent | Bounded execution; each failure is recorded without publication |
| `EVALUATING` | Candidates/splits → compare and validate → `ModelBenchmarkResult`, validation result, model card | Deterministic metrics/diagnostics; optional explanatory synthesis cannot change metrics | `select-and-validate-model`; failed criteria remain visible |
| `AWAITING_SCIENTIFIC_APPROVAL` | Review package → accept claim/version → approval or revision request | Read-only comparisons and limitations | `review-models`; can approve, reject, or request another benchmark |
| `REGISTERING` | Approved model/card/manifest → verify serving contract → endpoint/model registry entry | Atomic schema/hash/serving verification | `publish-endpoint`; collision or verification error stops publication |
| `COMPLETED` | Published registry entry → expose reviewed endpoint → terminal build | Read-only verification | Admin/API reads; a new scientific change starts a new version/build |

## Specification and discovery scope

A specification defines the prediction claim, observation grain, target-table fields, acceptable evidence, identity/structure requirements, exclusions, assumptions, and unresolved questions. An `EndpointDiscoveryScope` can be fixed-modality or broad-modality exploration. Source hints are optional reviewed inputs, never hidden expectations.

For broad receptor discovery, binding, agonism, and antagonism are candidate modalities investigated separately. Raw observations keep their modality and assay provenance. Discovery cannot aggregate them or choose a winning model. A later planner may compare modality-specific endpoints, separate models, hierarchical evidence use, or a functional union when the observed evidence supports it.

## Discovery planning and execution

`DiscoveryPlan` decomposes requirements into typed `ProviderDiscoveryTask` objects. The registered provider capability matrix determines which task is executable. A `DiscoveryExecutionLedger` reserves and records attempts so one successful modality cannot consume capacity reserved for mandatory peers.

Agents can propose search terms or select reviewed operations. Deterministic code validates arguments, applies provider limits and batching, executes transport, stores raw responses, and normalizes observations. Zero candidates and `dependency_unmet` are valid scientific outcomes. Orchestration-generated schema violations are implementation failures.

Candidate search hits remain `metadata_candidate` until hydration establishes the fields needed to judge scientific relevance and obtainability. PubChem assay IDs are assay identifiers, never compound identifiers. Identity tools receive only validated source compound identifiers; absence becomes a structured gap.

## Coverage and joinability

Combination coverage is deterministic. Before any assembly decision it reports, per source and modality:

- candidate and verified-record counts;
- stable compound identifiers and mapping status;
- assay or profile identifiers;
- duplicates and conflicts;
- activity-label/value availability;
- transcriptomic model, dose, time, processing level, and locator availability;
- activity/transcriptomics overlap by modality;
- missing or unusable records with reasons;
- whether deterministic preview rows are possible.

The shortest validated path is preferred, but evidence cannot be invented to complete a table. A precise, evidence-backed blocking gap is an acceptable outcome.

## Assembly strategies and modality aggregation

`StrategyProposalSet` compares evidence-supported recipes. A `ModalityAggregationPolicy` may describe a derived endpoint, source modalities, assay roles, exclusions, aggregation operator, active/inactive/inconclusive/conflict/missing rules, provenance requirements, rationale, approval status, and policy version.

Permitted patterns include modality-specific models; an approved agonism OR antagonism functional union; a hierarchy where functional assays define labels and binding supports interpretation; or separate models plus a derived summary. No pattern is universal. Binding cannot be silently promoted to functional activity. Approval never destroys or overwrites the original records.

## Deterministic assembly

An approved recipe pins source artifacts, release provenance, joins, filters, label policy, feature policy, exclusions, and expected validation. Assembly produces row-level artifacts, identifier bridge, joinability report, preview, validation report, and hashes. It does not ask a model to invent labels or mappings.

The main lifecycle contracts are `DiscoveryPlan`, `DiscoveryExecutionLedger` (the execution ledger), `SourceCandidateSet`, `HydratedSourceSet`, `CombinationCoverageSet`, `StrategyProposalSet`, `AssemblyRecipe`, the assembled dataset bundle, quality report, `BenchmarkPlan`, `ModelBenchmarkResult`, validation result, model card, endpoint/model registry entry, and Explore artifacts. Compact workflow JSON contains their metadata and references; row-level scientific tables remain external immutable artifacts rather than embedded workflow payloads.

Dataset approval is tied to the dataset fingerprint. Post-approval mutation requires a new version and review. Explore artifacts may be materialized at this boundary so reviewers can inspect the assembled space before training.

## Training, evaluation, and publication

Training executes only after a separate approval. Split logic and feature generation are deterministic and leakage-aware. Benchmark artifacts record candidates, hyperparameters, splits, metrics, uncertainty, applicability diagnostics, and failures. Scientific review can approve, reject, or request another benchmark; it cannot rewrite the result.

Registration verifies schema compatibility, model and feature hashes, endpoint/version uniqueness, limitations, and serving readiness. Publication is an explicit state transition, not a side effect of training.

## Approval semantics

- Approvals name the build, gate, target artifact/version, reviewer decision, and timestamp.
- Pending approvals are not transferable between builds or artifact versions.
- Consumption and transition happen in one transaction.
- Rejection and revision remain in history.
- UI buttons call the production approval API; browser state does not itself confer approval.

## Restart and failure behavior

SQLite holds the state machine, steps, approvals, agent runs, events, errors, budgets, and artifact references. Content-addressed files hold larger payloads. On restart, the service reconstructs the build from durable records. Completed tool calls are replayed from cache where policy permits; browser refresh performs reads only. Retry counts are explicit, provider error diagnostics are sanitized, and a terminal error cannot silently create a second paid run.

## Offline demonstration

The `tr_receptor` and `dna_damage` fixtures exercise every governed stage, including approvals, assembly, benchmark, and registration, with fake providers and prepared source observations. They prove workflow wiring and reproducibility, not live-source completeness or biological validity. See [Offline Demo](OFFLINE_DEMO.md).
