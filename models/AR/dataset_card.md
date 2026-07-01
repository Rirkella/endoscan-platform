# Dataset Card — AR

## Target
- **Biological target:** AR
- **Input type:** transcriptomics

## Sources
compara, lincs

## Composition
- **Compounds (total / per class):** overlap=131, compounds_per_class={0: 94, 1: 37}, signatures_per_class={0: 94, 1: 37}
- **Label ∩ signature overlap:** labeled=1720, with_signature=140, overlap=131
- **Duplicate signatures:** 0.000
- **Label conflicts / confidence:** conflicts=0 (rate=0.000); confidence={'min': 0.8, 'mean': 0.8, 'max': 0.8}
- **Metadata coverage:** 1.000

## Splits
- **Strategy:** compound-level grouping (no compound spans split groups)
- **Plan:** compound-level, n_groups=10, feasible=True, leakage=False

## Known limitations
Constructed from a curated allow-list of sources; not a regulatory dataset. Conflicted-label compounds are excluded. Coverage and class balance are limited by source overlap.
