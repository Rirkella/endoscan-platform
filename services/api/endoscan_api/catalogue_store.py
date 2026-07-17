"""Validated read-only store for the versioned real measured-signature catalogue."""

from __future__ import annotations

import csv
import hashlib
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
IDENTITIES_RELPATH = Path("data/identities/v1/reference_identities.json")
SOURCE_OVERRIDES_RELPATH = Path("data/identities/v1/source_identity_overrides.json")
CERAPP_RELPATH = Path("data/staged/er/cerapp.csv")
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


class _ReferenceIdentityRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inchikey: str
    preferred_name: str | None
    pubchem_cid: int | None
    iupac_name: str | None
    synonyms: list[str]
    canonical_smiles: str | None
    isomeric_smiles: str | None
    resolution_status: str
    failure_category: str | None
    retrieved_at: str
    source: str


class _ReferenceIdentityDoc(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str
    artifact_version: str
    statistics: dict
    records_sha256: str
    identities: list[_ReferenceIdentityRecord]


@lru_cache(maxsize=4)
def load_reference_identities(repo_root: Path) -> _ReferenceIdentityDoc:
    try:
        raw = json.loads((repo_root / IDENTITIES_RELPATH).read_text(encoding="utf-8"))
        doc = _ReferenceIdentityDoc.model_validate(raw)
    except (OSError, ValueError) as exc:
        raise CatalogueCorruptError("the reference identity artifact could not be loaded") from exc
    canonical = json.dumps(
        [item.model_dump() for item in doc.identities],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if hashlib.sha256(canonical).hexdigest() != doc.records_sha256:
        raise CatalogueCorruptError("the reference identity artifact checksum did not match")
    return doc


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


def _reference_contexts(repo_root: Path) -> dict[str, list[str]]:
    contexts: dict[str, list[str]] = {}
    models_root = repo_root / "models"
    if not models_root.is_dir():
        return contexts
    for manifest_path in models_root.glob("*/explore/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            context = manifest_path.parent.parent.name
            for compound_id in manifest.get("compound_ids", []):
                contexts.setdefault(str(compound_id), []).append(context)
        except (OSError, ValueError, TypeError):
            continue
    return contexts


def _availability(reference_contexts: list[str], measured: CatalogueCompound | None) -> str:
    if measured and measured.signatures:
        return "Reference profile available"
    if reference_contexts:
        return "Reference profile available"
    return "Identity known — no compatible measured signature"


@lru_cache(maxsize=4)
def unified_identity_index(repo_root: Path) -> dict[str, CatalogueCompound]:
    """Canonical search index shared by catalogue search and reference-map identity joins."""
    root = repo_root.resolve()
    measured = (
        {compound.compound_id: _compound(compound) for compound in load_catalogue(root).compounds}
        if (root / CATALOGUE_RELPATH).is_file()
        else {}
    )
    contexts = _reference_contexts(root)
    output: dict[str, CatalogueCompound] = {}

    if (root / IDENTITIES_RELPATH).is_file():
        for item in load_reference_identities(root).identities:
            linked = measured.get(item.inchikey)
            resolved = item.resolution_status == "resolved" and item.pubchem_cid is not None
            reference_contexts = sorted(contexts.get(item.inchikey, []))
            output[item.inchikey] = CatalogueCompound(
                compound_id=item.inchikey,
                preferred_name=(
                    item.preferred_name
                    or item.iupac_name
                    or (f"PubChem CID {item.pubchem_cid}" if item.pubchem_cid else item.inchikey)
                ),
                aliases=item.synonyms,
                pubchem_cid=item.pubchem_cid,
                iupac_name=item.iupac_name,
                canonical_smiles=item.canonical_smiles,
                isomeric_smiles=item.isomeric_smiles,
                signatures=linked.signatures if linked else [],
                availability_status=(
                    _availability(reference_contexts, linked) if resolved else "Identity unresolved"
                ),
                availability_reason=(
                    None
                    if resolved
                    else (
                        "The reference profile exists, but no reviewed public compound identity "
                        "was resolved."
                    )
                ),
                reference_contexts=reference_contexts,
            )

    # Source records make the search domain wider than the four curated examples. They never
    # manufacture a transcriptomic vector: only support-manifest membership grants Analyze.
    cerapp_path = root / CERAPP_RELPATH
    if cerapp_path.is_file():
        with cerapp_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                inchikey = str(row.get("inchikey") or "").strip().upper()
                source_id = str(row.get("casrn") or "").strip()
                if not inchikey:
                    continue
                existing = output.get(inchikey)
                if existing is not None:
                    if source_id and source_id not in existing.source_compound_ids:
                        existing.source_compound_ids.append(source_id)
                    continue
                output[inchikey] = CatalogueCompound(
                    compound_id=inchikey,
                    preferred_name=inchikey,
                    aliases=[source_id] if source_id else [],
                    pubchem_cid=None,
                    signatures=[],
                    availability_status="Identity unresolved",
                    availability_reason=(
                        "Present in the CERAPP endpoint source data, but no reviewed public "
                        "identity "
                        "or compatible committed LINCS support profile is available."
                    ),
                    source_compound_ids=[source_id] if source_id else [],
                )

    override_path = root / SOURCE_OVERRIDES_RELPATH
    if override_path.is_file():
        try:
            records = json.loads(override_path.read_text(encoding="utf-8"))["records"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CatalogueCorruptError("the reviewed source identity override is invalid") from exc
        for record in records:
            inchikey = str(record["inchikey"]).upper()
            reference_contexts = sorted(contexts.get(inchikey, []))
            output[inchikey] = CatalogueCompound(
                compound_id=inchikey,
                preferred_name=str(record["preferred_name"]),
                aliases=[str(value) for value in record.get("synonyms", [])],
                pubchem_cid=int(record["pubchem_cid"]),
                iupac_name=record.get("iupac_name"),
                canonical_smiles=record.get("canonical_smiles"),
                isomeric_smiles=record.get("canonical_smiles"),
                signatures=measured.get(inchikey).signatures if inchikey in measured else [],
                availability_status=_availability(reference_contexts, measured.get(inchikey)),
                availability_reason=str(record.get("audit_reason") or "") or None,
                reference_contexts=reference_contexts,
                source_compound_ids=[str(value) for value in record.get("source_compound_ids", [])],
            )

    for inchikey, linked in measured.items():
        output.setdefault(inchikey, linked)
    return output


def search_compounds(
    repo_root: Path, query: str, limit: int
) -> tuple[_CatalogueDoc, list[CatalogueCompound]]:
    root = repo_root.resolve()
    doc = load_catalogue(root)
    needle = query.strip().casefold()
    ranked: list[tuple[int, str, CatalogueCompound]] = []
    for compound in unified_identity_index(root).values():
        terms = [
            compound.preferred_name,
            compound.compound_id,
            str(compound.pubchem_cid or ""),
            f"cid {compound.pubchem_cid}" if compound.pubchem_cid else "",
            compound.iupac_name or "",
            compound.canonical_smiles or "",
            compound.isomeric_smiles or "",
            *compound.aliases,
            *compound.source_compound_ids,
            *compound.lincs_perturbagen_ids,
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
    return doc, [item[2] for item in ranked[:limit]]


def get_compound(repo_root: Path, compound_id: str) -> tuple[_CatalogueDoc, CatalogueCompound]:
    doc = load_catalogue(repo_root.resolve())
    for compound in doc.compounds:
        if compound.compound_id == compound_id:
            return doc, _compound(compound)
    raise CatalogueNotFoundError("compound is not in the measured-signature catalogue")


def identity_index(repo_root: Path) -> dict[str, CatalogueCompound]:
    """Return only committed, versioned identities; callers must not fill gaps from live APIs."""
    return unified_identity_index(repo_root.resolve())


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
