"""``python -m endoscan_jobs`` — run a named job in the container, write logs/artifacts.

    python -m endoscan_jobs list
    python -m endoscan_jobs run coverage AR --source-zip <Data.zip> --lincs-dir <dir>
    python -m endoscan_jobs run <job> <target> [--data-root DIR] [--run-id ID]

Exit codes: 0 ran (incl. failed_qc verdict), 2 job error, 3 usage, 4 environment.
"""

from __future__ import annotations

import argparse
import os
import sys

from .runner import EXIT_ENV_ERROR, EXIT_USAGE, JOBS, run_job
from .storage import LocalStorage

DEFAULT_DATA_ROOT = os.environ.get("ENDOSCAN_DATA_ROOT", "./.endoscan-data")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="endoscan_jobs", description="EndoScan job runner")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list available jobs")

    run = sub.add_parser("run", help="run a named job")
    run.add_argument("job", help="job name (e.g. coverage)")
    run.add_argument("target", help="endpoint/target id (e.g. AR)")
    run.add_argument("--data-root", default=DEFAULT_DATA_ROOT, help="persistent storage root")
    run.add_argument("--run-id", default=None, help="explicit run id (else generated)")
    run.add_argument("--source-zip", default=None, help="local CoMPARA Data.zip (offline)")
    run.add_argument("--lincs-dir", default=None, help="dir with sig_info/pert_info (offline)")
    run.add_argument(
        "--mode",
        default=None,
        help="coverage mode: binding|agonist|antagonist|functional_modulation|"
        "broad_any_activity (default functional_modulation = agonist∪antagonist)",
    )
    run.add_argument("--force-table", default=None)
    run.add_argument("--force-call-col", default=None)
    run.add_argument("--force-struct-col", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 0 for --help/-h (success); non-zero for a parse error (usage).
        return 0 if exc.code in (0, None) else EXIT_USAGE

    if args.command == "list":
        print("available jobs:", ", ".join(sorted(JOBS)))
        return 0

    if args.job not in JOBS:
        print(f"unknown job {args.job!r}; available: {sorted(JOBS)}", file=sys.stderr)
        return EXIT_USAGE

    try:
        storage = LocalStorage(args.data_root)
    except OSError as exc:
        print(f"storage root unavailable ({args.data_root}): {exc}", file=sys.stderr)
        return EXIT_ENV_ERROR

    options = {
        "source_zip": args.source_zip,
        "lincs_dir": args.lincs_dir,
        "mode": args.mode,
        "force_table": args.force_table,
        "force_call_col": args.force_call_col,
        "force_struct_col": args.force_struct_col,
    }
    result = run_job(args.job, args.target, storage=storage, run_id=args.run_id, options=options)
    print(f"status={result.status} exit={result.exit_code} verdict={result.verdict}")
    print(f"summary: {result.summary or result.error}")
    print(f"report: {result.report_key} | log: {result.log_key}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
