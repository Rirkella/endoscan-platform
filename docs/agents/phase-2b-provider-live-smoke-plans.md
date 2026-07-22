# Phase 2B metadata-provider live-smoke plans

Status: executed on 2026-07-21 in an isolated technical workflow. No OpenAI agent,
scientific endpoint build, source selection, assembly recipe, expression retrieval,
dataset assembly, or model training was run.

Validated outcomes:

- Tox21: an exact source-constrained assay was hydrated through summary, concise
  compound-level results and assay-description relationships. The live validation
  retained 10,486 activity rows, five relationships and 10,486 source-backed compound
  mappings; replay used zero transport requests.
- PubChem BioAssay: an exact reviewed AID was hydrated through the same complete typed
  operation set. The live validation retained 25 activity rows, 12 relationships and
  25 source-backed compound mappings; replay used zero transport requests.
- PubChem Compound: one reviewed 21-CID property request produced 21 exact CID,
  InChIKey, title, and connectivity-SMILES mappings. Replay used zero transport
  requests. The production locator now strips only a validated `CID:` prefix and
  rejects AID or noncanonical identifiers.
- NCBI GEO: exact accession `GSE330744` was resolved and hydrated through ESummary and
  the endpoint-scoped Series SOFT response. The parser retained organism, design,
  exposure time, processing level and official processed-file locators, classified the
  name-only hit as unrelated, and replayed all three resources with zero requests.
- ToxCast: the reviewed 7,502,075,199-byte Summary Files archive was staged once with
  SHA-256 `c61d46cf685d5d84e72d0d4f6f46746ab1714d6f924e43480d390925ae827c59`.
  Verified mc4, mc5-6 and sc1/sc2 roles are normalized separately into an immutable,
  indexed SQLite activity cache. Public coverage uses that cache without an EPA key;
  later replays perform no archive request, extraction or normalization.

The operational capability registry records every pre-approval provider as operational.
Heavy expression slicing, selected-source ingestion, dataset construction and training
remain later approval-gated work.

Common controls for every smoke:

- use a new technical workflow ID, not a scientific endpoint build;
- provider retry count `0`, HTTP redirect count at most `2`, timeout `30 s` per request;
- request only the hosts and templates in
  `registry/data/preapproval_provider_releases.json`;
- persist every response as an immutable SHA-256 artifact before cache insertion;
- require a completion proof; a safety limit is `incomplete`, never `completed`;
- execute one immediate cache replay and require zero transport requests plus identical
  counts and fingerprints;
- keep complete rows in a provider SQLite artifact; API/tool output is limited to 20
  preview candidates and exact counts;
- prohibit arbitrary/model-generated URLs, automatic provider substitution, label
  inference, chemical-form collapse, final source selection, and heavy data.

## ToxCast / invitroDB

Release: `invitrodb-v4.3-2025-08`; adapter `epa-comptox-toxcast@2.2.0`.

Two access modes are deliberately separate:

- `public_release` is mandatory, unauthenticated, and reproducible. Clowder is the source
  of record for release v4.3, datasets, file inventory, archive-member inventory, public
  workbooks, checksums, and DOI provenance. A release/dataset listing is never a chemical,
  assay, or activity row.
- `authenticated_api` is optional acceleration only. The pinned CTX contract is OpenAPI
  `3.1.0`, API `1.1.1`, schema SHA-256
  `5a14591752181a0f841d30ca6157006b1a9632ca6d524c878cd777d3b5257798`.

The public smoke reconciles the release space and file lists for MySQL, Summary Files,
Assay Description Documents, and Release Note. It inspects the immutable Clowder archive
inventory and downloads only these reviewed lightweight scientific workbooks:

1. `assay_annotations_invitrodb_v4_3_AUG2024.xlsx` - 1,647 assay endpoints;
2. `assay_target_mappings_invitrodb_v4_3_AUG2024.xlsx` - source target relations;
3. `analytical_qc_invitrodb_v4_3_AUG2024.xlsx` - DTXSID/SPID quality references;
4. `cytotox_invitrodb_v4_3_AUG2024.xlsx` - DTXSID/CHID cytotoxicity references.

The filename suffix is preserved exactly as published even though the release authority is
August 2025. The provider never downloads `INVITRODB_SUMMARY.zip` (7,502,075,199 bytes)
or `invitrodb_v4_3.sql.gz` (17,651,807,605 bytes) automatically. An operator-approved
one-time staging command validates the exact archive identity, size and SHA-256 before
placing it in the ignored provider cache. Runtime endpoint coverage only opens the
verified local mc4, mc5-6 and sc1/sc2 normalized tables; it cannot trigger a bulk
download, extraction or scientific-role inference.

The content-addressed normalized SQLite index is a local derived cache whose required
free space is checked separately before normalization. It is not constrained by the
network response-size policy: every downloaded public file remains bounded by its own
reviewed manifest size and checksum, while the complete normalized cache retains all
source rows needed for deterministic endpoint coverage.

Scientific requests use only these OpenAPI operations:

1. `GET /ctx-api/bioactivity/assay/` for the complete `AssayAnnotation[]` catalogue;
2. `GET /ctx-api/bioactivity/data/summary/search/by-aeid/{aeid}` for `AssayAgg[]`;
3. `GET /ctx-api/bioactivity/data/search/by-aeid/{aeid}` for complete
   `BioactivityDataAll[]` rows for an explicitly matched AEID.

