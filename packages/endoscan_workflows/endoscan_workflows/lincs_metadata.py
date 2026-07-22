"""Reviewed LINCS metadata retrieval, normalization, exact indexes, and coverage.

This module never reads expression vectors.  Release-specific locators and column
mappings live in the reviewed manifest; all matching and coverage operations consume the
complete metadata universe supplied to them and are deterministic.
"""

from __future__ import annotations

import csv
import gzip
import io
import re
from collections import Counter, defaultdict
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .contracts import ToolAccessPhase, ToolInvocation
from .discovery_strategy import (
    DiscoveryExecutionRecord,
    ImmutableV2Contract,
    deterministic_fingerprint,
)
from .provider_execution import (
    ProviderExecutionOutcome,
    ProviderItemKind,
    ProviderResourceRequest,
    ProviderRetrievalTask,
    ProviderTaskExecutor,
)

LINCS_RELEASE_REGISTRY_VERSION: Literal["1.0.0"] = "1.0.0"
FULL_INCHIKEY = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
MAXIMUM_DECOMPRESSED_METADATA_BYTES = 1_000_000_000
MetadataRole = Literal["pert_info", "sig_info", "cell_info", "gene_info"]
METADATA_ROLES: tuple[MetadataRole, ...] = (
    "pert_info",
    "sig_info",
    "cell_info",
    "gene_info",
)


class LincsReleaseResource(ImmutableV2Contract):
    resource_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,159}$")
    logical_role: Literal["pert_info", "sig_info", "cell_info", "gene_info", "level5_gctx"]
    source_registry_locator_name: str
    public_locator: str = Field(pattern=r"^https://[^\s]+$")
    expected_filename_pattern: str
    compression: Literal["gzip", "none"]
    expected_columns: list[str]
    column_mapping: dict[str, str]
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    maximum_response_bytes: int = Field(ge=1_024, le=25_000_000_000)
    required: bool
    access_phase: ToolAccessPhase
    licence_and_provenance: list[str] = Field(min_length=1, max_length=20)


class LincsReleaseManifest(ImmutableV2Contract):
    release_id: str
    source_registry_id: Literal["lincs"]
    accession: str = Field(pattern=r"^GSE[1-9][0-9]{1,8}$")
    source_release: str
    source_locator: str = Field(pattern=r"^https://[^\s]+$")
    licence_and_provenance: list[str] = Field(min_length=1, max_length=20)
    chemical_perturbation_types: list[str] = Field(min_length=1, max_length=20)
    resources: list[LincsReleaseResource] = Field(min_length=5, max_length=20)
    manifest_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_manifest(self) -> LincsReleaseManifest:
        roles = [item.logical_role for item in self.resources]
        if len(roles) != len(set(roles)):
            raise ValueError("LINCS release resource roles must be unique")
        required = {"pert_info", "sig_info", "cell_info", "gene_info"}
        preapproval = {
            item.logical_role
            for item in self.resources
            if item.access_phase is ToolAccessPhase.PRE_APPROVAL_METADATA and item.required
        }
        if preapproval != required:
            raise ValueError("LINCS manifest must declare all four mandatory metadata roles")
        gctx = next(item for item in self.resources if item.logical_role == "level5_gctx")
        if gctx.access_phase is not ToolAccessPhase.POST_APPROVAL_EXTRACTION or gctx.required:
            raise ValueError("Level-5 GCTX must remain optional and post-approval-only")
        payload = self.model_dump(mode="json", exclude={"manifest_fingerprint"})
        if deterministic_fingerprint(payload) != self.manifest_fingerprint:
            raise ValueError("LINCS release manifest fingerprint does not match its content")
        return self


class LincsReleaseRegistry(ImmutableV2Contract):
    registry_version: Literal["1.0.0"] = LINCS_RELEASE_REGISTRY_VERSION
    releases: list[LincsReleaseManifest] = Field(min_length=1, max_length=20)

    def release(self, release_id: str) -> LincsReleaseManifest:
        matches = [item for item in self.releases if item.release_id == release_id]
        if len(matches) != 1:
            raise KeyError(release_id)
        return matches[0]


def load_lincs_release_registry(repo_root: Path) -> LincsReleaseRegistry:
    path = Path(repo_root) / "registry" / "data" / "lincs_release_manifests.json"
    return LincsReleaseRegistry.model_validate_json(path.read_text(encoding="utf-8"))


class LincsRowStatus(StrEnum):
    VALID = "valid"
    INCOMPLETE = "incomplete"
    EXCLUDED = "excluded"


