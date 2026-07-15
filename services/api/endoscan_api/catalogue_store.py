"""Validated read-only store for the versioned real measured-signature catalogue."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .schemas import (
    CatalogueCompound,
    CatalogueSignatureDetail,
    CatalogueSignatureSummary,
    CatalogueSourceBlock,
)

CATALOGUE_RELPATH = Path("data/catalogue/v1/catalogue.json")
SIGNATURE_ROOT = Path("apps/web/src/demo-signatures")


class CatalogueNotFoundError(LookupError):
    """A compound or measured signature is absent from the committed catalogue."""


class CatalogueCorruptError(RuntimeError):
    """The committed catalogue or one of its referenced signatures is invalid."""


class _SignatureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signature_id: str
    signature_path: str
    dataset: str
    accession: str
    processing_level: str
    cell_lines: list[str]
    dose: str | None
    timepoint: str | None
    aggregation: str
    n_genes: int


class _CompoundRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compound_id: str
    preferred_name: str
    aliases: list[str]
    pubchem_cid: int
    iupac_name: str | None
    canonical_smiles: str | None
    isomeric_smiles: str | None
    signatures: list[_SignatureRecord]


class _CatalogueDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    catalogue_version: str
    sources: dict[str, CatalogueSourceBlock]
    compounds: list[_CompoundRecord]


def _signature_path(repo_root: Path, relative: str) -> Path:
    allowed_root = (repo_root / SIGNATURE_ROOT).resolve()
    path = (repo_root / relative).resolve()
    if not path.is_relative_to(allowed_root):
        raise CatalogueCorruptError("catalogue signature path escaped the curated signature root")
    if not path.is_file():
        raise CatalogueCorruptError("catalogue references a missing measured signature")
    return path


def _signature_payload(repo_root: Path, relative: str) -> dict:
    try:
        payload = json.loads(_signature_path(repo_root, relative).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogueCorruptError("the measured signature could not be loaded") from exc
    if not isinstance(payload, dict):
        raise CatalogueCorruptError("the measured signature payload is invalid")
    return payload


@lru_cache(maxsize=4)
def load_catalogue(repo_root: Path) -> _CatalogueDoc:
    try:
        raw = json.loads((repo_root / CATALOGUE_RELPATH).read_text(encoding="utf-8"))
        doc = _CatalogueDoc.model_validate(raw)
    except (OSError, ValueError) as exc:
        raise CatalogueCorruptError("the committed catalogue could not be loaded") from exc

    compound_ids: set[str] = set()
    signature_ids: set[str] = set()
    for compound in doc.compounds:
        if compound.compound_id in compound_ids or not compound.signatures:
            raise CatalogueCorruptError("catalogue compound ids must be unique and measured")
        compound_ids.add(compound.compound_id)
        for record in compound.signatures:
            if record.signature_id in signature_ids:
                raise CatalogueCorruptError("catalogue signature ids must be unique")
            signature_ids.add(record.signature_id)
            payload = _signature_payload(repo_root, record.signature_path)
            signature = payload.get("signature")
            provenance = payload.get("provenance", "")
            if (
                not isinstance(signature, dict)
                or len(signature) != record.n_genes
                or compound.compound_id not in provenance
                or "REAL measured" not in provenance
            ):
                raise CatalogueCorruptError(
                    "catalogue signature provenance or gene count is invalid"
                )
    return doc


def _summary(compound: _CompoundRecord, record: _SignatureRecord) -> CatalogueSignatureSummary:
    return CatalogueSignatureSummary(
        signature_id=record.signature_id,
        compound_id=compound.compound_id,
        compound_name=compound.preferred_name,
        dataset=record.dataset,
        accession=record.accession,
        processing_level=record.processing_level,
        cell_lines=record.cell_lines,
        dose=record.dose,
        timepoint=record.timepoint,
        aggregation=record.aggregation,
        n_genes=record.n_genes,
    )


def _compound(compound: _CompoundRecord) -> CatalogueCompound:
    return CatalogueCompound(
        compound_id=compound.compound_id,
        preferred_name=compound.preferred_name,
        aliases=compound.aliases,
        pubchem_cid=compound.pubchem_cid,
        iupac_name=compound.iupac_name,
        canonical_smiles=compound.canonical_smiles,
        isomeric_smiles=compound.isomeric_smiles,
        signatures=[_summary(compound, record) for record in compound.signatures],
    )


def search_compounds(
    repo_root: Path, query: str, limit: int
) -> tuple[_CatalogueDoc, list[CatalogueCompound]]:
    doc = load_catalogue(repo_root.resolve())
    needle = query.strip().casefold()
    ranked: list[tuple[int, str, _CompoundRecord]] = []
    for compound in doc.compounds:
        terms = [
            compound.preferred_name,
            compound.compound_id,
            str(compound.pubchem_cid),
            f"cid {compound.pubchem_cid}",
            compound.iupac_name or "",
            *compound.aliases,
        ]
        normalized = [term.casefold() for term in terms]
        if needle in normalized:
            rank = 0
        elif any(term.startswith(needle) for term in normalized):
            rank = 1
        elif any(needle in term for term in normalized):
            rank = 2
        else:
            continue
        ranked.append((rank, compound.preferred_name.casefold(), compound))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return doc, [_compound(item[2]) for item in ranked[:limit]]


def get_compound(repo_root: Path, compound_id: str) -> tuple[_CatalogueDoc, CatalogueCompound]:
    doc = load_catalogue(repo_root.resolve())
    for compound in doc.compounds:
        if compound.compound_id == compound_id:
            return doc, _compound(compound)
    raise CatalogueNotFoundError("compound is not in the measured-signature catalogue")


def get_signature(repo_root: Path, signature_id: str) -> CatalogueSignatureDetail:
    doc = load_catalogue(repo_root.resolve())
    source_url = doc.sources["signatures"].url
    for compound in doc.compounds:
        for record in compound.signatures:
            if record.signature_id != signature_id:
                continue
            payload = _signature_payload(repo_root, record.signature_path)
            summary = _summary(compound, record)
            return CatalogueSignatureDetail(
                **summary.model_dump(),
                schema_version=doc.schema_version,
                catalogue_version=doc.catalogue_version,
                provenance=payload["provenance"],
                source_url=source_url,
                signature=payload["signature"],
            )
    raise CatalogueNotFoundError("measured signature is not in the catalogue")
