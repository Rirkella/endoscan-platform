# ADR 0006: Large scientific data remain artifact-backed

## Context

Expression matrices, assay tables, and release archives can be too large for prompts, database rows, or memory-bound processing.

## Decision

Store large payloads as checksum-addressed external artifacts. Pass manifests and bounded summaries through workflows; use dedicated streaming/selective jobs after approval.

## Consequences

Lineage and replay are explicit and model context stays bounded. Backups must cover both database and artifact storage, and missing artifacts are detectable failures.

## Status

Accepted.
