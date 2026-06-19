# Model Card — {{ endpoint_id }}

> EndoScan is a **pre-screening / prioritization** tool, **not** a regulatory,
> clinical, or diagnostic instrument. See *Limitations & Disclaimer* below.

## Endpoint
- **Endpoint ID:** {{ endpoint_id }}
- **Biological target:** {{ biological_target }}
- **Input type:** {{ input_type }} (transcriptomic signature → endocrine risk)
- **Version:** {{ version }}
- **Status:** {{ status }}

## Data
- **Sources:** {{ sources }}
- **Compounds (total / per class):** {{ compound_counts }}
- **Transcriptomic features:** {{ feature_summary }}
- **Train/test split:** compound-level (no leakage)

## Model
- **Model type:** {{ model_type }}
- **Training summary:** {{ training_summary }}

## Metrics
{{ metrics_summary }}

## Recommended use
{{ recommended_use }}

## NOT recommended / out of scope
{{ not_recommended_use }}

## Limitations & Disclaimer
{{ limitations }}

This model has **not** undergone regulatory-grade validation. Predictions are
hypotheses for prioritization only and must be confirmed experimentally.
