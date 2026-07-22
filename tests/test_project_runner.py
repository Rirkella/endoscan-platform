from __future__ import annotations

import importlib.util
from pathlib import Path


def _project_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "project.py"
    spec = importlib.util.spec_from_file_location("endoscan_project_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_tool_resolution_prefers_executable_launcher(monkeypatch) -> None:
    project = _project_module()
    observed: list[str] = []

    def recording_which(name: str) -> str | None:
        observed.append(name)
        return {
            "npm.cmd": r"C:\Program Files\nodejs\npm.cmd",
            "npm": r"C:\Program Files\nodejs\npm",
        }.get(name)

    monkeypatch.setattr(project.sys, "platform", "win32")
    monkeypatch.setattr(project.shutil, "which", recording_which)

    assert project._tool("npm") == r"C:\Program Files\nodejs\npm.cmd"
    assert observed == ["npm.cmd"]
