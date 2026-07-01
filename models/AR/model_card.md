# Model Card — AR

> EndoScan is a **pre-screening / prioritization** tool, **not** a regulatory,
> clinical, or diagnostic instrument. See *Limitations & Disclaimer* below.

## Endpoint
- **Endpoint ID:** AR
- **Biological target:** Androgen Receptor
- **Input type:** transcriptomics (transcriptomic signature → endocrine risk)
- **Version:** 0.1.0
- **Status:** experimental

## Data
- **Sources:** compara, lincs, pubchem
- **Compounds (total / per class):** overlap=131, compounds_per_class={0: 94, 1: 37}
- **Transcriptomic features:** 978 landmark genes
- **Train/test split:** compound-level (no leakage)

## Model
- **Model type:** ridge_logreg
- **Training summary:** compound-level nested evaluation; model chosen by a simplicity-aware scorecard over 5 sklearn models

## Metrics
AUROC=0.768, AUPRC=0.524, balanced_acc=0.731, F1=0.613, Brier=0.191 (honest nested estimate)

## Recommended use
Research prioritization of androgen-receptor functional modulation from transcriptomic signatures.

## NOT recommended / out of scope
Any regulatory, clinical, or diagnostic decision.

## Limitations & Disclaimer
**Status — experimental:** a real-data methodology demonstration, NOT a validated predictor. Unmet validated_mvp criteria: auroc 95% CI lower bound 0.678 < 0.75 floor; auprc 95% CI lower bound 0.420 < 0.50 floor; balanced accuracy 95% CI lower bound 0.642 < 0.65 floor; brier score 95% CI upper bound 0.243 > 0.20 ceiling.

**Statistical power:** 37 positives / 131 compounds (prevalence 0.282). Only ~7 positives per held-out fold (n_pos / 5).

**Model-selection stability:** selection was consistent across folds (ridge_logreg); chosen by the automated simplicity-aware scorecard.

**Coverage:** built from the intersection of approved labels and transcriptomic signatures (compound-level); it is coverage-limited and NOT representative of the full chemical space.

**Scope of claim:** AR functional modulation (agonist or antagonist transactivation; binding EXCLUDED; direction not distinguished) in the androgen-responsive VCaP/LINCS context; not a physical-binding, pan-endocrine, or regulatory/clinical/diagnostic claim.

Pre-screening / prioritization only; metrics are the honest compound-level cross-validated estimate, NOT regulatory-grade validation, and must be confirmed experimentally.

This model has **not** undergone regulatory-grade validation. Predictions are
hypotheses for prioritization only and must be confirmed experimentally.
