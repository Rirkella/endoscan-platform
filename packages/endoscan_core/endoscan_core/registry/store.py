"""Thin JSON-backed read/write store for the EndoScan registry.

Persists `EndpointEntry` records to ``registry/models/endpoints.json`` (the
index) and resolves per-endpoint artifacts under ``models/{ENDPOINT_ID}/``.

Path semantics
--------------
Artifact paths in the index are stored **relative to the repository root**
(e.g. ``models/ER/model.pkl``) and resolved here against a repo-root anchor.
Rationale: the index is git-versioned text shared across machines/CI, so
absolute paths would be non-portable; and the index lives in
``registry/models/`` while artifacts live in ``models/{ID}/`` (a different
subtree), so endpoint-folder-relative paths would need fragile ``../../`` walks.
Repo-root-relative also matches how DVC tracks ``models/``.

Callers may pass an explicit ``repo_root`` to every function (tests do this to
operate on a throwaway ``tmp_path`` copy and never touch the real index).

Scope (M1)
----------
This is the entire contract surface: list/get/register/update_status plus a
minimal `load_model` that only deserializes a trusted, git-tracked stub. No
training, inference, or real model logic.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

from .schema import EndpointEntry, EndpointIndex, EndpointStatus

#: Location of the index, relative to the repository root.
INDEX_RELPATH = Path("registry/models/endpoints.json")
_REPO_MARKERS = (".git", "pyproject.toml")


class RegistryError(Exception):
    """Base class for registry errors."""


class RegistryValidationError(RegistryError):
    """Raised when an operation would violate a registry invariant."""


class EndpointNotFoundError(RegistryError):
    """Raised when an endpoint id is not present in the index."""


class FrozenEndpointError(RegistryValidationError):
    """Raised when re-registration is attempted on a FROZEN (immutable) endpoint (e.g. ER)."""


def find_repo_root(start: Path | None = None) -> Path:
    """Locate the repository root by walking upward from ``start``.

    The root is the nearest ancestor that both contains ``registry/models`` and
    a workspace ``pyproject.toml`` (the uv workspace root). Falls back to the
    nearest ancestor carrying any repo marker (``.git`` / ``pyproject.toml``).
    """
    start = (start or Path(__file__)).resolve()
    candidates = [start, *start.parents]
    for parent in candidates:
        if (parent / "registry" / "models").is_dir() and (parent / "pyproject.toml").is_file():
            return parent
    for parent in candidates:
        if any((parent / marker).exists() for marker in _REPO_MARKERS):
            return parent
    raise RegistryError(f"Could not locate repository root from {start}")


def _index_path(repo_root: Path) -> Path:
    return repo_root / INDEX_RELPATH


def _read_index(repo_root: Path) -> EndpointIndex:
    path = _index_path(repo_root)
    if not path.is_file():
        raise RegistryError(f"Registry index not found at {path}")
    with path.open("r", encoding="utf-8") as handle:
        return EndpointIndex.model_validate(json.load(handle))


def _write_index(repo_root: Path, index: EndpointIndex) -> None:
    path = _index_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(index.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _require_artifacts_exist(entry: EndpointEntry, repo_root: Path) -> None:
    """Enforce the validated_mvp gate: every referenced artifact must exist."""
    missing = [rel for rel in entry.artifact_paths() if not (repo_root / rel).is_file()]
    if missing:
        raise RegistryValidationError(
            f"Cannot promote {entry.endpoint_id!r} to 'validated_mvp': "
            f"missing artifact path(s): {', '.join(missing)}"
        )


def list_endpoints(repo_root: Path | None = None) -> list[EndpointEntry]:
    """Return all registered endpoints."""
    root = repo_root or find_repo_root()
    return list(_read_index(root).endpoints)


def get_endpoint(endpoint_id: str, repo_root: Path | None = None) -> EndpointEntry:
    """Return the entry with ``endpoint_id`` or raise `EndpointNotFoundError`."""
    root = repo_root or find_repo_root()
    for entry in _read_index(root).endpoints:
        if entry.endpoint_id == endpoint_id:
            return entry
    raise EndpointNotFoundError(f"No endpoint with id {endpoint_id!r} in registry")


def register_endpoint(entry: EndpointEntry, repo_root: Path | None = None) -> EndpointEntry:
    """Append a new entry to the index.

    Rejects duplicate ids and, when registering directly as ``validated_mvp``,
    enforces the artifact-existence gate.
    """
    root = repo_root or find_repo_root()
    index = _read_index(root)
    if any(existing.endpoint_id == entry.endpoint_id for existing in index.endpoints):
        raise RegistryValidationError(f"Endpoint {entry.endpoint_id!r} is already registered")
    if entry.status is EndpointStatus.validated_mvp:
        _require_artifacts_exist(entry, root)
    index.endpoints.append(entry)
    _write_index(root, index)
    return entry


def register_or_update_endpoint(
    entry: EndpointEntry, repo_root: Path | None = None
) -> EndpointEntry:
    """Register a NEW endpoint, or RE-REGISTER (overwrite) an existing NON-FROZEN one.

    A first registration behaves exactly like :func:`register_endpoint` (append + the
    validated_mvp artifact gate). For an EXISTING id this overwrites the entry in place —
    enforcing that every referenced artifact exists — while **preserving the original
    ``created_at``** and stamping ``updated_at`` so the audit trail shows the entry was
    re-registered, not silently mutated. A **FROZEN** entry (e.g. ER) is REFUSED with
    :class:`FrozenEndpointError`; frozen endpoints stay immutable and ``frozen`` cannot be
    flipped through this path.

    This changes only the re-registration *mechanics*: it is NOT a bypass of the approval
    gate. The single training/registration path (``run_pipeline``) reaches here ONLY after
    the quality gate PASSES and a human approval is set, so a re-promote still requires gate
    PASS + a valid approval token exactly as a first promote does.
    """
    root = repo_root or find_repo_root()
    index = _read_index(root)
    for position, existing in enumerate(index.endpoints):
        if existing.endpoint_id == entry.endpoint_id:
            if existing.frozen:
                raise FrozenEndpointError(
                    f"Endpoint {entry.endpoint_id!r} is frozen and cannot be overwritten; "
                    "frozen endpoints are immutable (re-registration is for non-frozen "
                    "endpoints only)."
                )
            missing = [rel for rel in entry.artifact_paths() if not (root / rel).is_file()]
            if missing:
                raise RegistryValidationError(
                    f"Cannot re-register {entry.endpoint_id!r}: missing artifact path(s): "
                    f"{', '.join(missing)}"
                )
            updated = entry.model_copy(
                update={
                    "created_at": existing.created_at,  # preserve the original registration
                    "updated_at": entry.created_at,  # audit: when it was re-registered
                    "frozen": existing.frozen,  # cannot flip frozen via re-registration
                }
            )
            index.endpoints[position] = updated
            _write_index(root, index)
            return updated
    # New id -> identical to register_endpoint (append + the validated_mvp gate).
    if entry.status is EndpointStatus.validated_mvp:
        _require_artifacts_exist(entry, root)
    index.endpoints.append(entry)
    _write_index(root, index)
    return entry


def update_status(
    endpoint_id: str,
    status: EndpointStatus | str,
    repo_root: Path | None = None,
) -> EndpointEntry:
    """Update an endpoint's status and persist it.

    ``status`` may be an `EndpointStatus` or its string value; an invalid value
    raises ``ValueError``. Promotion to ``validated_mvp`` enforces the
    artifact-existence gate and never bypasses the documented publication boundary.
    """
    root = repo_root or find_repo_root()
    status = EndpointStatus(status)  # ValueError on an invalid status string
    index = _read_index(root)
    for position, entry in enumerate(index.endpoints):
        if entry.endpoint_id == endpoint_id:
            if status is EndpointStatus.validated_mvp:
                _require_artifacts_exist(entry, root)
            updated = entry.model_copy(update={"status": status})
            index.endpoints[position] = updated
            _write_index(root, index)
            return updated
    raise EndpointNotFoundError(f"No endpoint with id {endpoint_id!r} in registry")


def load_model(endpoint_id: str, repo_root: Path | None = None) -> Any:
    """Deserialize and return the model artifact referenced by an endpoint.

    M1 scope: locate the entry's ``model_path`` (resolved relative to repo root)
    and ``pickle.load`` it. The fixture artifact is a trusted, git-tracked stub,
    so unpickling it is safe here — this performs no inference or model logic.

    SECURITY NOTE FOR LATER MILESTONES: later milestones MUST NOT unpickle
    untrusted or user-supplied artifacts. ``pickle.load`` executes arbitrary
    code on load; real serving (M4+) must use a safe format (e.g. joblib over a
    vetted artifact, or a non-pickle serialization) and never load arbitrary
    uploads.
    """
    root = repo_root or find_repo_root()
    entry = get_endpoint(endpoint_id, repo_root=root)
    model_file = root / entry.model_path
    if not model_file.is_file():
        raise RegistryError(f"Model artifact for {endpoint_id!r} not found at {model_file}")
    with model_file.open("rb") as handle:
        return pickle.load(handle)
