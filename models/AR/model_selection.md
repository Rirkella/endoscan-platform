# Model Selection Report

- **Selected model:** ridge_logreg
- **Tolerance (primary metrics):** 0.02
- **Rule:** Among candidates within `tolerance` of the best AUROC and AUPRC, prefer the lowest (interpretability_tier, simplicity_tier). Measured cost is reported but not used in the automated tie-break. Final pick is human-approved.

| model | AUROC | AUPRC | bal_acc | Brier | AUROC_fold_std | interp_tier | simplicity_tier |
|---|---|---|---|---|---|---|---|
| ridge_logreg | 0.711 | 0.494 | 0.645 | 0.220 | 0.096 | 1 | 1 |
| elastic_net_logreg | 0.710 | 0.467 | 0.644 | 0.203 | 0.058 | 1 | 1 |
| lasso_logreg | 0.708 | 0.460 | 0.636 | 0.205 | 0.044 | 1 | 1 |
| random_forest | 0.684 | 0.458 | 0.574 | 0.197 | 0.076 | 3 | 2 |
| gradient_boosting | 0.607 | 0.452 | 0.561 | 0.297 | 0.105 | 3 | 3 |

Nested per-fold selections: ['ridge_logreg', 'ridge_logreg', 'ridge_logreg', 'ridge_logreg', 'ridge_logreg']
