"""Run one complete deterministic endpoint build in a disposable local root."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from endoscan_workflows.offline_demo import execute_offline_demo


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", choices=["tr_receptor", "dna_damage"])
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="Disposable output root (default: .builds/offline-endpoint-demo/<fixture>).",
    )
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    output = args.output_root or repo_root / ".builds" / "offline-endpoint-demo" / args.fixture
    result = execute_offline_demo(repo_root, output, args.fixture)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
