# Model Selection Report

- **Selected model:** random_forest
- **Tolerance (primary metrics):** 0.02
- **Rule:** Among candidates within `tolerance` of the best AUROC and AUPRC, prefer the lowest (interpretability_tier, simplicity_tier). Measured cost is reported but not used in the automated tie-break. Final pick is human-approved.

| model | AUROC | AUPRC | bal_acc | Brier | AUROC_fold_std | interp_tier | simplicity_tier |
|---|---|---|---|---|---|---|---|
| gradient_boosting | 0.755 | 0.307 | 0.574 | 0.066 | 0.052 | 3 | 3 |
| random_forest | 0.751 | 0.313 | 0.556 | 0.067 | 0.072 | 3 | 2 |
| lasso_logreg | 0.696 | 0.268 | 0.602 | 0.092 | 0.066 | 1 | 1 |
| elastic_net_logreg | 0.688 | 0.262 | 0.608 | 0.090 | 0.060 | 1 | 1 |
| ridge_logreg | 0.677 | 0.258 | 0.619 | 0.103 | 0.044 | 1 | 1 |

Nested per-fold selections: ['random_forest', 'lasso_logreg', 'random_forest', 'random_forest', 'gradient_boosting']
