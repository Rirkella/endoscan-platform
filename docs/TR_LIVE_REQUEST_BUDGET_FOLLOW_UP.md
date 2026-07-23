# TR live request-budget follow-up

This note records the immutable-evidence audit of build
`build-2828690c-994e-59dc-b7b3-a2d6a2630adb`. The build remains historical
evidence and is not a valid budget-enforcement or joinability acceptance run.
All timestamps are UTC.

## Request timeline

| Time | Evidence-backed event |
|---|---|
| 13:25:10.955334 | Discovery was authorized and entered `DISCOVERING_SOURCE_CANDIDATES`. |
| 13:25:21.674281 | PubChem BioAssay binding task `task-84dd44b33738f9db0baf771f` was checkpointed as running. |
| 13:25:22.305434 | Its first source response was persisted. |
| 13:26:03.786507 | The 80th source response was persisted: PubChem relationship metadata for AID 715455, artifact `art-306e13ee-553c-5456-9d77-8ca24045205e`. |
| 13:26:04.310785 | The 81st response reached transport and was persisted: PubChem compound identifiers for AID 345732, artifact `art-32bccf88-d2bc-58a0-9ec3-17fd79e39aab`. |
| 13:28:21.674281 | The binding tool's configured 180-second deadline elapsed. The worker was not cooperatively cancelled. |
| 13:28:21.860689 | A further PubChem response was persisted after that deadline. |
| 13:34:56.827590 | Only after the worker stopped did the executor persist `tool_timeout`, artifact `art-2431f2e6-dddc-5b5e-b677-bd27f7cdf8b8`, SHA-256 `46fe2658d091400ebde115cd21598e7bfb33b4b3e8c651ec9d92e81d211c6dda`. |
| 13:34:56.846413 | The misleading task result was persisted with zero source requests and zero rows, artifact `art-586169ab-8db6-5e42-8a73-4e3b0e7902ee`. |
| 13:34:56.914332–13:38:41.791811 | The remaining provider tasks executed. PubChem agonism completed; PubChem antagonism retained partial evidence; ToxCast reported an unstaged public activity cache; Tox21 returned no candidates; GEO completed; LINCS was rejected locally by the response-size policy; identity and supporting metadata completed. |
| 13:40:01.014100 | The old workflow incorrectly reached assembly-strategy review with only zero-row blocked proposals. |

Exactly 80 responses were persisted before the configured cap, 272 more
between cap exhaustion and the binding deadline, 730 more between the
deadline and the executor's eventual timeout return, and 378 during later
tasks. Thus the old compact ledger reported 379 logical scientific-source
requests, while immutable raw-response/cache evidence proves 1,460 source
responses in the live window. The difference is not candidate expansion:
it is missing task accounting caused by discarding the timed-out worker
result.

No provider retry was recorded for the binding worker. The expansion came
from one search followed by per-assay identifier, activity, and relationship
requests. The later providers were sequential, not the cause of the hidden
binding-worker overrun.

## Root cause

The former `ToolRegistry.invoke` called `future.result(timeout=...)`, then
`future.cancel()` inside a `with ThreadPoolExecutor(...)` block. Python cannot
cancel a running thread. Returning from the block invoked
`executor.shutdown(wait=True)`, so the caller waited while the worker kept
issuing requests. No build-global counter existed at the transport boundary;
the configured 80-request value was only planning metadata.

The replacement is a persisted, atomic build/round governor at the common
HTTP transport boundary plus a cooperative task cancellation/deadline
context. Cache observations never consume transport budget. Reconciliation
derives counts from immutable attempt rows and can repair summary counters
without changing source artifacts.

## Binding and antagonism findings

| Provider / modality | Old result | Evidence-based cause | Cache-only recovery |
|---|---|---|---|
| PubChem BioAssay / binding | `tool_timeout`; ledger said 0 requests | Large per-assay expansion, missing global governor, and non-cooperative thread timeout. No parser or query defect was observed. | Responses are cached, but the discarded result never produced the final normalized dataset. A fresh build can replay cached responses and normalize them. |
| PubChem BioAssay / antagonism | Partial; ledger reported 263 requests | One official request for compound identifiers of AID 677155 ended in `transport_failure`; the remaining evidence was retained. No parser defect was observed. | All successful records are cache-replayable; the failed AID still requires one governed request if it remains required. |
| ToxCast / binding, agonism, antagonism | Partial, 10 requests each | `PUBLIC_TOXCAST_ACTIVITY_CACHE_NOT_STAGED`. Public release provenance and assay metadata were available, but the reviewed compound-level activity archive was not staged. | Metadata is cache-replayable; compound-level activity is not recoverable until the reviewed archive is staged. |
| Tox21 / binding, agonism, antagonism | Completed with no candidates | Valid zero-candidate scientific outcome, not an execution failure. | Yes. |

