"""Pydantic v2 schema for the EndoScan registry.

The registry is THE contract between dataset construction, training, and serving
(PROJECT_RULES.md §2.4). This module defines the typed entry shape and the
status lifecycle. Persistence lives in `store.py`.

Design notes
------------
- `input_type` is constrained to ``"transcriptomics"`` at the schema level to
  enforce EndoScan's transcriptomics-first rule (PROJECT_RULES.md §1, §4.3).
- Artifact paths are stored **relative to the repository root** (see `store.py`
  for why and how they are resolved).
- `source_refs` are **opaque strings** at M1; the allow-list (`sources.yaml`)
  does not exist until M2, so no structure or lookup is imposed here.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: An endpoint id is an uppercase symbolic token (e.g. "ER", "TR", "DEMO_ER").
#: It is NOT a compound identifier (CID/InChIKey/SMILES) — those appear in M2
#: dataset fixtures, not in the registry index.
ENDPOINT_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


class EndpointStatus(str, Enum):
    """Lifecycle of a registry entry.

    Flow: ``candidate`` → ``dataset_ready`` → ``under_review`` → one terminal
    state of ``validated_mvp`` / ``experimental`` / ``failed_qc`` /
    ``deprecated``. The lifecycle encodes the human-in-the-loop gate
    (PROJECT_RULES.md §3): reaching ``validated_mvp`` requires that every
    referenced artifact exists (enforced in `store.py`).
    """

    candidate = "candidate"
    dataset_ready = "dataset_ready"
    under_review = "under_review"
    validated_mvp = "validated_mvp"
    experimental = "experimental"
    failed_qc = "failed_qc"
    deprecated = "deprecated"


class EndpointEntry(BaseModel):
    """A single registered endpoint — the registry's unit of record."""

    # extra="forbid": unknown fields are rejected (the contract is explicit).
    # protected_namespaces=(): allow `model_*` field names without pydantic warnings.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    endpoint_id: str
    biological_target: str
    input_type: Literal["transcriptomics"] = "transcriptomics"
    model_path: str
    feature_schema_path: str
    metrics_path: str
    explainer_path: str | None = None
    model_card_path: str
    dataset_card_path: str
    status: EndpointStatus = EndpointStatus.candidate
    version: str
    created_at: datetime
    source_refs: list[str] = Field(default_factory=list)

    @field_validator("endpoint_id")
    @classmethod
    def _validate_endpoint_id(cls, value: str) -> str:
        if not ENDPOINT_ID_PATTERN.fullmatch(value):
            raise ValueError(
                f"endpoint_id must match {ENDPOINT_ID_PATTERN.pattern!r} "
                f"(uppercase token), got {value!r}"
            )
        return value

    def artifact_paths(self) -> list[str]:
        """All referenced artifact paths (omitting the optional explainer when unset)."""
        paths = [
            self.model_path,
            self.feature_schema_path,
            self.metrics_path,
            self.model_card_path,
            self.dataset_card_path,
        ]
        if self.explainer_path is not None:
            paths.append(self.explainer_path)
        return paths


class EndpointIndex(BaseModel):
    """On-disk shape of ``registry/models/endpoints.json``."""

    model_config = ConfigDict(extra="forbid")

    endpoints: list[EndpointEntry] = Field(default_factory=list)
