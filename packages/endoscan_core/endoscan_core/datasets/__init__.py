"""EndoScan dataset-construction toolchain.

A set of deterministic, individually-tested data-engineering tools that turn an
approved allow-list of sources into a candidate training table, a quality report,
a dataset card, and a quality-gate verdict — all offline on fixtures at M2.

No training, registration, inference, or agent orchestration lives here. The
quality gate emits a verdict only; it never triggers training.
"""

from .adapters import (
    FixtureSourceAdapter,
    RawTable,
    RealDownloadAdapter,
    SourceAdapter,
    StagedSourceAdapter,
)
from .candidate_table import CandidateTable, SplitPlan, candidate_table_builder
from .compound_mapper import (
    CompoundMapping,
    MappingConflict,
    MappingResult,
    compound_mapper,
)
from .label_retriever import LabelConflict, LabelRecord, LabelSet, label_retriever
from .overlap import OverlapResult, overlap_computer
from .quality_gates import (
    GateCheck,
    GateThresholds,
    GateVerdict,
    load_gates,
    quality_gates,
)
from .quality_report import DatasetQualityReport, dataset_quality_report
from .signature_retriever import (
    SignatureMetadata,
    SignatureRecord,
    SignatureSet,
    signature_retriever,
)
from .source_selector import source_selector
from .sources import (
    Locator,
    SourceEntry,
    SourcesAllowList,
    SourceType,
    UnregisteredSourceError,
    is_allowed,
    load_sources,
    require_allowed,
)

__all__ = [
    # sources + adapters
    "SourceEntry",
    "SourcesAllowList",
    "SourceType",
    "Locator",
    "UnregisteredSourceError",
    "is_allowed",
    "load_sources",
    "require_allowed",
    "SourceAdapter",
    "RawTable",
    "FixtureSourceAdapter",
    "StagedSourceAdapter",
    "RealDownloadAdapter",
    # tools
    "source_selector",
    "label_retriever",
    "signature_retriever",
    "compound_mapper",
    "overlap_computer",
    "candidate_table_builder",
    "dataset_quality_report",
    "load_gates",
    "quality_gates",
    # types
    "LabelRecord",
    "LabelConflict",
    "LabelSet",
    "SignatureRecord",
    "SignatureMetadata",
    "SignatureSet",
    "CompoundMapping",
    "MappingConflict",
    "MappingResult",
    "OverlapResult",
    "CandidateTable",
    "SplitPlan",
    "DatasetQualityReport",
    "GateThresholds",
    "GateCheck",
    "GateVerdict",
]
