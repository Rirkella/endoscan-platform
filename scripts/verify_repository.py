"""Offline repository documentation and hygiene checks used locally and in CI."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DOCS = (
    "README.md",
    "docs/ARCHITECTURE.md",
    "docs/ENDPOINT_BUILDING_WORKFLOW.md",
    "docs/PROVIDER_ARCHITECTURE.md",
    "docs/DATA_MODEL_AND_ARTIFACTS.md",
    "docs/ADMIN_CONSOLE_GUIDE.md",
    "docs/OFFLINE_DEMO.md",
    "docs/DEVELOPMENT.md",
    "docs/TESTING.md",
    "docs/REPRODUCIBILITY.md",
    "docs/SCIENTIFIC_LIMITATIONS.md",
    "docs/SECURITY.md",
    "docs/AI_ASSISTED_DEVELOPMENT.md",
    "docs/CONTRIBUTING.md",
    "docs/STATUS_AND_ROADMAP.md",
    "docs/decisions/README.md",
)
REQUIRED_TASKS = {
    "setup",
    "dev",
    "api",
    "web",
    "test",
    "test-workflows",
    "test-api",
    "test-backend",
    "test-frontend",
    "lint",
    "format",
    "format-check",
    "typecheck",
    "build",
    "demo",
    "demo-tr",
    "demo-dna-damage",
    "docs-check",
    "hygiene-check",
    "verify",
}
FORBIDDEN_TRACKED_PARTS = {
    ".builds",
    ".endoscan",
    ".endoscan-data",
    ".provider-cache",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "logs",
    "node_modules",
    "reports",
    "__pycache__",
}
FORBIDDEN_PUBLIC_ROOT_ITEMS = {
    "CLAUDE.md",
    "PROJECT_RULES.md",
    "agent",
    "legacy",
    "notebooks",
    "reports",
}
SECRET_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[opusr]_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
LOCAL_PATH_PATTERNS = (
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9.])/" + r"Users/[^/\s]+"),
    re.compile(r"(?<![A-Za-z0-9.])/" + r"home/[^/\s]+"),
)
MISLEADING_AGENT_PATTERNS = (
    re.compile("fa" + "ke", re.IGNORECASE),
    re.compile("cannot " + "publish production endpoints", re.IGNORECASE),
    re.compile(r"(?:placeholder|simulated|demo-only|prototype-only)[-_ ]agent", re.IGNORECASE),
)
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def _tracked_files() -> list[Path]:
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT, text=False).decode(
        "utf-8"
    )
    return [ROOT / value for value in output.rstrip("\0").split("\0") if value]


def _markdown_files() -> list[Path]:
    paths = [ROOT / path for path in CANONICAL_DOCS]
    decisions = sorted((ROOT / "docs" / "decisions").glob("*.md"))
    tracked = [path for path in _tracked_files() if path.suffix.lower() == ".md"]
    return sorted(set(paths + decisions + tracked))


def _check_docs() -> list[str]:
    failures: list[str] = []
    for relative in CANONICAL_DOCS:
        if not (ROOT / relative).is_file():
            failures.append(f"missing canonical document: {relative}")

    mermaid_count = 0
    for path in _markdown_files():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        opening = text.count("```mermaid")
        mermaid_count += opening
        if text.count("```") % 2:
            failures.append(f"unbalanced fenced code block: {path.relative_to(ROOT)}")
        for raw_target in MARKDOWN_LINK.findall(text):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            if (
                not target
                or target.startswith(("http://", "https://", "mailto:", "#"))
                or "{" in target
            ):
                continue
            target_path = target.split("#", 1)[0]
            resolved = (path.parent / target_path).resolve()
            if not resolved.exists():
                failures.append(f"broken relative link in {path.relative_to(ROOT)}: {raw_target}")
    if mermaid_count < 8:
        failures.append(f"expected at least 8 Mermaid diagrams, found {mermaid_count}")

    canonical_text = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8")
        for relative in CANONICAL_DOCS
        if (ROOT / relative).is_file()
    )
    obsolete_patterns = {
        "feature branch presented as current": r"codex/[a-z0-9_-]+",
        "obsolete live-validation claim": r"fully live validated",
        "obsolete modality invariant": r"one assay per modality",
        "obsolete discovery completion": r"top-N discovery completion",
    }
    for label, pattern in obsolete_patterns.items():
        if re.search(pattern, canonical_text, flags=re.IGNORECASE):
            failures.append(f"{label} appears in canonical documentation")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if "`main` is the authoritative development branch" not in readme:
        failures.append("README does not identify main as the authoritative branch")

    project_script = (ROOT / "scripts" / "project.py").read_text(encoding="utf-8")
    missing_tasks = sorted(task for task in REQUIRED_TASKS if f'"{task}"' not in project_script)
    if missing_tasks:
        failures.append(f"command surface is missing tasks: {', '.join(missing_tasks)}")

    for path in _tracked_files():
        if path.suffix == ".json":
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                failures.append(f"invalid JSON {path.relative_to(ROOT)}: {error}")
        elif path.suffix == ".toml":
            try:
                tomllib.loads(path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
                failures.append(f"invalid TOML {path.relative_to(ROOT)}: {error}")
        elif path.suffix.lower() in {".yaml", ".yml", ".cff"}:
            try:
                yaml.safe_load(path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, yaml.YAMLError) as error:
                failures.append(f"invalid YAML {path.relative_to(ROOT)}: {error}")

    compose_path = ROOT / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    if "jobs" not in compose.get("services", {}):
        failures.append("docker-compose.yml does not define the documented jobs service")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    if "COPY packages/ ./packages/" not in dockerfile:
        failures.append("Dockerfile does not copy the documented job package workspace")
    if "COPY agent/" in dockerfile or "COPY legacy/" in dockerfile:
        failures.append("Dockerfile references an obsolete public-root package")

    dvc_pointer_path = ROOT / "data" / "staged" / "er" / "lincs.parquet.dvc"
    dvc_pointer = yaml.safe_load(dvc_pointer_path.read_text(encoding="utf-8"))
    outputs = dvc_pointer.get("outs", []) if isinstance(dvc_pointer, dict) else []
    if not outputs or outputs[0].get("path") != "lincs.parquet" or not outputs[0].get("md5"):
        failures.append("LINCS DVC pointer is missing its reviewed path or content hash")
    if not (ROOT / ".dvc" / "config").is_file() or not (ROOT / ".dvcignore").is_file():
        failures.append("DVC metadata is incomplete")
    return failures


def _check_hygiene() -> list[str]:
    failures: list[str] = []
    tracked = _tracked_files()
    tracked_roots = {path.relative_to(ROOT).parts[0] for path in tracked}
    for name in sorted(FORBIDDEN_PUBLIC_ROOT_ITEMS):
        if name in tracked_roots:
            failures.append(f"obsolete or generated public-root item remains: {name}")

    tracked_ignored = subprocess.check_output(
        ["git", "ls-files", "-ci", "--exclude-standard"], cwd=ROOT, text=True
    ).splitlines()
    for relative in tracked_ignored:
        failures.append(f"tracked file is covered by an ignore rule: {relative}")

    text_suffixes = {
        ".css",
        ".cff",
        ".html",
        ".js",
        ".json",
        ".md",
        ".py",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".yaml",
        ".yml",
    }
    for path in tracked:
        tracked_relative = path.relative_to(ROOT)
        if not path.exists():
            continue
        if any(part in FORBIDDEN_TRACKED_PARTS for part in tracked_relative.parts):
            failures.append(f"tracked runtime/cache path: {tracked_relative}")
        if tracked_relative.parts[:2] == ("docs", "agents"):
            failures.append(
                f"superseded agent planning artifact remains public: {tracked_relative}"
            )
        if path.name == ".gitkeep":
            failures.append(f"stale tracked directory placeholder: {tracked_relative}")
        if path.stat().st_size > 10 * 1024 * 1024:
            failures.append(f"tracked file exceeds 10 MiB review threshold: {tracked_relative}")
        if path.suffix.lower() not in text_suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                failures.append(f"secret-like value in tracked file: {tracked_relative}")
        for pattern in LOCAL_PATH_PATTERNS:
            if pattern.search(text):
                failures.append(f"machine-local absolute path in tracked file: {tracked_relative}")
        for pattern in MISLEADING_AGENT_PATTERNS:
            if pattern.search(text):
                failures.append(f"misleading agent terminology in tracked file: {tracked_relative}")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scope", choices=("docs", "hygiene", "all"), nargs="?", default="all")
    arguments = parser.parse_args()
    failures: list[str] = []
    if arguments.scope in {"docs", "all"}:
        failures.extend(_check_docs())
    if arguments.scope in {"hygiene", "all"}:
        failures.extend(_check_hygiene())
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        raise SystemExit(1)
    print(f"Repository {arguments.scope} checks passed.")


if __name__ == "__main__":
    main()
