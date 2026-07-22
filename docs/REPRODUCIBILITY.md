# Reproducibility

## Reproducibility contract

EndoScan records enough information to explain and replay a workflow boundary: typed input; contract and producer version; source/release and reviewed locator; budgets; tool/provider configuration; raw response SHA-256; normalized artifact and parent hashes; assembly policy; dataset fingerprint; split/feature configuration; benchmark output; registry manifest; and immutable state events.

Python dependencies are locked in `uv.lock`; frontend dependencies are locked in `apps/web/package-lock.json`. CI uses Python 3.12 and Node 24. The task runner gives contributors and CI the same command graph.

## Determinism boundaries

State transitions, batching, normalization, joins, coverage, assembly, fingerprinting, training configuration, evaluation, and registry serialization are deterministic for fixed inputs. Model-agent proposals and remote sources are not inherently deterministic; their exact outputs are persisted and reused rather than reconstructed from memory. Live source content can change despite stable URLs, so release/version and raw hashes are essential.

## Artifact lineage

Every derived scientific artifact should identify its parents and producer. Raw provider responses are immutable. Cache replay references existing hashes. Dataset manifests enumerate included artifacts and assembly policy. Benchmark manifests identify dataset, splits, features, candidates, metrics, and code/config. Registry manifests identify the approved benchmark and serving files.

## Replay levels

1. **Transport replay** parses the same raw source artifact without network.
2. **Workflow replay** reconstructs normalized observations and downstream deterministic stages from persisted inputs.
3. **Offline fixture replay** executes a complete representative lifecycle with prepared data.
4. **Live revalidation** contacts a source again under new explicit authorization and creates new evidence; it is not a cache replay.

A replay match demonstrates implementation consistency, not source truth, endpoint validity, or model generalization.

## Expected non-determinism

Provider request IDs, latency, live search ordering, mutable source records, and stochastic model training can differ. Where stochastic algorithms are used, seeds and splits must be recorded and repeated-run uncertainty reported. A source update or semantic policy change creates a new version and should never overwrite the previous approved artifact.

## Reproducing the demonstrator

Run locked setup followed by `verify`. The two offline demos must complete with zero network and stable scientific fingerprints. Preserve the output directory only when investigating lineage; it is ignored runtime state.

## Repository data and model assets

Git directly tracks schemas, registry manifests, deterministic fixtures, reviewed catalogue
and identity metadata, the bounded Reactome reference, small ER label/mapping tables, and
the reviewed ER/AR serving artifacts required by the API. The committed `model.pkl` and
Explore support files are published endpoint assets with registry checksums; they are not
outputs of the offline demos.

`data/staged/er/lincs.parquet.dvc` records the omitted LINCS matrix by content hash. The
matrix itself is not in Git, and no DVC remote credential or machine-local remote path is
committed. The current application can serve the reviewed endpoints without pulling it;
the pointer is relevant only to an explicitly authorized reproduction run.

Workflow databases, provider caches, raw source responses, large expression matrices,
scientific downloads, job storage, generated datasets/models, reports, logs, and demo
workspaces are local artifacts. They are ignored and must be regenerated or supplied via a
reviewed artifact channel. Maintainer extraction utilities require explicit local inputs and
never run as part of setup or the offline demo.

## Production gaps

The local demonstrator does not yet provide reproducible container images for API/web, signed releases, software bills of materials, server-grade database backup, object-store retention, or a formal dataset/model card publication pipeline. These are roadmap items, not implicit guarantees.
