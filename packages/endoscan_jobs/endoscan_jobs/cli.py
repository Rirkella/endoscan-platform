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
    run.add_argument(
        "--contexts",
        default=None,
        help="coverage cell-line contexts: ';'-separated 'NAME:CELL1,CELL2' entries or bare "
        "preset names (default presets include 'VCaP+A549 (mixed)')",
    )
    run.add_argument("--force-table", default=None)
    run.add_argument("--force-call-col", default=None)
    run.add_argument("--force-struct-col", default=None)
    # extract_signatures job: the LINCS gctx slice (server stages the real files; CI uses
    # a synthetic gctx fixture). The cell-line set is parameterized (not hardcoded).
    run.add_argument("--gctx-path", default=None, help="local LINCS Level-5 .gctx (server only)")
    run.add_argument(
        "--gene-info", default=None, help="LINCS gene_info.txt (else found under --lincs-dir)"
    )
    run.add_argument(
        "--cell-lines",
        default=None,
        help="comma-separated cell lines for extraction (e.g. 'VCAP,A549'; default MCF7,A549)",
    )
    run.add_argument("--slice-batch", type=int, default=None, help="sig_ids per hyperslab read")
    run.add_argument("--rdcc-bytes", type=int, default=None, help="HDF5 chunk-cache bound (bytes)")
    # build_explore_map job: a SEEDED UMAP over curated/<TARGET>/signatures.parquet. All params
    # are recorded in the manifest; labels are OPTIONAL (uncoloured map if absent, never faked).
    run.add_argument("--labels-csv", default=None, help="optional compound_id->label CSV to colour")
    run.add_argument(
        "--labels-id-col", default=None, help="labels CSV id column (default compound_id)"
    )
    run.add_argument(
        "--labels-label-col", default=None, help="labels CSV label column (default label)"
    )
    run.add_argument("--umap-seed", type=int, default=None, help="UMAP random_state (default 42)")
    run.add_argument(
        "--umap-neighbors", type=int, default=None, help="UMAP n_neighbors (default 15)"
    )
    run.add_argument(
        "--umap-min-dist", type=float, default=None, help="UMAP min_dist (default 0.1)"
    )
    run.add_argument("--umap-metric", default=None, help="UMAP/kNN metric (default euclidean)")
    run.add_argument("--nn-k", type=int, default=None, help="k for the domain metric (default 5)")
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
        "contexts": args.contexts,
        "force_table": args.force_table,
        "force_call_col": args.force_call_col,
        "force_struct_col": args.force_struct_col,
        "gctx_path": args.gctx_path,
        "gene_info": args.gene_info,
        "cell_lines": args.cell_lines,
        "slice_batch": args.slice_batch,
        "rdcc_bytes": args.rdcc_bytes,
        "labels_csv": args.labels_csv,
        "labels_id_col": args.labels_id_col,
        "labels_label_col": args.labels_label_col,
        "umap_seed": args.umap_seed,
        "umap_neighbors": args.umap_neighbors,
        "umap_min_dist": args.umap_min_dist,
        "umap_metric": args.umap_metric,
        "nn_k": args.nn_k,
    }
    result = run_job(args.job, args.target, storage=storage, run_id=args.run_id, options=options)
    print(f"status={result.status} exit={result.exit_code} verdict={result.verdict}")
    print(f"summary: {result.summary or result.error}")
    print(f"report: {result.report_key} | log: {result.log_key}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
