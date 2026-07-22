# ADR 0001: Transcriptomics-first product input

## Context

EndoScan interprets a chemical through the cellular response it produces. Compound identity alone does not encode biological context.

## Decision

The primary analysis signal is a measured or explicitly parsed transcriptomic signature tied to compound, biological model, dose, duration, processing, and provenance. Chemical identity anchors retrieval and joins but does not replace the signature.

## Consequences

Analysis can compare profiles, pathways, and endpoint models on a shared feature contract. Coverage is limited by measured profiles and context; missing signatures remain visible. Inference must not invent expression values.

## Status

Accepted.
