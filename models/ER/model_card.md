# Model Card — ER

> EndoScan is a **pre-screening / prioritization** tool, **not** a regulatory,
> clinical, or diagnostic instrument. See *Limitations & Disclaimer* below.

## Endpoint
- **Endpoint ID:** ER
- **Biological target:** Estrogen Receptor
- **Input type:** transcriptomics (transcriptomic signature → endocrine risk)
- **Version:** 0.1.0
- **Status:** experimental

## Data
- **Sources:** cerapp, lincs, pubchem
- **Compounds (total / per class):** overlap=963, compounds_per_class={0: 886, 1: 73}
- **Transcriptomic features:** 978 landmark genes
- **Train/test split:** compound-level (no leakage)

## Model
- **Model type:** random_forest
- **Training summary:** compound-level nested evaluation; model chosen by a simplicity-aware scorecard over 5 sklearn models, family selection unstable across folds (see Limitations)

## Metrics
AUROC=0.740, AUPRC=0.256, balanced_acc=0.579, F1=0.250, Brier=0.070 (honest nested estimate)

## Recommended use
Research prioritization of estrogen-receptor activity from signatures.

## NOT recommended / out of scope
Any regulatory, clinical, or diagnostic decision.

## Limitations & Disclaimer
**Status — experimental:** a real-data methodology demonstration, NOT a validated predictor. Unmet validated_mvp criteria: balanced accuracy 0.579 < 0.60 floor.

**Statistical power:** 73 positives / 959 compounds (prevalence 0.076). Only ~15 positives per held-out fold (n_pos / 5).

**Model-selection stability:** family selection was UNSTABLE across folds (per-fold picks: ['random_forest', 'lasso_logreg', 'random_forest', 'random_forest', 'gradient_boosting']); the registered model (random_forest) is the automated simplicity-aware scorecard pick on all data.

**Coverage:** built from the intersection of approved labels and transcriptomic signatures (compound-level); it is coverage-limited and NOT representative of the full chemical space.

**Scope of claim:** ER functional modulation (agonist or antagonist transactivation; direction not distinguished); not a physical-binding, pan-endocrine, or regulatory/clinical/diagnostic claim.

Pre-screening / prioritization only; metrics are the honest compound-level cross-validated estimate, NOT regulatory-grade validation, and must be confirmed experimentally.

This model has **not** undergone regulatory-grade validation. Predictions are
hypotheses for prioritization only and must be confirmed experimentally.
