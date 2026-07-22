# Offline Demo

## What it proves

The offline demonstrations execute the complete endpoint lifecycle with prepared, deterministic fixtures: build creation, specification, discovery plan, candidate/hydrated evidence, coverage, strategy review, deterministic assembly, dataset and training approvals, benchmark evaluation, scientific approval, and registry publication. They prove orchestration, contracts, persistence, artifacts, approvals, replay, and serving integration without external dependencies.

They do **not** prove that a live source contains the same records, that a resulting dataset is biologically valid, or that a model is suitable for safety decisions. Fixture observations are visibly marked as prepared data.

## Run

After [development setup](DEVELOPMENT.md):

```bash
uv run python scripts/project.py demo-tr
uv run python scripts/project.py demo-dna-damage
```

Run both:

```bash
uv run python scripts/project.py demo
```

The direct entry point accepts `tr_receptor` or `dna_damage` and an optional output root:

```bash
uv run python scripts/run_offline_endpoint_demo.py tr_receptor --output-root .builds/offline-endpoint-demo/tr_receptor
```

## Expected result

Each command exits zero, reports the build/endpoint identifiers and final `COMPLETED` state, and writes a local workflow database, immutable artifacts, endpoint registry content, and summary below `.builds/offline-endpoint-demo/`. The directory is ignored by Git. Network and OpenAI request counts are zero.

On a normal developer laptop each compact fixture should finish in well under a minute after dependencies are installed. The expected endpoint identifiers are `OFFLINE_TR_ACTIVITY` and `OFFLINE_DNA_DAMAGE`. Outputs include the specification, discovery/hydration and coverage contracts, strategy and approved recipe, source-like raw artifacts, identifier/assembly artifacts, quality report, Explore payload, dataset manifest/fingerprint, benchmark result, model-card/validation material, and registry entry.

To inspect through the UI, start the API/web against the demo output root using the repository path configuration, then open the build identifier printed by the command in `/admin/endpoints/<build-id>` or the published endpoint in Model Library. The demo does not automatically start or mutate a shared server.

The thyroid-receptor fixture exercises modality-preserving discovery and a reviewed assembly decision. The DNA-damage fixture proves the workflow is not hard-coded to a receptor or a single provider/source shape.

## Review checklist

- Specification and component requirements are present before discovery.
- Candidate and hydrated records retain source and modality provenance.
- Coverage precedes strategy selection.
- Approvals are separate and version-bound.
- Original evidence survives derived labels.
- Dataset and benchmark artifacts have stable hashes.
- Registry publication occurs only after scientific approval.
- Rerunning from the same fixture yields equivalent scientific fingerprints.
- No credential, live database, source download, or output root becomes tracked.

## Cleaning local outputs

Outputs are disposable local runtime state. Remove the chosen `.builds/offline-endpoint-demo/` directory only when no local audit needs it. Never delete shared or preserved emergency backups as part of demo cleanup.
