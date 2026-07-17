"""Build the versioned PubChem identity artifact for committed reference maps.

The serving API never calls PubChem.  This reviewed operator job collects the unique
canonical InChIKeys from committed ER/AR map artifacts, resolves them in bounded batches
through PUG REST, records failures explicitly, and atomically writes one reproducible JSON
artifact for later serving.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PUBCHEM_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
USER_AGENT = "EndoScan-reference-identity-enrichment/1.0 (reviewed research demonstrator)"
INCHIKEY = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
PROPERTY_FIELDS = "Title,IUPACName,CanonicalSMILES,IsomericSMILES,InChIKey"


@dataclass
class PubChemClient:
    """Small sequential PUG REST client with an explicit request-rate ceiling."""

    requests_per_second: float = 3.0
    timeout_seconds: float = 30.0
    max_retries: int = 4
    opener: Callable[..., object] = urllib.request.urlopen

    def __post_init__(self) -> None:
        self._last_request = 0.0
        self.request_count = 0

    def get_json(self, path: str) -> dict:
        interval = 1.0 / self.requests_per_second
        for attempt in range(self.max_retries + 1):
            delay = interval - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            request = urllib.request.Request(
                f"{PUBCHEM_BASE}{path}",
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            self._last_request = time.monotonic()
            self.request_count += 1
            try:
                with self.opener(request, timeout=self.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise
                if exc.code not in {429, 500, 502, 503, 504} or attempt >= self.max_retries:
                    raise
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                time.sleep(float(retry_after) if retry_after else min(8.0, 2.0**attempt))
            except (TimeoutError, urllib.error.URLError):
                if attempt >= self.max_retries:
                    raise
                time.sleep(min(8.0, 2.0**attempt))
        raise RuntimeError("unreachable PubChem retry state")


def collect_map_inchikeys(repo_root: Path) -> list[str]:
    keys: set[str] = set()
    for path in sorted((repo_root / "models").glob("*/explore/umap.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        keys.update(
            str(point["compound_id"]).strip().upper() for point in document.get("points", [])
        )
    return sorted(keys)


def _batched(values: list, size: int = 20):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _properties(client: PubChemClient, keys: list[str]) -> tuple[dict[str, dict], set[str]]:
    """Resolve property records, splitting a failed batch so one missing key cannot hide peers."""
    if not keys:
        return {}, set()
    encoded = urllib.parse.quote(",".join(keys), safe=",-")
    path = f"/compound/inchikey/{encoded}/property/{PROPERTY_FIELDS}/JSON"
    try:
        payload = client.get_json(path)
    except urllib.error.HTTPError as exc:
        if exc.code != 404 or len(keys) == 1:
            if exc.code == 404:
                return {}, set(keys)
            raise
        midpoint = len(keys) // 2
        left, left_missing = _properties(client, keys[:midpoint])
        right, right_missing = _properties(client, keys[midpoint:])
        return {**left, **right}, left_missing | right_missing

    records = {
        str(item["InChIKey"]).upper(): item
        for item in payload.get("PropertyTable", {}).get("Properties", [])
        if item.get("InChIKey")
    }
    return records, set(keys) - set(records)


def _synonyms(client: PubChemClient, cids: list[int]) -> dict[int, list[str]]:
    if not cids:
        return {}
    path = f"/compound/cid/{','.join(str(cid) for cid in cids)}/synonyms/JSON"
    try:
        payload = client.get_json(path)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise
    output: dict[int, list[str]] = {}
    for item in payload.get("InformationList", {}).get("Information", []):
        cid = int(item["CID"])
        unique: list[str] = []
        for value in item.get("Synonym", []):
            text = str(value).strip()
            if text and text.casefold() not in {entry.casefold() for entry in unique}:
                unique.append(text)
            if len(unique) == 20:
                break
        output[cid] = unique
    return output


def build_artifact(repo_root: Path, client: PubChemClient, *, artifact_version: str) -> dict:
    keys = collect_map_inchikeys(repo_root)
    invalid = {key for key in keys if not INCHIKEY.fullmatch(key)}
    valid = [key for key in keys if key not in invalid]
    properties: dict[str, dict] = {}
    missing: set[str] = set()
    transient: set[str] = set()
    for batch in _batched(valid):
        try:
            found, absent = _properties(client, batch)
            properties.update(found)
            missing.update(absent)
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError):
            transient.update(batch)

    synonym_map: dict[int, list[str]] = {}
    cids = sorted({int(item["CID"]) for item in properties.values() if item.get("CID")})
    for batch in _batched(cids):
        try:
            synonym_map.update(_synonyms(client, batch))
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError):
            # Identity remains resolved even when the optional synonym expansion failed.
            continue

    retrieved_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    identities = []
    for key in keys:
        item = properties.get(key)
        if item:
            cid = int(item["CID"])
            title = str(item.get("Title") or "").strip() or None
            iupac = str(item.get("IUPACName") or "").strip() or None
            identities.append(
                {
                    "inchikey": key,
                    "preferred_name": title or iupac,
                    "pubchem_cid": cid,
                    "iupac_name": iupac,
                    "synonyms": synonym_map.get(cid, []),
                    "canonical_smiles": (
                        item.get("ConnectivitySMILES") or item.get("CanonicalSMILES")
                    ),
                    "isomeric_smiles": item.get("SMILES") or item.get("IsomericSMILES"),
                    "resolution_status": "resolved",
                    "failure_category": None,
                    "retrieved_at": retrieved_at,
                    "source": "PubChem PUG REST",
                }
            )
        else:
            category = (
                "invalid_inchikey"
                if key in invalid
                else "transient_error"
                if key in transient
                else "not_found"
            )
            identities.append(
                {
                    "inchikey": key,
                    "preferred_name": None,
                    "pubchem_cid": None,
                    "iupac_name": None,
                    "synonyms": [],
                    "canonical_smiles": None,
                    "isomeric_smiles": None,
                    "resolution_status": "unresolved",
                    "failure_category": category,
                    "retrieved_at": retrieved_at,
                    "source": "PubChem PUG REST",
                }
            )

    resolved = sum(item["resolution_status"] == "resolved" for item in identities)
    canonical = json.dumps(identities, sort_keys=True, separators=(",", ":")).encode()
    failures = {
        name: sum(item["failure_category"] == name for item in identities)
        for name in ("not_found", "invalid_inchikey", "transient_error")
    }
    return {
        "schema_version": "1.0.0",
        "artifact_version": artifact_version,
        "generated_at": retrieved_at,
        "source": {
            "name": "PubChem PUG REST",
            "base_url": PUBCHEM_BASE,
            "request_rate_limit_per_second": client.requests_per_second,
            "user_agent": USER_AGENT,
        },
        "map_inputs": ["models/ER/explore/umap.json", "models/AR/explore/umap.json"],
        "statistics": {
            "total_unique_inchikeys": len(identities),
            "resolved": resolved,
            "unresolved": len(identities) - resolved,
            "resolved_percentage": (
                round(100.0 * resolved / len(identities), 2) if identities else 0.0
            ),
            "failed_lookup_categories": failures,
            "pubchem_requests": client.request_count,
        },
        "records_sha256": hashlib.sha256(canonical).hexdigest(),
        "identities": identities,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/identities/v1/reference_identities.json"),
    )
    parser.add_argument("--artifact-version", default=datetime.now(UTC).date().isoformat())
    parser.add_argument("--requests-per-second", type=float, default=3.0)
    args = parser.parse_args()
    root = args.repo_root.resolve()
    output = args.output if args.output.is_absolute() else root / args.output
    artifact = build_artifact(
        root,
        PubChemClient(requests_per_second=args.requests_per_second),
        artifact_version=args.artifact_version,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(output)
    print(json.dumps(artifact["statistics"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
