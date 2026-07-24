"""Explicitly stage the reviewed public invitroDB cache outside bounded discovery."""

from __future__ import annotations

import argparse
from pathlib import Path

from endoscan_workflows.toxcast_public_activity import (
    TOXCAST_ARCHIVE_SHA256,
    HttpxArchiveDownloadTransport,
    ToxCastActivityNormalizer,
    ToxCastArchiveCache,
    ToxCastArchiveProcessor,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stage and normalize the reviewed multi-gigabyte invitroDB v4.3 archive. "
            "This is never invoked by bounded discovery."
        )
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=Path(".endoscan/provider-cache"),
    )
    parser.add_argument("--confirm-large-download", action="store_true")
    args = parser.parse_args()
    if not args.confirm_large_download:
        parser.error(
            "The reviewed archive is multi-gigabyte; pass --confirm-large-download only "
            "after explicit operator approval."
        )
    cache = ToxCastArchiveCache(
        args.cache_root,
        HttpxArchiveDownloadTransport(),
        expected_archive_sha256=TOXCAST_ARCHIVE_SHA256,
    )
    stage = cache.stage(maximum_full_download_retries=0)
    processor = ToxCastArchiveProcessor(cache)
    inventory = processor.enumerate(stage)
    verified = processor.verify_and_extract_roles(stage, inventory)
    normalized = ToxCastActivityNormalizer(cache).normalize(verified)
    print(
        "ToxCast cache ready: "
        f"release={stage.identity.release_id} "
        f"bundle={normalized.bundle_fingerprint} "
        f"sqlite_sha256={normalized.sqlite.sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
