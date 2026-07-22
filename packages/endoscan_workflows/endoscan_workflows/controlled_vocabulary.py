"""Reviewed controlled vocabularies used at bounded discovery tool boundaries."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum, StrEnum
from types import MappingProxyType

CONTROLLED_VOCABULARY_POLICY_VERSION = "controlled-vocabulary-v1"


class VocabularyFieldCategory(str, Enum):
    """Audit classification for values crossing a bounded discovery model boundary."""

    EXACT_MACHINE_IDENTIFIER = "A"
    HUMAN_CONTROLLED_VOCABULARY = "B"
    FREE_SCIENTIFIC_TEXT = "C"


class GeoStudyType(StrEnum):
    """GEO study types supported by the bounded deterministic search client."""

    EXPRESSION_PROFILING_BY_ARRAY = "Expression profiling by array"
    EXPRESSION_PROFILING_BY_HIGH_THROUGHPUT_SEQUENCING = (
        "Expression profiling by high throughput sequencing"
    )


CONTROLLED_VOCABULARY_AUDIT = MappingProxyType(
    {
        "organism_alternatives": VocabularyFieldCategory.HUMAN_CONTROLLED_VOCABULARY,
        "study_type_alternatives": VocabularyFieldCategory.HUMAN_CONTROLLED_VOCABULARY,
        "recommendation_status": VocabularyFieldCategory.EXACT_MACHINE_IDENTIFIER,
        "validation_status": VocabularyFieldCategory.EXACT_MACHINE_IDENTIFIER,
        "run_mode": VocabularyFieldCategory.EXACT_MACHINE_IDENTIFIER,
        "candidate_status": VocabularyFieldCategory.EXACT_MACHINE_IDENTIFIER,
        "confidence_category": VocabularyFieldCategory.EXACT_MACHINE_IDENTIFIER,
        "scientific_terms": VocabularyFieldCategory.FREE_SCIENTIFIC_TEXT,
        "cell_tissue_terms": VocabularyFieldCategory.FREE_SCIENTIFIC_TEXT,
        "treatment_terms": VocabularyFieldCategory.FREE_SCIENTIFIC_TEXT,
    }
)


def _comparison_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    return re.sub(r"\s+", " ", normalized).casefold()


GEO_STUDY_TYPE_ALIASES = MappingProxyType(
    {
        _comparison_key(GeoStudyType.EXPRESSION_PROFILING_BY_ARRAY.value): (
            GeoStudyType.EXPRESSION_PROFILING_BY_ARRAY
        ),
        _comparison_key("array"): GeoStudyType.EXPRESSION_PROFILING_BY_ARRAY,
        _comparison_key(
            GeoStudyType.EXPRESSION_PROFILING_BY_HIGH_THROUGHPUT_SEQUENCING.value
        ): GeoStudyType.EXPRESSION_PROFILING_BY_HIGH_THROUGHPUT_SEQUENCING,
        _comparison_key("high throughput sequencing"): (
            GeoStudyType.EXPRESSION_PROFILING_BY_HIGH_THROUGHPUT_SEQUENCING
        ),
        _comparison_key("sequencing"): (
            GeoStudyType.EXPRESSION_PROFILING_BY_HIGH_THROUGHPUT_SEQUENCING
        ),
    }
)


@dataclass(frozen=True)
class ControlledVocabularyMatch:
    original: str
    comparison_value: str
    canonical: str | None


def canonicalize_geo_study_type(value: str) -> ControlledVocabularyMatch:
    """Resolve only explicitly reviewed aliases; unknown values remain unresolved."""

    comparison_value = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).strip())
    resolved = GEO_STUDY_TYPE_ALIASES.get(comparison_value.casefold())
    return ControlledVocabularyMatch(
        original=value,
        comparison_value=comparison_value,
        canonical=resolved.value if resolved is not None else None,
    )