class LincsPerturbagenRecord(ImmutableV2Contract):
    record_id: str
    pert_id: str | None = None
    perturbation_type: str | None = None
    canonical_name: str | None = None
    inchikey: str | None = Field(default=None, pattern=r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
    pubchem_cid: str | None = Field(default=None, pattern=r"^CID:[1-9][0-9]{0,11}$")
    synonyms: list[str] = Field(default_factory=list, max_length=100)
    structure_source_fields: dict[str, str] = Field(default_factory=dict, max_length=20)
    source_release: str
    source_locator: str
    row_status: LincsRowStatus
    exclusion_reason: str | None = None
    original_source_fields: dict[str, str]


class LincsSignatureRecord(ImmutableV2Contract):
    record_id: str
    sig_id: str | None = None
    pert_id: str | None = None
    perturbation_type: str | None = None
    cell_id: str | None = None
    dose: str | None = None
    exposure_time: str | None = None
    quality_status: str | None = None
    processing_metadata: str | None = None
    measured_chemical_perturbation: bool
    source_release: str
    source_locator: str
    row_status: LincsRowStatus
    exclusion_reason: str | None = None
    original_source_fields: dict[str, str]


class LincsCellRecord(ImmutableV2Contract):
    record_id: str
    cell_id: str | None = None
    organism: str | None = None
    tissue_or_lineage: str | None = None
    disease_or_context: str | None = None
    source_release: str
    source_locator: str
    row_status: LincsRowStatus
    exclusion_reason: str | None = None
    original_source_fields: dict[str, str]


class LincsGeneRecord(ImmutableV2Contract):
    record_id: str
    source_gene_identifier: str | None = None
    gene_symbol: str | None = None
    landmark_gene_status: bool | None = None
    source_release: str
    source_locator: str
    row_status: LincsRowStatus
    exclusion_reason: str | None = None
    original_source_fields: dict[str, str]


class LincsNormalizedMetadataBundle(ImmutableV2Contract):
    release_id: str
    source_release: str
    manifest_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    perturbagens: list[LincsPerturbagenRecord]
    signatures: list[LincsSignatureRecord]
    cells: list[LincsCellRecord]
    genes: list[LincsGeneRecord]
    source_field_mappings: dict[str, dict[str, str]]
    source_artifact_ids: list[str]
    expression_values_retrieved: Literal[False] = False
    bundle_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_fingerprint(self) -> LincsNormalizedMetadataBundle:
        payload = self.model_dump(mode="json", exclude={"bundle_fingerprint"})
        if deterministic_fingerprint(payload) != self.bundle_fingerprint:
            raise ValueError("LINCS metadata bundle fingerprint does not match")
        return self

    @classmethod
    def create(cls, **values: Any) -> LincsNormalizedMetadataBundle:
        values["bundle_fingerprint"] = deterministic_fingerprint(values)
        return cls.model_validate(values)


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return None if not text or text.casefold() in {"-666", "na", "nan", "none", "null"} else text


def _cid(value: Any) -> str | None:
    text = _clean(value)
    if text is None:
        return None
    text = text.removeprefix("CID:")
    return f"CID:{text}" if re.fullmatch(r"[1-9][0-9]{0,11}", text) else None


def _inchikey(value: Any) -> str | None:
    text = (_clean(value) or "").upper()
    return text if FULL_INCHIKEY.fullmatch(text) else None


def _bool(value: Any) -> bool | None:
    text = (_clean(value) or "").casefold()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return None


def parse_lincs_metadata_resource(
    resource: ProviderResourceRequest, content: bytes
) -> dict[str, Any]:
    if resource.logical_role == "level5_gctx":
        raise ValueError("Level-5 GCTX access is prohibited during metadata discovery")
    try:
        if resource.compression == "gzip":
            maximum = min(
                MAXIMUM_DECOMPRESSED_METADATA_BYTES,
                resource.maximum_response_bytes * 10,
            )
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as handle:
                payload = handle.read(maximum + 1)
            if len(payload) > maximum:
                raise ValueError("LINCS metadata exceeds its decompressed size policy")
        else:
            payload = content
    except (OSError, EOFError) as exc:
        raise ValueError("LINCS metadata resource is not valid reviewed gzip content") from exc
    text = payload.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    if reader.fieldnames is None:
        raise ValueError("LINCS metadata resource has no header")
    rows = [{str(key): str(value or "") for key, value in row.items()} for row in reader]
    return {"columns": list(reader.fieldnames), "rows": rows, "record_count": len(rows)}


def _value(row: dict[str, str], mapping: dict[str, str], field: str) -> str | None:
    source_field = mapping.get(field)
    return _clean(row.get(source_field, "")) if source_field else None


def normalize_lincs_metadata(
    manifest: LincsReleaseManifest,
    outcome: ProviderExecutionOutcome,
) -> LincsNormalizedMetadataBundle:
    if not outcome.completion_proof.completed:
        raise ValueError("LINCS metadata cannot be normalized before manifest completion")
    resources = {item.logical_role: item for item in manifest.resources}
    for role in METADATA_ROLES:
        parsed = outcome.normalized_resources.get(role) or outcome.normalized_resources.get(
            resources[role].resource_id
        )
        if parsed is None:
            raise ValueError(f"LINCS normalized resource is missing: {role}")
        missing_columns = set(resources[role].expected_columns) - set(parsed.get("columns", []))
        if missing_columns:
            raise ValueError(
                f"LINCS {role} lacks reviewed columns: {', '.join(sorted(missing_columns))}"
            )

    parsed_by_role = {
        role: outcome.normalized_resources[resources[role].resource_id] for role in METADATA_ROLES
    }
    perturbagens: list[LincsPerturbagenRecord] = []
    for index, row in enumerate(parsed_by_role["pert_info"]["rows"]):
        mapping = resources["pert_info"].column_mapping
        pert_id = _value(row, mapping, "pert_id")
        pert_type = _value(row, mapping, "perturbation_type")
        canonical_name = _value(row, mapping, "canonical_name")
        row_status = LincsRowStatus.VALID
        reason = None
        if not pert_id or not pert_type:
            row_status, reason = LincsRowStatus.INCOMPLETE, "missing_pert_id_or_type"
        elif pert_type not in manifest.chemical_perturbation_types:
            row_status, reason = LincsRowStatus.EXCLUDED, "non_chemical_perturbation"
        synonyms = [
            item.strip()
            for field in ("pert_alias", "aliases", "synonyms")
            for item in str(row.get(field, "")).split("|")
            if item.strip()
        ]
        perturbagens.append(
            LincsPerturbagenRecord(
                record_id=f"{manifest.release_id}:pert:{index + 1}",
                pert_id=pert_id,
                perturbation_type=pert_type,
                canonical_name=canonical_name,
                inchikey=_inchikey(_value(row, mapping, "inchikey")),
                pubchem_cid=_cid(_value(row, mapping, "pubchem_cid")),
                synonyms=list(dict.fromkeys(synonyms)),
                structure_source_fields={
                    field: value
                    for field in ("canonical_smiles", "inchi_key", "pubchem_cid")
                    if (value := row.get(field, "")).strip()
                },
                source_release=manifest.source_release,
                source_locator=resources["pert_info"].public_locator,
                row_status=row_status,
                exclusion_reason=reason,
                original_source_fields=row,
            )
        )
    perturbation_types = {
        item.pert_id: item.perturbation_type for item in perturbagens if item.pert_id
    }

    signatures: list[LincsSignatureRecord] = []
    for index, row in enumerate(parsed_by_role["sig_info"]["rows"]):
        mapping = resources["sig_info"].column_mapping
        sig_id = _value(row, mapping, "sig_id")
        pert_id = _value(row, mapping, "pert_id")
        pert_type = _value(row, mapping, "perturbation_type") or (
            perturbation_types.get(pert_id) if pert_id else None
        )
        cell_id = _value(row, mapping, "cell_id")
        row_status = LincsRowStatus.VALID
        reason = None
        if not sig_id or not pert_id or not cell_id:
            row_status, reason = LincsRowStatus.INCOMPLETE, "missing_sig_pert_or_cell_identifier"
        measured = pert_type in manifest.chemical_perturbation_types
        if row_status is LincsRowStatus.VALID and not measured:
            row_status, reason = LincsRowStatus.EXCLUDED, "non_chemical_perturbation"
        signatures.append(
            LincsSignatureRecord(
                record_id=f"{manifest.release_id}:sig:{index + 1}",
                sig_id=sig_id,
                pert_id=pert_id,
                perturbation_type=pert_type,
                cell_id=cell_id,
                dose=_value(row, mapping, "dose"),
                exposure_time=_value(row, mapping, "exposure_time"),
                quality_status=_value(row, mapping, "quality_status"),
                processing_metadata=_value(row, mapping, "processing_metadata"),
                measured_chemical_perturbation=measured,
                source_release=manifest.source_release,
                source_locator=resources["sig_info"].public_locator,
                row_status=row_status,
                exclusion_reason=reason,
                original_source_fields=row,
            )
        )

    cells: list[LincsCellRecord] = []
    for index, row in enumerate(parsed_by_role["cell_info"]["rows"]):
        mapping = resources["cell_info"].column_mapping
        cell_id = _value(row, mapping, "cell_id")
        cells.append(
            LincsCellRecord(
                record_id=f"{manifest.release_id}:cell:{index + 1}",
                cell_id=cell_id,
                organism=_value(row, mapping, "organism"),
                tissue_or_lineage=_value(row, mapping, "tissue_or_lineage"),
                disease_or_context=_value(row, mapping, "disease_or_context"),
                source_release=manifest.source_release,
                source_locator=resources["cell_info"].public_locator,
                row_status=(LincsRowStatus.VALID if cell_id else LincsRowStatus.INCOMPLETE),
                exclusion_reason=None if cell_id else "missing_cell_id",
                original_source_fields=row,
            )
        )

    genes: list[LincsGeneRecord] = []
    for index, row in enumerate(parsed_by_role["gene_info"]["rows"]):
        mapping = resources["gene_info"].column_mapping
        gene_id = _value(row, mapping, "gene_identifier")
        symbol = _value(row, mapping, "gene_symbol")
        genes.append(
            LincsGeneRecord(
                record_id=f"{manifest.release_id}:gene:{index + 1}",
                source_gene_identifier=gene_id,
                gene_symbol=symbol,
                landmark_gene_status=_bool(_value(row, mapping, "landmark_gene_status")),
                source_release=manifest.source_release,
                source_locator=resources["gene_info"].public_locator,
                row_status=(
                    LincsRowStatus.VALID if gene_id and symbol else LincsRowStatus.INCOMPLETE
                ),
                exclusion_reason=(
                    None if gene_id and symbol else "missing_gene_identifier_or_symbol"
                ),
                original_source_fields=row,
            )
        )

    return LincsNormalizedMetadataBundle.create(
        release_id=manifest.release_id,
        source_release=manifest.source_release,
        manifest_fingerprint=manifest.manifest_fingerprint,
        perturbagens=perturbagens,
        signatures=signatures,
        cells=cells,
        genes=genes,
        source_field_mappings={role: resources[role].column_mapping for role in METADATA_ROLES},
        source_artifact_ids=sorted(
            item.raw_artifact.artifact_id
            for item in outcome.page_or_file_results
            if item.raw_artifact is not None
        ),
        expression_values_retrieved=False,
    )


class LincsMetadataIndexes(ImmutableV2Contract):
    release_id: str
    bundle_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    perturbagen_record_ids_by_pert_id: dict[str, list[str]]
    perturbagen_record_ids_by_full_inchikey: dict[str, list[str]]
    perturbagen_record_ids_by_pubchem_cid: dict[str, list[str]]
    perturbagen_record_ids_by_exact_name: dict[str, list[str]]
    signature_record_ids_by_pert_id: dict[str, list[str]]
    signature_record_ids_by_sig_id: dict[str, list[str]]
    cell_record_ids_by_cell_id: dict[str, list[str]]
    gene_record_ids_by_gene_identifier: dict[str, list[str]]
    landmark_gene_identifiers: list[str]
    index_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_fingerprint(self) -> LincsMetadataIndexes:
        payload = self.model_dump(mode="json", exclude={"index_fingerprint"})
        if deterministic_fingerprint(payload) != self.index_fingerprint:
            raise ValueError("LINCS metadata index fingerprint does not match")
        return self

    @classmethod
    def create(cls, **values: Any) -> LincsMetadataIndexes:
        values["index_fingerprint"] = deterministic_fingerprint(values)
        return cls.model_validate(values)


def _sorted_mapping(values: dict[str, list[str]]) -> dict[str, list[str]]:
    return {key: sorted(set(items)) for key, items in sorted(values.items())}


def build_lincs_metadata_indexes(bundle: LincsNormalizedMetadataBundle) -> LincsMetadataIndexes:
    pert_id: defaultdict[str, list[str]] = defaultdict(list)
    inchikey: defaultdict[str, list[str]] = defaultdict(list)
    cid: defaultdict[str, list[str]] = defaultdict(list)
    names: defaultdict[str, list[str]] = defaultdict(list)
    for item in bundle.perturbagens:
        if item.row_status is LincsRowStatus.EXCLUDED:
            continue
        if item.pert_id:
            pert_id[item.pert_id].append(item.record_id)
        if item.inchikey:
            inchikey[item.inchikey].append(item.record_id)
        if item.pubchem_cid:
            cid[item.pubchem_cid].append(item.record_id)
        for name in [item.canonical_name, *item.synonyms]:
            if name:
                names[name.casefold()].append(item.record_id)
    sig_by_pert: defaultdict[str, list[str]] = defaultdict(list)
    sig_by_id: defaultdict[str, list[str]] = defaultdict(list)
    for item in bundle.signatures:
        if item.pert_id:
            sig_by_pert[item.pert_id].append(item.record_id)
        if item.sig_id:
            sig_by_id[item.sig_id].append(item.record_id)
    cell_by_id: defaultdict[str, list[str]] = defaultdict(list)
    for item in bundle.cells:
        if item.cell_id:
            cell_by_id[item.cell_id].append(item.record_id)
    gene_by_id: defaultdict[str, list[str]] = defaultdict(list)
    for item in bundle.genes:
        if item.source_gene_identifier:
            gene_by_id[item.source_gene_identifier].append(item.record_id)
    return LincsMetadataIndexes.create(
        release_id=bundle.release_id,
        bundle_fingerprint=bundle.bundle_fingerprint,
        perturbagen_record_ids_by_pert_id=_sorted_mapping(pert_id),
        perturbagen_record_ids_by_full_inchikey=_sorted_mapping(inchikey),
        perturbagen_record_ids_by_pubchem_cid=_sorted_mapping(cid),
        perturbagen_record_ids_by_exact_name=_sorted_mapping(names),
        signature_record_ids_by_pert_id=_sorted_mapping(sig_by_pert),
        signature_record_ids_by_sig_id=_sorted_mapping(sig_by_id),
        cell_record_ids_by_cell_id=_sorted_mapping(cell_by_id),
        gene_record_ids_by_gene_identifier=_sorted_mapping(gene_by_id),
        landmark_gene_identifiers=sorted(
            item.source_gene_identifier
            for item in bundle.genes
            if item.landmark_gene_status and item.source_gene_identifier
        ),
    )


class CompoundIdentityRecord(ImmutableV2Contract):
    compound_id: str = Field(min_length=1, max_length=300)
    inchikey: str | None = Field(default=None, pattern=r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
    pubchem_cid: str | None = Field(default=None, pattern=r"^CID:[1-9][0-9]{0,11}$")
    reviewed_provider_identifiers: dict[str, str] = Field(default_factory=dict, max_length=20)
    canonical_name: str | None = Field(default=None, max_length=500)
    synonyms: list[str] = Field(default_factory=list, max_length=100)


class LincsCompoundMatchStatus(StrEnum):
    EXACT_MATCH = "exact_match"
    AMBIGUOUS_MATCH = "ambiguous_match"
    IDENTIFIER_MISSING = "identifier_missing"
    SOURCE_MAPPING_MISSING = "source_mapping_missing"
    NOT_FOUND = "not_found"


class LincsCompoundMatch(ImmutableV2Contract):
    compound_id: str
    status: LincsCompoundMatchStatus
    match_method: str | None = None
    perturbagen_record_ids: list[str] = Field(default_factory=list)
    pert_ids: list[str] = Field(default_factory=list)
    explanation: str


def match_lincs_compounds(
    compounds: list[CompoundIdentityRecord],
    bundle: LincsNormalizedMetadataBundle,
    indexes: LincsMetadataIndexes,
) -> list[LincsCompoundMatch]:
    by_record = {item.record_id: item for item in bundle.perturbagens}
    results: list[LincsCompoundMatch] = []
    for compound in compounds:
        candidates: list[str] = []
        method: str | None = None
        if compound.inchikey:
            candidates = indexes.perturbagen_record_ids_by_full_inchikey.get(compound.inchikey, [])
            method = "exact_full_inchikey" if candidates else None
        if not candidates and compound.pubchem_cid:
            candidates = indexes.perturbagen_record_ids_by_pubchem_cid.get(compound.pubchem_cid, [])
            method = "exact_pubchem_cid" if candidates else None
        provider_id = compound.reviewed_provider_identifiers.get("lincs-l1000") or (
            compound.reviewed_provider_identifiers.get("lincs")
        )
        if not candidates and provider_id:
            candidates = indexes.perturbagen_record_ids_by_pert_id.get(provider_id, [])
            method = "exact_reviewed_provider_identifier" if candidates else None
        if not candidates:
            name_candidates = {
                record_id
                for name in [compound.canonical_name, *compound.synonyms]
                if name
                for record_id in indexes.perturbagen_record_ids_by_exact_name.get(
                    name.casefold(), []
                )
            }
            candidates = sorted(name_candidates)
            method = "unique_exact_name_or_synonym" if candidates else None
        if len(candidates) == 1:
            record = by_record[candidates[0]]
            results.append(
                LincsCompoundMatch(
                    compound_id=compound.compound_id,
                    status=LincsCompoundMatchStatus.EXACT_MATCH,
                    match_method=method,
                    perturbagen_record_ids=candidates,
                    pert_ids=[record.pert_id] if record.pert_id else [],
                    explanation="One exact reviewed LINCS metadata mapping was found.",
                )
            )
        elif len(candidates) > 1:
            results.append(
                LincsCompoundMatch(
                    compound_id=compound.compound_id,
                    status=LincsCompoundMatchStatus.AMBIGUOUS_MATCH,
                    match_method=method,
                    perturbagen_record_ids=candidates,
                    pert_ids=sorted(
                        {by_record[item].pert_id for item in candidates if by_record[item].pert_id}
                    ),
                    explanation="Multiple exact LINCS mappings exist; none was selected silently.",
                )
            )
        elif provider_id:
            results.append(
                LincsCompoundMatch(
                    compound_id=compound.compound_id,
                    status=LincsCompoundMatchStatus.SOURCE_MAPPING_MISSING,
                    explanation=(
                        "The reviewed LINCS provider identifier is absent from the release."
                    ),
                )
            )
        elif not any(
            [compound.inchikey, compound.pubchem_cid, compound.canonical_name, *compound.synonyms]
        ):
            results.append(
                LincsCompoundMatch(
                    compound_id=compound.compound_id,
                    status=LincsCompoundMatchStatus.IDENTIFIER_MISSING,
                    explanation="No exact identifier or reviewed name is available for matching.",
                )
            )
        else:
            results.append(
                LincsCompoundMatch(
                    compound_id=compound.compound_id,
                    status=LincsCompoundMatchStatus.NOT_FOUND,
                    explanation="No exact LINCS metadata mapping was found.",
                )
            )
    return results


class LincsContextFilter(ImmutableV2Contract):
    context_id: str
    cell_ids: list[str] = Field(default_factory=list)
    organisms: list[str] = Field(default_factory=list)
    doses: list[str] = Field(default_factory=list)
    exposure_times: list[str] = Field(default_factory=list)
    quality_statuses: list[str] = Field(default_factory=list)


class LincsContextCoverageView(ImmutableV2Contract):
    view_id: str
    operator: Literal["individual", "union", "intersection", "at_least_one", "every"]
    context_ids: list[str]
    compound_ids: list[str]
    exact_sig_ids: list[str]
    signature_count: int = Field(ge=0)


class LincsMetadataCoverage(ImmutableV2Contract):
    release_id: str
    total_input_compounds: int = Field(ge=0)
    exact_perturbagen_matches: int = Field(ge=0)
    ambiguous_matches: int = Field(ge=0)
    unmatched_compounds: int = Field(ge=0)
    compounds_with_measured_chemical_signatures: int = Field(ge=0)
    signature_counts_per_compound: dict[str, int]
    coverage_by_cell: dict[str, int]
    coverage_by_organism: dict[str, int]
    coverage_by_dose: dict[str, int]
    coverage_by_exposure_time: dict[str, int]
    coverage_by_processing_or_quality_status: dict[str, int]
    exact_available_sig_ids: list[str]
    missing_metadata_fields: dict[str, int]
    exclusion_reasons: dict[str, int]
    compound_matches: list[LincsCompoundMatch]
    context_views: list[LincsContextCoverageView]
    expression_values_retrieved: Literal[False] = False
    coverage_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_fingerprint(self) -> LincsMetadataCoverage:
        payload = self.model_dump(mode="json", exclude={"coverage_fingerprint"})
        if deterministic_fingerprint(payload) != self.coverage_fingerprint:
            raise ValueError("LINCS metadata coverage fingerprint does not match")
        return self

    @classmethod
    def create(cls, **values: Any) -> LincsMetadataCoverage:
        values["coverage_fingerprint"] = deterministic_fingerprint(values)
        return cls.model_validate(values)


def _signature_matches_context(
    signature: LincsSignatureRecord,
    cell: LincsCellRecord | None,
    context: LincsContextFilter,
) -> bool:
    checks = (
        not context.cell_ids or signature.cell_id in context.cell_ids,
        not context.organisms or (cell is not None and cell.organism in context.organisms),
        not context.doses or signature.dose in context.doses,
        not context.exposure_times or signature.exposure_time in context.exposure_times,
        not context.quality_statuses or signature.quality_status in context.quality_statuses,
    )
    return all(checks)


def compute_lincs_metadata_coverage(
    compounds: list[CompoundIdentityRecord],
    bundle: LincsNormalizedMetadataBundle,
    indexes: LincsMetadataIndexes,
    contexts: list[LincsContextFilter] | None = None,
) -> LincsMetadataCoverage:
    matches = match_lincs_compounds(compounds, bundle, indexes)
    signature_by_record = {item.record_id: item for item in bundle.signatures}
    cell_by_id = {item.cell_id: item for item in bundle.cells if item.cell_id}
    signatures_by_compound: dict[str, list[LincsSignatureRecord]] = {}
    for match in matches:
        signatures = [
            signature_by_record[record_id]
            for pert_id in match.pert_ids
            for record_id in indexes.signature_record_ids_by_pert_id.get(pert_id, [])
            if match.status is LincsCompoundMatchStatus.EXACT_MATCH
            if signature_by_record[record_id].measured_chemical_perturbation
            and signature_by_record[record_id].row_status is LincsRowStatus.VALID
        ]
        signatures_by_compound[match.compound_id] = signatures

    all_signatures = [item for values in signatures_by_compound.values() for item in values]
    coverage_by_cell = Counter(item.cell_id or "missing" for item in all_signatures)
    coverage_by_organism = Counter(
        (cell_by_id[item.cell_id].organism if item.cell_id in cell_by_id else None) or "missing"
        for item in all_signatures
    )
    coverage_by_dose = Counter(item.dose or "missing" for item in all_signatures)
    coverage_by_time = Counter(item.exposure_time or "missing" for item in all_signatures)
    coverage_by_quality = Counter(item.quality_status or "missing" for item in all_signatures)
    missing = Counter(
        field
        for item in all_signatures
        for field, value in {
            "cell_id": item.cell_id,
            "dose": item.dose,
            "exposure_time": item.exposure_time,
            "quality_status": item.quality_status,
        }.items()
        if value is None
    )
    exclusion_values = [
        *(item.exclusion_reason for item in bundle.perturbagens if item.exclusion_reason),
        *(item.exclusion_reason for item in bundle.signatures if item.exclusion_reason),
        *(item.exclusion_reason for item in bundle.cells if item.exclusion_reason),
        *(item.exclusion_reason for item in bundle.genes if item.exclusion_reason),
    ]
    exclusions = Counter(exclusion_values)
    contexts = list(contexts or [])
    context_compounds: dict[str, set[str]] = {}
    context_sigs: dict[str, set[str]] = {}
    views: list[LincsContextCoverageView] = []
    for context in contexts:
        selected_pairs = [
            (compound_id, signature)
            for compound_id, signatures in signatures_by_compound.items()
            for signature in signatures
            if _signature_matches_context(
                signature, cell_by_id.get(signature.cell_id or ""), context
            )
        ]
        context_compounds[context.context_id] = {item[0] for item in selected_pairs}
        context_sigs[context.context_id] = {
            item[1].sig_id for item in selected_pairs if item[1].sig_id
        }
        views.append(
            LincsContextCoverageView(
                view_id=f"individual:{context.context_id}",
                operator="individual",
                context_ids=[context.context_id],
                compound_ids=sorted(context_compounds[context.context_id]),
                exact_sig_ids=sorted(context_sigs[context.context_id]),
                signature_count=len(context_sigs[context.context_id]),
            )
        )
    if contexts:
        context_ids = [item.context_id for item in contexts]
        union_compounds = set().union(*(context_compounds[item] for item in context_ids))
        union_sigs = set().union(*(context_sigs[item] for item in context_ids))
        intersection_compounds = set.intersection(
            *(context_compounds[item] for item in context_ids)
        )
        intersection_sigs = {
            signature.sig_id
            for compound_id in intersection_compounds
            for signature in signatures_by_compound[compound_id]
            if signature.sig_id
            and any(
                _signature_matches_context(
                    signature, cell_by_id.get(signature.cell_id or ""), context
                )
                for context in contexts
            )
        }
        for operator, compounds_set, sigs_set in (
            ("union", union_compounds, union_sigs),
            ("at_least_one", union_compounds, union_sigs),
            ("intersection", intersection_compounds, intersection_sigs),
            ("every", intersection_compounds, intersection_sigs),
        ):
            views.append(
                LincsContextCoverageView(
                    view_id=f"{operator}:" + "+".join(context_ids),
                    operator=operator,
                    context_ids=context_ids,
                    compound_ids=sorted(compounds_set),
                    exact_sig_ids=sorted(sigs_set),
                    signature_count=len(sigs_set),
                )
            )

    exact = sum(item.status is LincsCompoundMatchStatus.EXACT_MATCH for item in matches)
    ambiguous = sum(item.status is LincsCompoundMatchStatus.AMBIGUOUS_MATCH for item in matches)
    return LincsMetadataCoverage.create(
        release_id=bundle.release_id,
        total_input_compounds=len(compounds),
        exact_perturbagen_matches=exact,
        ambiguous_matches=ambiguous,
        unmatched_compounds=len(compounds) - exact - ambiguous,
        compounds_with_measured_chemical_signatures=sum(
            bool(values) for values in signatures_by_compound.values()
        ),
        signature_counts_per_compound={
            item.compound_id: len(signatures_by_compound[item.compound_id]) for item in compounds
        },
        coverage_by_cell=dict(sorted(coverage_by_cell.items())),
        coverage_by_organism=dict(sorted(coverage_by_organism.items())),
        coverage_by_dose=dict(sorted(coverage_by_dose.items())),
        coverage_by_exposure_time=dict(sorted(coverage_by_time.items())),
        coverage_by_processing_or_quality_status=dict(sorted(coverage_by_quality.items())),
        exact_available_sig_ids=sorted({item.sig_id for item in all_signatures if item.sig_id}),
        missing_metadata_fields=dict(sorted(missing.items())),
        exclusion_reasons=dict(sorted(exclusions.items())),
        compound_matches=matches,
        context_views=views,
        expression_values_retrieved=False,
    )


class LincsMetadataRetrievalInput(ImmutableV2Contract):
    release_id: str
    ledger_record: DiscoveryExecutionRecord


class LincsMetadataRetrievalOutput(ImmutableV2Contract):
    execution: ProviderExecutionOutcome
    metadata: LincsNormalizedMetadataBundle | None = None


class LincsIndexInput(ImmutableV2Contract):
    metadata: LincsNormalizedMetadataBundle


class LincsIndexOutput(ImmutableV2Contract):
    indexes: LincsMetadataIndexes


class LincsCoverageInput(ImmutableV2Contract):
    compounds: list[CompoundIdentityRecord]
    metadata: LincsNormalizedMetadataBundle
    indexes: LincsMetadataIndexes
    contexts: list[LincsContextFilter] = Field(default_factory=list)


class LincsCoverageOutput(ImmutableV2Contract):
    coverage: LincsMetadataCoverage


class ApprovedLincsSliceInput(ImmutableV2Contract):
    assembly_recipe_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    release_id: str
    exact_sig_ids: list[str] = Field(min_length=1, max_length=100_000)
    exact_gene_identifiers: list[str] = Field(min_length=1, max_length=20_000)


class ApprovedLincsSliceOutput(ImmutableV2Contract):
    expression_artifact_id: str
    selected_signature_count: int = Field(ge=1)
    selected_gene_count: int = Field(ge=1)


def unavailable_lincs_level5_slice(
    _request: ApprovedLincsSliceInput,
) -> ApprovedLincsSliceOutput:
    raise RuntimeError(
        "LINCS Level-5 partial extraction is deferred until the post-approval extractor phase."
    )


class LincsMetadataProvider:
    def __init__(self, registry: LincsReleaseRegistry, executor: ProviderTaskExecutor) -> None:
        self.registry = registry
        self.executor = executor

    def retrieval_task(
        self,
        *,
        workflow_id: str,
        task_id: str,
        release_id: str,
        idempotency_key: str,
    ) -> ProviderRetrievalTask:
        manifest = self.registry.release(release_id)
        resources = [
            ProviderResourceRequest(
                resource_id=item.resource_id,
                logical_role=item.logical_role,
                locator=item.public_locator,
                item_kind=ProviderItemKind.FILE,
                required=item.required,
                expected_sha256=item.expected_sha256,
                accepted_mime_types=[
                    "application/gzip",
                    "application/x-gzip",
                    "application/octet-stream",
                    "text/plain",
                ],
                maximum_response_bytes=item.maximum_response_bytes,
                cursor_or_file_token=item.expected_filename_pattern,
                compression=item.compression,
            )
            for item in manifest.resources
            if item.access_phase is ToolAccessPhase.PRE_APPROVAL_METADATA
        ]
        return ProviderRetrievalTask.create(
            workflow_id=workflow_id,
            task_id=task_id,
            provider="lincs-l1000",
            operation="retrieve_lincs_metadata_release",
            source_version=manifest.source_release,
            source_locator=manifest.source_locator,
            resources=resources,
            licence_and_provenance=manifest.licence_and_provenance,
            timeout_seconds=180.0,
            maximum_transport_retries=1,
            idempotency_key=idempotency_key,
        )

    def retrieve(
        self,
        request: LincsMetadataRetrievalInput,
        invocation: ToolInvocation,
    ) -> LincsMetadataRetrievalOutput:
        if invocation.workflow_id is None:
            raise ValueError("LINCS retrieval requires a durable workflow identifier")
        task = self.retrieval_task(
            workflow_id=invocation.workflow_id,
            task_id=request.ledger_record.task_id,
            release_id=request.release_id,
            idempotency_key=invocation.idempotency_key or request.ledger_record.task_id,
        )
        execution = self.executor.execute(
            task, request.ledger_record, parser=parse_lincs_metadata_resource
        )
        metadata = (
            normalize_lincs_metadata(self.registry.release(request.release_id), execution)
            if execution.completion_proof.completed
            else None
        )
        return LincsMetadataRetrievalOutput(execution=execution, metadata=metadata)


def build_lincs_indexes_tool(request: LincsIndexInput) -> LincsIndexOutput:
    return LincsIndexOutput(indexes=build_lincs_metadata_indexes(request.metadata))


def compute_lincs_coverage_tool(request: LincsCoverageInput) -> LincsCoverageOutput:
    return LincsCoverageOutput(
        coverage=compute_lincs_metadata_coverage(
            request.compounds, request.metadata, request.indexes, request.contexts
        )
    )
