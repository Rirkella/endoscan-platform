# Dataset Card — ER

## Target
- **Biological target:** ER
- **Input type:** transcriptomics

## Sources
cerapp, lincs

## Composition
- **Compounds (total / per class):** overlap=963, compounds_per_class={0: 886, 1: 73}, signatures_per_class={0: 886, 1: 73}
- **Label ∩ signature overlap:** labeled=7171, with_signature=963, overlap=963
- **Duplicate signatures:** 0.000
- **Label conflicts / confidence:** conflicts=4 (rate=0.004); confidence={'min': 0.5, 'mean': 0.5, 'max': 0.5}
- **Metadata coverage:** 1.000

## Splits
- **Strategy:** compound-level grouping (no compound spans split groups)
- **Plan:** compound-level, n_groups=10, feasible=True, leakage=False

## Known limitations
Constructed from a curated allow-list of sources; not a regulatory dataset. Conflicted-label compounds are excluded. Coverage and class balance are limited by source overlap.
