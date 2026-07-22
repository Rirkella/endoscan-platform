# Provider Architecture

## Boundary model

EndoScan has two provider boundaries:

1. A provider-neutral model interface accepts a typed prompt/context, allowed tools, structured-output schema, and budget. `FakeAgentProvider` supports deterministic tests; `OpenAIAgentProvider` is the optional live adapter.
2. Reviewed scientific-source providers expose typed operations over fixed source families. Models select an operation and arguments; they do not supply arbitrary URLs, credentials, parsers, or transport policy.

Provider results are untrusted input. Strict schemas, allowlists, MIME and size checks, completion proof, artifact hashes, cache fingerprints, and safe diagnostics apply before a result enters workflow state.

## Reviewed source families

The pipeline needs data by function before it needs a brand name: laboratory activity records define possible endpoint labels; chemical-identity records bridge source IDs to canonical compounds/structures; cellular-response profiles supply expression features and biological context; additional public studies broaden evidence beyond a dedicated catalogue; and supporting metadata/publications establish design, access, release, and interpretation. Providers below implement one or more of those functions without implying that records from different families are equivalent.

Adapter definitions are versioned (`1.0.0` in the current registry) and operations are enumerated in code. The table summarizes their production intent; the [canonical status matrix](STATUS_AND_ROADMAP.md#canonical-status-matrix) distinguishes implementation from live validation.

| Source family | Adapter / principal operations | Release and provenance | Pagination/cache | Heavy-data boundary |
|---|---|---|---|---|
| PubChem BioAssay | `pubchem-bioassay`: search/validate assays, assay metadata, activity and identifier availability, bounded result extraction, outcome summary, counter-screen relationships | NCBI/PubChem identifiers, locator, retrieval metadata, raw SHA-256 | Typed batching/page completion; boundary-fingerprint cache | Metadata and bounded rows pre-approval; full assay tables require approved materialization |
| EPA ToxCast / invitroDB | `epa-toxcast-public-downloads` plus `epa-comptox-toxcast`: release, package, annotations, target mapping, summaries, chemical archive and optional API metadata | Reviewed public release manifests; invitroDB release/version and official citation retained | File/page completion proofs and normalized disk cache | Multi-gigabyte release files are manifested/staged once and selectively normalized; not loaded in model context |
| Tox21 | Pre-approval Tox21 release provider: assay summary, compound identifiers, activity rows, assay relationships | Reviewed official public release and file hashes | Deterministic page/file completion and replay cache | Complete tables are handled as provider artifacts/jobs, never prompt payloads |
| NCBI GEO | `ncbi-geo-series`: search/validate series, metadata, perturbation design, identity fields, conditions, matrix availability and feature schema | Accession, official NCBI record/file listing, retrieval time, artifact hash | E-utilities batching and bounded file listing; cached by request/release fingerprint | Discovery fetches metadata/listings; matrices are delegated to GCTX/extraction jobs |
| LINCS L1000 | `lincs-l1000`: distribution health, resource search, perturbagen/signature/feature metadata, processed-signature availability, release manifest | Reviewed LINCS or official GEO distribution, release identifiers, artifact hashes | Metadata index/cache with bounded counts | GCTX matrices are never downloaded in discovery; streaming/indexed jobs slice approved data |
| PubChem Compound | `pubchem-compound`: source identity fields, record availability, sampled identity and synonym resolution | CID/property response, locator, retrieval metadata, raw hash | Input batches obey typed limits and merge deterministically; cache per normalized batch | No bulk identity resolution during discovery |
| NCBI supporting metadata | `ncbi-supporting-metadata`: supporting records, official file listings, linked publications, source access | NCBI identifiers, official links, retrieval metadata, raw hash | Bounded identifier batching and cache | Supporting metadata only; scientific payload remains in its source adapter |

ToxCast and Tox21 are separate source families even when assays or compounds overlap. LINCS and GEO are separately searched and deduplicated through provenance-aware identifiers. PubChem BioAssay IDs are not compound IDs.

### Implementation, live evidence, and limits

| Provider | Current implementation | Live technical evidence | Known limitation |
|---|---|---|---|
| PubChem BioAssay | Registered, typed discovery/hydration and bounded materialization; offline tested | Selected assay rows/relationships and replay passed | Search relevance, endpoint semantics, and complete compound-level availability vary by assay |
| EPA ToxCast/invitroDB | Registered public-release manifests, typed table roles, selective archive handling; offline tested | Public v4.3 release/archive path and replay passed | Public releases are large; endpoint mapping and label construction still require review |
| Tox21 | Registered exact release/file roles with normalization; offline tested | Bounded activity/mapping release operations and replay passed | Tox21 does not substitute for all ToxCast or endpoint-specific evidence |
| NCBI GEO | Registered series search/hydration/file metadata; offline tested | One selected series metadata path and replay passed | Free-text indexing is noisy; compound/dose/time identity and processed matrices may be incomplete |
| LINCS L1000 | Registered metadata, disk index, streaming/selective data boundary; offline tested | Pending | Distribution/release access and real metadata completeness still need bounded acceptance |
| PubChem Compound | Registered typed sampled identity/property/synonym mapping; offline tested | Bounded CID mapping and replay passed | Names, salts, mixtures, stereochemistry and source identifiers can remain ambiguous |
| NCBI supporting metadata | Registered typed supporting/file/publication access; offline tested | Pending | Supporting records cannot repair absent primary scientific evidence |

## Operation contract

A reviewed operation fixes:

- adapter ID/version and source system;
- supported component roles;
- operation name and typed input/output schema;
- reviewed request builder or release resource template;
- allowlisted host(s), HTTPS and redirect policy;
- method, timeout, retry count, rate limit, response-size and MIME bounds;
- parser identity/version;
- cache TTL/fingerprint and completion proof;
- provenance and artifact requirements.

Endpoint-specific assay IDs, accessions, counts, or expected answers do not belong in universal adapter definitions. A discovery scope may contain a separately reviewed hint, but blind benchmarks keep it empty.

## Discovery, hydration, and materialization

Search produces candidates, not verified evidence. Hydration uses source-specific metadata operations to establish target, modality, organism/model, measurement, compound-identifier availability, processed-data availability, and official download location. Candidate status remains explicit when a source lacks the prerequisite.

Materialization turns provider outputs into immutable candidate/hydrated sets. The semantics-v2 executor validates provider output, records raw and normalized arguments, preserves partial observations, and records gaps. Deterministic internal batching prevents model prompts from reasoning about limits such as ten identifiers per metadata call.

## Pagination and completion proof

A provider result is complete only when its declared retrieval scope is proven complete. Depending on the source, proof includes page offsets/tokens, expected versus observed file members, content length and hash, archive member inventory, row counts, or an explicit bounded-sample marker. A successful HTTP response without completion proof is not silently represented as a complete dataset.

## Cache and replay

Cache keys include normalized typed request, adapter/operation version, reviewed locator or release, and relevant parsing policy. Raw bytes are persisted once by hash. A replay references the same artifact and records a cache hit with zero transport attempts. Refreshing the browser reads persisted state and cannot initiate provider work.

## Heavy data

Provider discovery is metadata-first. Full assay tables, ToxCast archives, LINCS/GEO matrices, and mass identity resolution are jobs with separate authorization, storage limits, checksums, restart semantics, and output manifests. Selective ZIP/GCTX readers avoid loading entire resources when the approved task needs a bounded slice.

## Model-provider diagnostics

Safe diagnostics preserve normalized error type/code/message, original exception class, HTTP status, provider error code, request/response ID, parameter name when available, retryability decision, attempt count, usage, cost, and latency. Secret-like values and authorization headers are redacted. Provider retries are explicit; zero means one generation attempt total.

## Live validation boundaries

Technical live smokes validate connectivity, parsing, completion, artifact persistence, and replay for a bounded known operation. They do not establish source suitability for an endpoint. Selected Tox21, PubChem BioAssay/Compound, GEO, and public ToxCast operations have bounded smoke evidence; LINCS and NCBI supporting-metadata live acceptance are pending, and complete broad-thyroid discovery remains unvalidated. Historical execution transcripts are retained in Git history rather than presented as current product documentation.

## Extending a provider

Add a versioned declaration, immutable fixtures, request-policy tests, parser tests, completion proof, cache/replay tests, provenance assertions, safe-error cases, and capability registration. Test oversized responses, MIME mismatch, redirects, missing identifiers, partial pages, duplicates, and zero candidates. A new provider is not operational until its declaration and execution path agree and the status matrix identifies its actual validation level.
