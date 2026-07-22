# LINCS metadata-only live smoke plan

This plan is intentionally **not executed in Phase 2A implementation or tests**. It is
the exact operator gate for a later separately authorized live smoke against the reviewed
GSE92742 release manifest.

## Preconditions

- Use a new empty technical workflow SQLite database and artifact directory.
- Configure `ENDOSCAN_SOURCE_MAX_RESPONSE_BYTES=450000000`.
- Keep the provider retry policy at one deterministic transient transport retry, but abort
  the smoke if any retry is used; the success case must make exactly four HTTPS GETs.
- Do not configure or call an LLM provider.
- Record the checked-out commit and tree before execution.

## Exact requests

Run `retrieve_lincs_metadata_release` once for release
`lincs-gse92742-phase1-2017`. The reviewed manifest must issue one GET for each of:

1. `GSE92742_Broad_LINCS_pert_info.txt.gz` — maximum 100,000,000 bytes;
2. `GSE92742_Broad_LINCS_sig_info.txt.gz` — maximum 450,000,000 bytes;
3. `GSE92742_Broad_LINCS_cell_info.txt.gz` — maximum 10,000,000 bytes;
4. `GSE92742_Broad_LINCS_gene_info.txt.gz` — maximum 25,000,000 bytes.

The total declared upper bound is 585,000,000 bytes. The Level-5 GCTX locator must not
appear in the retrieval task, transport log, cache, or artifact inventory.

## Pass criteria

- four logical manifest items and exactly four scientific-source/transport requests;
- HTTPS final hosts remain `ftp.ncbi.nlm.nih.gov` with only allowlisted redirects;
- all four responses satisfy the reviewed MIME and per-file size policies;
- all raw gzip payloads are immutable content-addressed artifacts;
- cache rows reference those exact artifacts and hashes;
- source release, locator, retrieval time, licence/provenance, and any published checksum
  are retained;
- required columns parse, while invalid or incomplete rows remain present and classified;
- completion proof reconciles all four mandatory resources before task completion;
- a second identical invocation is four cache hits and zero external GETs;
- compound/signature/context coverage completes from metadata only;
- no expression value is read, no GCTX file is opened, and no build advances to ingestion,
  assembly, curation, or training.

Any missing file, checksum mismatch, parser error, timeout, disallowed redirect, MIME
mismatch, oversized response, or retry makes the smoke fail. Persist the partial ledger
and already verified resources; do not label the task completed and do not repeat verified
requests on the next explicitly authorized resume.