Those API operations run only after explicit `authenticated_api` selection with an already
configured `EPA_COMPTOX_API_KEY`, sent only as `x-api-key`. Missing authentication never
affects `public_release`, never removes ToxCast from the DiscoveryPlan, and never triggers
PubChem substitution. The public smoke builds disk-backed assay, target-relation and
chemical-reference indexes, connects them to the separately staged activity cache, and
then replays entirely from cache. PubChem AID and AEID remain assay identifiers, never
compound identities. Relationships are derived only from source fields; unavailable
source-defined counterscreen roles are reported as unavailable and never inferred.

It is a structural failure if a release-list response is assigned a scientific role or
if identical payloads satisfy distinct mandatory scientific roles. The latter emits
`ROLE_ENDPOINT_ALIASING_DETECTED`, and normalization does not run.

## Tox21

Release: `tox21-pubchem-reviewed-current`; adapter `tox21@2.0.0`.

Exact transport is NCBI E-utilities `pcassay` search with the fixed source constraint
`Tox21[SourceName]`, deterministic reviewed target/modality terms, `retmode=json`,
`retmax=500`, and `retstart` pages. At most 20 pages (10,000 assay identifiers) are
allowed in a bounded smoke; stop earlier only when the provider result count/cursor is
exhausted. Hydration is deterministically bounded by the request contract, and the
completion result remains explicitly truncated if the catalogue exceeds that bound.

Every record must retain `provider=tox21` even though PubChem is the transport. Success
requires distinct Tox21 source provenance, AID used only as an assay identifier,
compound identifier-field and activity-outcome availability, relationship provenance,
raw artifacts, normalized SQLite, and cache-only replay. Generic PubChem results,
ToxCast substitution, AID-as-compound use, or an exhausted safety bound reported as
complete are failures. Full assay result tables are prohibited.

## PubChem BioAssay

Release: `pubchem-bioassay-reviewed-current`; adapter
`pubchem-bioassay@2.0.0`.

Use the reviewed NCBI E-utilities `pcassay` search template with a bounded scientific
term, `retmode=json`, `retmax=500`, and successive `retstart` values. Limit a smoke to 20
pages and an explicit hydration ceiling. Record provider total count, every visited
cursor, deduplication count, and whether the result set was exhausted or
safety-truncated.

Success requires deterministic query provenance, no descriptive rank used as
selection, linked-assay visited ledger, source-backed compound/result-field
availability, immutable artifacts, disk-backed normalized records, and zero-request
replay. It is a failure to call the top-N subset complete, discard candidates, follow an
unapproved relation, or treat AID as a compound. Bulk result tables are prohibited.

## PubChem Compound

Release: `pubchem-compound-reviewed-current`; adapter
`pubchem-compound@2.0.0`.

Prepare an immutable technical input artifact containing exactly 21 reviewed PubChem
CIDs. Execute PUG REST property requests in deterministic batches of at most 100 using
only:

`GET https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{reviewed-batch}/property/Title,InChIKey,CanonicalSMILES/JSON`

The smoke permits one request for 21 identifiers and a 10 MiB response. Success
requires 21 explicit mapping outcomes (including missing/ambiguous when returned), full
InChIKey and source structure fields without standardization, exact upstream/processed
count reconciliation, immutable artifacts, indexed SQLite, and zero-request replay.
Sampling, fuzzy identity, silent salt/parent collapse, or a missing upstream identifier
without an explicit status is failure.

## NCBI GEO

Release: `ncbi-geo-reviewed-current`; adapter `ncbi-geo-series@2.0.0`.

Use the reviewed NCBI E-utilities `gds` query template with a neutral perturbagen term,
the fixed `gse[Entry Type]` constraint, `retmode=json`, `retmax=500`, and successive
`retstart` values. Limit the smoke to ten pages. Hydrate at most five returned Series
through official GEO Series/Sample metadata and supplementary-file manifests. No
matrix or supplementary archive may be downloaded.

Success requires explicit classification of each hydrated candidate, experimental
application evidence for any `verified_compound_perturbation`, organism/model/dose/time
and control-design presence or explicit missing fields, processed-matrix availability
and locator metadata, immutable artifacts, indexed SQLite, and zero-request replay.
Name-only matches must not be promoted. GEO must not replace LINCS and missing processed
data must remain a scientific outcome rather than an execution failure.

## Recommended authorized sequence

For one combined bounded provider smoke, run ToxCast, Tox21, PubChem BioAssay, PubChem
Compound, and GEO in that order. Stop on the first security, provenance, parser, cache,
or completion-proof failure. Run the one replay immediately after each cold provider;
do not defer replay until the end. Do not include LINCS in the cold run: Phase 2A's
successful four-file hashes and cache regression are the evidence of record.

After all five smokes pass, the first semantics-v2 metadata-only discovery should use a
fresh approved specification and immutable `DiscoveryPlan`, create its complete ledger,
execute every applicable provider task, persist only disk-backed complete sets, and
stop after `SourceCandidate` and `HydratedSource` production for human review. Do not
compute a `StrategyProposal`, create an `AssemblyRecipe`, retrieve GCTX, or authorize
ingestion in that run.
