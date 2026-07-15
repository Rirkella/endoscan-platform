# Demo signatures — REAL curated signatures only

These are the bundled "try-it" examples on the Analyze page. **They must be real
curated/staged transcriptomic signatures — never fabricated or randomly generated.**

## Why this directory may be empty

The real LINCS signatures live in `lincs.parquet`, which is DVC-tracked and **not present in a
clean git checkout** (dead remote). So selection is **operator-assisted**, mirroring the ER/AR
model-artifact transfers. Until the operator transfers the selected real signatures, this
directory holds no `*.json` and the Analyze page uses JSON paste only.

## Selection protocol (operator side, where the staged data is available)

ER and AR share the **identical 978-gene `feature_schema.json`**, so one signature is valid input
for both endpoints. Run candidate real signatures through the M7 API (`POST /predict` on **both**
ER and AR) and pick **3–4** with distinct profiles:

- low ER / low AR (little endpoint-consistent signal either way)
- high ER / low AR
- low ER / high AR
- optional 4th: a real both-signal or borderline/ambiguous case **only if one exists**

## File format (`<id>.json`) — validated by `demoSignatures.test.ts`

```json
{
  "id": "lincs-<perturbagen>-<cell>-<dose>-<time>",
  "label": "Demo example — real curated signature, illustrative only",
  "provenance": "LINCS sig_id ... | perturbagen ... | cell MCF7 | 10uM | 24h  (or staged-row id)",
  "expected_profile": { "ER": 0.00, "AR": 0.00 },
  "signature": { "GENE1": 0.0, "…exactly the 978 landmark genes…": 0.0 }
}
```

`expected_profile` records the uncalibrated endpoint scores returned by the API at selection time; the
verify-on-commit test confirms each file has exactly the 978 schema genes (finite values) and
that its recorded profile matches what the API returns (small tolerance).