## Transcriptomic joinability policy

LINCS L1000 is the primary registered perturbational source. Coverage uses its
artifact-backed `pert_info` and `sig_info` indexes, resolves perturbagen IDs
through PubChem CID or InChIKey bridges, and measures overlap and cell/dose/time
context deterministically. Expression values remain inaccessible until an
exact strategy is approved and the registered Level-5 extraction path is used.

GEO remains supplemental. A GEO study without an explicit stable chemical
bridge, verified perturbation/design, usable context, and a processed-matrix
or approved extraction locator cannot produce a viable strategy. Titles and
free text are never converted into compound mappings.

Binding, agonism, and antagonism stay separate throughout discovery and
coverage. No functional union is created automatically.

## Agent boundary

Dataset specification has an existing bounded model-backed agent path.
Semantics-v2 source planning, source retrieval, identity resolution,
combination coverage, and the current strategy proposal generator are
intentionally deterministic. The preserved live run recorded zero provider
generations and zero model turns and must be described as deterministic
agentic orchestration, not a live LLM-agent run.

## Fair scheduling policy 1.0.0

The 80-request governor remains the final transport authority. A persisted
build/round scheduler now reserves breadth before any task can use the shared
remainder. Every reservation, consumption, release, terminal outcome, provider
cap, source-family cap, and evidence-role cap survives restart and is exposed
in discovery progress.

The default 80-request policy is:

| Task class | Initial per task | Maximum per task | Family / role cap |
|---|---:|---:|---:|
| LINCS L1000 | 12 | 16 | LINCS 16; transcriptomic total 20 |
| PubChem Compound identity | 10 | 14 | identity 14 |
| GEO supplemental metadata | 6 | 8 | GEO 8; transcriptomic total 20 |
| Supporting metadata | 4 | 4 | supporting 4 |
| PubChem BioAssay, each requested modality | 5 | 8 | PubChem BioAssay 24 |
| Tox21, each requested modality | 2 | 3 | Tox21 9 |
| ToxCast, each requested modality | 3 | 3 | ToxCast 9 |

For binding, agonism, and antagonism this reserves 62 requests and leaves an
18-request shared remainder. Expansion cannot consume another pending task's
reservation within a provider, source-family, or role cap. Unused allocations
return to the shared pool only after a deterministic terminal outcome.
Binding, agonism, and antagonism retain distinct task IDs, observations, and
coverage results.

Execution is ordered into four joinability-first phases:

1. validate local/provider prerequisites and establish LINCS readiness;
2. perform shallow, separately bounded activity discovery across modality and
   provider families;
3. resolve stable identities and supplemental transcriptomic metadata;
4. spend the remainder on evidence that can improve a registered join, then
   compute coverage deterministically.

Paginated outputs persist per-page request count, raw and normalized row
counts, provider-unique records, new stable identifiers, new compact
candidates, duplicate count, joinability contribution, and continuation
token. Two consecutive pages with neither a new compact candidate nor a new
stable identifier stop pagination before the task ceiling. Cancellation,
deadline, missing prerequisite, allocation exhaustion, and global budget
exhaustion also stop before another transport request.

### ToxCast prerequisite

Bounded discovery never downloads the multi-gigabyte invitroDB archive. The
preflight checks the reviewed v4.3 cache locally and records release,
fingerprints/checksums, cache-only readiness, and remediation. If the cache is
absent, all ToxCast modality tasks terminate as prerequisite-blocked with zero
network requests and release their unused reservations.

After explicit operator approval, the separate preparation command is:

```powershell
.\.venv\Scripts\python.exe scripts\stage_toxcast_public_cache.py `
  --cache-root .endoscan\provider-cache `
  --confirm-large-download
```

The command performs one reviewed large-download attempt, verifies the
authoritative archive, and creates the normalized local cache. It is not
called by tests or discovery.

### Scientific success gate

Retrieval, normalization, stable-ID bridging, and coverage remain
deterministic. The model may synthesize a typed proposal only from verified
artifacts; it does not calculate joins or invent identifiers. A proposal is
reviewable only with non-zero stable overlap and expected rows, one explicit
activity modality, an explicit transcriptomic source, biological context,
dose/time evidence or a bounded approved context, and an expression
availability or approvable extraction path. Zero-row proposals do not advance
to human strategy review.
