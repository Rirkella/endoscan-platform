# ADR 0002: One model per biological-risk endpoint

## Context

Different biological risks use different labels, evidence, applicability domains, and validation criteria. A single opaque “toxicity” score would collapse incompatible claims.

## Decision

Publish separately versioned models for separately defined endpoint claims. A reviewed derived endpoint can aggregate modalities, but its policy, dataset, benchmark, and registry entry are independent and preserve contributing records.

## Consequences

Users can inspect the exact claim and limitations. More endpoints require more governance, data, validation, and registry maintenance. Cross-endpoint summaries cannot substitute for endpoint results.

## Status

Accepted.
