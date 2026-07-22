"""EndoScan core science library (framework-free, transcriptomics-first).

Scientific logic shared by the FastAPI service, deterministic jobs, and workflow
tools lives in this package; those callers do not reimplement it. The package
exposes the registry, dataset, feature, training, inference, and diagnostics layers.
"""

__version__ = "0.0.0"
