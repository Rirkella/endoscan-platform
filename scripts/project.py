"""Cross-platform command surface for EndoScan contributors and CI.

Run ``python scripts/project.py --list`` to see the supported tasks. The
interpreter executing this file is reused so the command works with ``uv run
python``, an activated virtual environment, or an explicit virtualenv Python.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"
PYTHON = sys.executable
PYTEST_COMMON = (
    f"--basetemp={Path(tempfile.gettempdir()) / 'endoscan-pytest'}",
    "-p",
    "no:cacheprovider",
)

MYPY_TARGETS = (
    "scripts/project.py",
    "scripts/verify_repository.py",
    "packages/endoscan_core/endoscan_core/registry/schema.py",
    "packages/endoscan_core/endoscan_core/registry/store.py",
    "packages/endoscan_core/endoscan_core/inference/schema_validation.py",
    "packages/endoscan_workflows/endoscan_workflows/models.py",
    "packages/endoscan_workflows/endoscan_workflows/state_machine.py",
    "packages/endoscan_workflows/endoscan_workflows/artifacts.py",
    "packages/endoscan_workflows/endoscan_workflows/database.py",
    "services/api/endoscan_api/app.py",
)

TASK_HELP = {
    "setup": "Install locked Python and frontend dependencies.",
    "dev": "Run the API and Vite development servers together.",
    "api": "Run the FastAPI development server on port 8001.",
    "web": "Run the Vite development server.",
    "test": "Run the full Python and frontend test suites.",
    "test-workflows": "Run workflow-package and workflow regression tests.",
    "test-api": "Run API service tests.",
    "test-backend": "Run all Python tests.",
    "test-frontend": "Run the frontend test suite.",
    "lint": "Run Ruff lint checks.",
    "format": "Apply Ruff formatting.",
    "format-check": "Check Ruff formatting without changing files.",
    "typecheck": "Run bounded Python mypy and full TypeScript checks.",
    "build": "Create the production frontend bundle.",
    "demo": "Run both fully offline endpoint lifecycle demos.",
    "demo-tr": "Run the offline thyroid-receptor endpoint demo.",
    "demo-dna-damage": "Run the offline DNA-damage endpoint demo.",
    "docs-check": "Validate canonical docs, links, diagrams, JSON, and TOML.",
    "hygiene-check": "Audit tracked files for runtime debris and secret/local paths.",
    "verify": "Run every offline merge gate used by CI.",
}


def _tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise SystemExit(
            f"Required tool '{name}' is not on PATH. See docs/DEVELOPMENT.md for setup."
        )
    return executable


def _npm(*arguments: str) -> list[str]:
    return [_tool("npm"), *arguments]


def _python_module(module: str, *arguments: str) -> list[str]:
    return [PYTHON, "-m", module, *arguments]


def commands_for(task: str) -> list[tuple[Sequence[str], Path]]:
    """Return subprocess commands for a finite task."""
    if task == "setup":
        return [
            ([_tool("uv"), "sync", "--frozen"], ROOT),
            (_npm("ci"), WEB),
        ]
    if task == "test-backend":
        return [(_python_module("pytest", *PYTEST_COMMON), ROOT)]
    if task == "test-workflows":
        return [
            (
                _python_module(
                    "pytest",
                    *PYTEST_COMMON,
                    "packages/endoscan_workflows",
                ),
                ROOT,
            )
        ]
    if task == "test-api":
        return [
            (
                _python_module("pytest", *PYTEST_COMMON, "services/api/tests"),
                ROOT,
            )
        ]
    if task == "test-frontend":
        return [(_npm("test"), WEB)]
    if task == "test":
        return commands_for("test-backend") + commands_for("test-frontend")
    if task == "lint":
        return [(_python_module("ruff", "check", "."), ROOT)]
    if task == "format":
        return [(_python_module("ruff", "format", "."), ROOT)]
    if task == "format-check":
        return [(_python_module("ruff", "format", "--check", "."), ROOT)]
    if task == "typecheck":
        return [
            (_python_module("mypy", *MYPY_TARGETS), ROOT),
            (_npm("run", "typecheck"), WEB),
        ]
    if task == "build":
        return [(_npm("run", "build"), WEB)]
    if task == "demo-tr":
        return [([PYTHON, "scripts/run_offline_endpoint_demo.py", "tr_receptor"], ROOT)]
    if task == "demo-dna-damage":
        return [([PYTHON, "scripts/run_offline_endpoint_demo.py", "dna_damage"], ROOT)]
    if task == "demo":
        return commands_for("demo-tr") + commands_for("demo-dna-damage")
    if task == "docs-check":
        return [([PYTHON, "scripts/verify_repository.py", "docs"], ROOT)]
    if task == "hygiene-check":
        return [([PYTHON, "scripts/verify_repository.py", "hygiene"], ROOT)]
    if task == "verify":
        return (
            commands_for("lint")
            + commands_for("format-check")
            + commands_for("typecheck")
            + commands_for("test")
            + commands_for("build")
            + commands_for("docs-check")
            + commands_for("hygiene-check")
            + commands_for("demo")
        )
    raise SystemExit(f"Task '{task}' does not have a finite command sequence.")


def _server_command(task: str) -> tuple[list[str], Path]:
    if task == "api":
        return (
            _python_module(
                "uvicorn",
                "endoscan_api.app:create_app",
                "--factory",
                "--app-dir",
                "services/api",
                "--host",
                "127.0.0.1",
                "--port",
                "8001",
                "--reload",
            ),
            ROOT,
        )
    if task == "web":
        return (_npm("run", "dev"), WEB)
    raise AssertionError(task)


def _display(command: Sequence[str], cwd: Path) -> None:
    relative = cwd.relative_to(ROOT) if cwd != ROOT else Path(".")
    print(f"[{relative}] {' '.join(command)}", flush=True)


def _run(command: Sequence[str], cwd: Path, *, dry_run: bool) -> None:
    _display(command, cwd)
    if not dry_run:
        subprocess.run(command, cwd=cwd, check=True)


def run_dev(*, dry_run: bool) -> None:
    commands = [_server_command("api"), _server_command("web")]
    if dry_run:
        for command, cwd in commands:
            _display(command, cwd)
        return
    processes: list[subprocess.Popen[bytes]] = []
    try:
        for command, cwd in commands:
            _display(command, cwd)
            processes.append(subprocess.Popen(command, cwd=cwd))
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        failed = next((process.returncode for process in processes if process.returncode), 0)
        if failed:
            raise SystemExit(failed)
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", nargs="?", choices=sorted(TASK_HELP))
    parser.add_argument("--list", action="store_true", help="List supported tasks and exit.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print subprocesses without executing them."
    )
    arguments = parser.parse_args()
    if arguments.list or arguments.task is None:
        for task, description in TASK_HELP.items():
            print(f"{task:20} {description}")
        return
    if arguments.task == "dev":
        run_dev(dry_run=arguments.dry_run)
        return
    if arguments.task in {"api", "web"}:
        server_command, cwd = _server_command(arguments.task)
        _run(server_command, cwd, dry_run=arguments.dry_run)
        return
    for task_command, cwd in commands_for(arguments.task):
        _run(task_command, cwd, dry_run=arguments.dry_run)


if __name__ == "__main__":
    main()
