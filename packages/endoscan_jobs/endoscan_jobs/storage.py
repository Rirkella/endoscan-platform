"""Backend-agnostic artifact storage — the contract the runner WRITES and M7 READS.

Keys are logical POSIX paths under five namespaces: ``raw/`` (downloaded source
archives), ``curated/`` (parsed intermediates), ``artifacts/<ENDPOINT>/`` (model
binaries/metrics), ``logs/<run_id>/``, ``reports/``. Callers depend only on the
:class:`Storage` protocol; swapping :class:`LocalStorage` for a future S3/MinIO backend
(same protocol, same keys) needs no caller change.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

#: The five top-level namespaces a job may write under.
NAMESPACES = ("raw", "curated", "artifacts", "logs", "reports")


class StorageKeyError(ValueError):
    """Raised for an unsafe or malformed storage key (traversal, absolute, empty)."""


def normalize_key(key: str) -> str:
    """Validate + normalize a logical key to ``ns/sub/path`` POSIX form.

    Rejects absolute paths, ``..`` traversal, empty segments, and keys whose first
    segment is not one of :data:`NAMESPACES` — so a job can never escape the store root.
    """
    if not key or not key.strip():
        raise StorageKeyError("empty storage key")
    raw = key.strip().replace("\\", "/")
    if raw.startswith("/"):
        raise StorageKeyError(f"absolute keys are not allowed: {key!r}")
    parts = [p for p in PurePosixPath(raw).parts]
    if any(p in ("..", ".") for p in parts) or not parts:
        raise StorageKeyError(f"key must not contain '.'/'..' segments: {key!r}")
    if parts[0] not in NAMESPACES:
        raise StorageKeyError(f"key must start with one of {NAMESPACES}: {key!r}")
    return "/".join(parts)


@runtime_checkable
class Storage(Protocol):
    """Get/put/list artifacts by logical key. Implemented by LocalStorage (and later S3)."""

    def put_bytes(self, key: str, data: bytes) -> str: ...
    def get_bytes(self, key: str) -> bytes: ...
    def put_text(self, key: str, text: str) -> str: ...
    def get_text(self, key: str) -> str: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...
    def local_path(self, key: str) -> Path | None: ...


class LocalStorage:
    """Local-disk backend rooted at a persistent directory (``ENDOSCAN_DATA_ROOT``).

    ``local_path`` returns a real filesystem path (for tools needing one, e.g. parquet/
    SDF readers); a future remote backend returns ``None`` there.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        norm = normalize_key(key)
        path = (self.root / norm).resolve()
        # Defense in depth: the resolved path must stay within the root.
        if self.root != path and self.root not in path.parents:
            raise StorageKeyError(f"key escapes storage root: {key!r}")
        return path

    def put_bytes(self, key: str, data: bytes) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return normalize_key(key)

    def get_bytes(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def put_text(self, key: str, text: str) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return normalize_key(key)

    def get_text(self, key: str) -> str:
        return self._resolve(key).read_text(encoding="utf-8")

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def list(self, prefix: str) -> list[str]:
        norm = normalize_key(prefix) if prefix.split("/")[0] in NAMESPACES else prefix
        base = self.root / norm
        if base.is_dir():
            files = base.rglob("*")
        else:  # treat prefix as a partial path under the root
            base = self.root / norm
            files = base.parent.glob(base.name + "*") if base.parent.is_dir() else []
        return sorted(str(p.relative_to(self.root).as_posix()) for p in files if p.is_file())

    def local_path(self, key: str) -> Path | None:
        return self._resolve(key)
