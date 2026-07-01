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

## Provenance & reproduction
The original model binary was unavailable (DVC artifact lost). The model was
deterministically regenerated from source (committed CERAPP experimental-functional labels
+ LINCS MCF7/A549 signatures re-extracted from the gctx, seed 0) through the reproducible
gated pipeline. The regenerated binary is byte-identical to the original
(md5 5bed2e0de503dbec65992db1e498212e), confirming faithful reproduction. Confidence-interval
/ evidence blocks were added under the strengthened status logic; point metrics are unchanged
from the original record.

## Metrics
AUROC=0.740, AUPRC=0.256, balanced_acc=0.579, F1=0.250, Brier=0.070 (honest nested estimate)

## Recommended use
Research prioritization of estrogen-receptor activity from signatures.

## NOT recommended / out of scope
Any regulatory, clinical, or diagnostic decision.

## Limitations & Disclaimer
**Status — experimental:** a real-data methodology demonstration, NOT a validated predictor. Unmet validated_mvp criteria: auroc 95% CI lower bound 0.675 < 0.75 floor; auprc 95% CI lower bound 0.194 < 0.50 floor; balanced accuracy 95% CI lower bound 0.537 < 0.65 floor.

**Statistical power:** 73 positives / 959 compounds (prevalence 0.076). Only ~15 positives per held-out fold (n_pos / 5).

**Model-selection stability:** family selection was UNSTABLE across folds (per-fold picks: ['random_forest', 'lasso_logreg', 'random_forest', 'random_forest', 'gradient_boosting']); the registered model (random_forest) is the automated simplicity-aware scorecard pick on all data.

**Coverage:** built from the intersection of approved labels and transcriptomic signatures (compound-level); it is coverage-limited and NOT representative of the full chemical space.

**Scope of claim:** ER functional modulation (agonist or antagonist transactivation; direction not distinguished); not a physical-binding, pan-endocrine, or regulatory/clinical/diagnostic claim.

Pre-screening / prioritization only; metrics are the honest compound-level cross-validated estimate, NOT regulatory-grade validation, and must be confirmed experimentally.

This model has **not** undergone regulatory-grade validation. Predictions are
hypotheses for prioritization only and must be confirmed experimentally.
