# Scientific Limitations

## Intended interpretation

EndoScan is a research demonstrator for organizing evidence and constructing candidate endpoint models. It is not a clinical device, regulatory submission system, environmental-release decision tool, or substitute for expert toxicology review. A score is not proof of harm, safety, mechanism, or causality.

## Evidence limitations

- Public assay and transcriptomic sources differ in scope, curation, release cadence, identifier quality, and access mechanics.
- Search hits are not verified evidence. Hydration and obtainability checks can expose missing target, modality, dose, time, biological model, labels, or compound IDs.
- Assay binding, agonism, antagonism, activation, inhibition, and counter-screen outcomes are not interchangeable.
- Transcriptomic responses depend on cell/tissue, dose, duration, platform, processing, and feature space.
- Compound names can be ambiguous; salts, stereochemistry, mixtures, metabolites, and formulations require explicit treatment.
- Absence from a source is not evidence of inactivity.

## Endpoint and label limitations

Discovery retains assay modalities separately. Binding cannot be silently interpreted as functional activity. A derived union—such as agonism OR antagonism—requires a versioned `ModalityAggregationPolicy`, scientific rationale, conflict and missingness rules, and human approval. Original observations remain available.

Thresholds, inactive definitions, inconclusive handling, replicate aggregation, and conflict resolution materially affect class balance and prediction claims. The builder can make those choices auditable; it cannot make them universally correct.

## Dataset limitations

Joinability depends on verified identifiers across activity and transcriptomic records. Overlap can be small and biased toward well-studied compounds. Multiple profiles per compound create dependence; random row splits can leak chemical or biological context. Coverage and preview reports are necessary but do not eliminate selection bias or measurement error.

## Model limitations

Current AR and ER registry entries are experimental serving artifacts. Repository tests establish software behavior, schema compatibility, and deterministic fixture results—not scientific performance. Metrics on a benchmark are conditional on label construction, split design, feature coverage, sample size, and hyperparameter search. Applicability-domain and uncertainty outputs are diagnostic signals, not guarantees.

Feature attribution explains the fitted model locally; it does not establish a molecular mechanism. Pathway enrichment is sensitive to gene-set version, ranking, background universe, and multiple testing.

## Offline and live evidence

Offline TR and DNA-damage fixtures are prepared scenarios used to validate workflow mechanics. They must not be cited as discovered datasets or validated endpoints. Selected provider operations have bounded technical live-smoke evidence, but complete live broad-TR discovery and scientific assembly remain unvalidated. A technical HTTP/parser success does not validate source suitability.

## Required scientific review

Before any external use, qualified reviewers must examine endpoint definition, source release and licensing, assay roles, identity mappings, label policy, exclusions, class balance, joinability, leakage, splits, metrics, calibration, uncertainty, applicability, failure modes, and model/data cards. Independent validation on appropriate held-out evidence remains required.

## Future structure-input extension

A planned extension would accept a machine-readable chemical structure, use a separate model to predict a likely cellular-response profile, and then analyze that predicted profile with endpoint models. Such results would be clearly marked as **predicted**, never measured. They add another model and uncertainty boundary and therefore carry lower evidence than an observed cellular response; they cannot be presented as equivalent inputs.
