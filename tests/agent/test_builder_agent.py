"""M5 Builder Agent: orchestration, the promote chokepoint, and the gate/approval
boundary — all offline on the staged ER fixture (mirrors tests/training/test_pipeline_er.py).

Load-bearing guarantees proven here:
- train/register is UNREACHABLE without an issued approval token (structural);
- a FAILED gate + a present well-formed token does NOT train/register (necessary-but-
  not-sufficient — the single most important test);
- registration refuses when any artifact / limitations is missing;
- thresholds come from yaml and the agent has no path to mutate them;
- a FAILED build leaves a workspace but never mutates the registry;
- rejection is terminal-for-registration;
- the agent calls the existing tools (zero reimplemented science);
- the outer (agent) and inner (run_pipeline) gate verdicts are identical (determinism);
- tampering the fingerprinted gate report invalidates the token.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent import (
    ApprovalRequiredError,
    ApprovalToken,
    BuildRecipe,
    BuildState,
    BuildStateError,
    GateDivergenceError,
    GateFailedError,
    approve,
    get_gate_report,
    get_status,
    promote,
    reject,
    start_build,
)
from agent import builder_agent as ba
from endoscan_core.datasets import load_gates
from endoscan_core.registry import get_endpoint

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_SRC = REPO_ROOT / "agent"
RUNNER = "pipelines/endpoints/ER/run.py"


def _endpoints(output_root: Path) -> list:
    return json.loads((output_root / "registry" / "models" / "endpoints.json").read_text())[
        "endpoints"
    ]


def _forge_token(build_id: str, fingerprint: str) -> ApprovalToken:
    """A well-formed token NOT issued by approve() — used to prove the gate fires first."""
    return ApprovalToken(
        build_id=build_id,
        gate_fingerprint=fingerprint,
        approver="attacker",
        value="forged",
        issued_at=datetime.now(UTC),
    )


# --- happy path -----------------------------------------------------------------------


def test_pass_then_approve_then_promote_registers(pass_recipe, builds_root, output_root):
    record = start_build(pass_recipe, repo_root=REPO_ROOT, builds_root=builds_root)
    assert record.state is BuildState.awaiting_approval
    assert get_gate_report(record.build_id, builds_root=builds_root).passed is True

    token = approve(record.build_id, "alice", builds_root=builds_root)
    result = promote(
        record.build_id,
        token,
        repo_root=REPO_ROOT,
        output_root=output_root,
        builds_root=builds_root,
    )
    assert result.registered is True
    entry = get_endpoint("ER", repo_root=output_root)
    assert entry.status.value in {"experimental", "validated_mvp"}
    assert entry.source_refs  # provenance derived by run_pipeline, not the agent
    assert get_status(record.build_id, builds_root=builds_root).state is BuildState.registered


def _approve_and_promote(recipe, builds_root, output_root):
    record = start_build(recipe, repo_root=REPO_ROOT, builds_root=builds_root)
    token = approve(record.build_id, "alice", builds_root=builds_root)
    return promote(
        record.build_id, token,
        repo_root=REPO_ROOT, output_root=output_root, builds_root=builds_root,
    )  # fmt: skip


def test_repromote_overwrites_registered_endpoint_without_manual_edit(
    pass_recipe, builds_root, output_root
):
    # A re-promote of an already-registered (non-frozen) endpoint overwrites it IN PLACE —
    # no manual endpoints.json editing, no duplicate-id error (the bug this fixes).
    _approve_and_promote(pass_recipe, builds_root, output_root)
    first = get_endpoint("ER", repo_root=output_root)
    assert first.updated_at is None  # first registration -> no re-register stamp

    result = _approve_and_promote(pass_recipe, builds_root, output_root)  # SECOND promote
    assert result.registered is True
    again = get_endpoint("ER", repo_root=output_root)
    assert again.updated_at is not None  # audit trail: re-registered
    assert again.created_at == first.created_at  # original registration time preserved
    assert len(_endpoints(output_root)) == 1  # overwritten, not duplicated


def test_repromote_without_token_is_still_blocked(pass_recipe, builds_root, output_root):
    # Re-registration is NOT a backdoor: a second promote WITHOUT a token is refused by the
    # SAME chokepoint, and the already-registered entry is left untouched.
    _approve_and_promote(pass_recipe, builds_root, output_root)
    first = get_endpoint("ER", repo_root=output_root)

    record2 = start_build(pass_recipe, repo_root=REPO_ROOT, builds_root=builds_root)
    with pytest.raises(ApprovalRequiredError):
        promote(
            record2.build_id, None,
            repo_root=REPO_ROOT, output_root=output_root, builds_root=builds_root,
        )  # fmt: skip
    after = get_endpoint("ER", repo_root=output_root)
    assert after.updated_at is None and after.created_at == first.created_at  # untouched


# --- THE chokepoint: necessary-but-not-sufficient -------------------------------------


def test_promote_without_token_blocks(pass_recipe, builds_root, output_root):
    record = start_build(pass_recipe, repo_root=REPO_ROOT, builds_root=builds_root)
    with pytest.raises(ApprovalRequiredError):
        promote(
            record.build_id,
            None,
            repo_root=REPO_ROOT,
            output_root=output_root,
            builds_root=builds_root,
        )
    assert _endpoints(output_root) == []  # registry untouched


def test_fingerprint_valid_token_still_needs_issuance(pass_recipe, builds_root, output_root):
    # A token whose fingerprint matches but was NOT issued by approve() is refused.
    record = start_build(pass_recipe, repo_root=REPO_ROOT, builds_root=builds_root)
    fp = get_gate_report(record.build_id, builds_root=builds_root).fingerprint()
    with pytest.raises(ApprovalRequiredError):
        promote(
            record.build_id,
            _forge_token(record.build_id, fp),
            repo_root=REPO_ROOT,
            output_root=output_root,
            builds_root=builds_root,
        )
    assert _endpoints(output_root) == []


def test_failed_gate_with_present_token_does_not_register(fail_recipe, builds_root, output_root):
    # ★ The single most important test: a gated_fail build + a present, well-formed,
    # fingerprint-matching approval token -> GateFailedError, registry unchanged.
    record = start_build(fail_recipe, repo_root=REPO_ROOT, builds_root=builds_root)
    assert record.state is BuildState.failed_qc
    report = get_gate_report(record.build_id, builds_root=builds_root)
    assert report.passed is False

    token = _forge_token(record.build_id, report.fingerprint())  # well-formed, matches the report
    with pytest.raises(GateFailedError):
        promote(
            record.build_id,
            token,
            repo_root=REPO_ROOT,
            output_root=output_root,
            builds_root=builds_root,
        )
    assert _endpoints(output_root) == []
    assert not (output_root / "models" / "ER" / "model.pkl").exists()


def test_cannot_approve_a_failed_build(fail_recipe, builds_root):
    record = start_build(fail_recipe, repo_root=REPO_ROOT, builds_root=builds_root)
    with pytest.raises(BuildStateError):
        approve(record.build_id, "alice", builds_root=builds_root)


# --- determinism between the two enforcement layers (addition 1) ----------------------


def test_outer_and_inner_gate_verdicts_are_identical(pass_recipe, builds_root, output_root):
    # Outer: two independent builds of the same recipe produce a byte-identical gate
    # report (passed + summary + per-check observed/threshold/passed) -> the gate
    # computation is deterministic, so run_pipeline's INNER verdict (same functions, same
    # fixtures, same seed) is necessarily identical.
    r1 = start_build(pass_recipe, repo_root=REPO_ROOT, builds_root=builds_root)
    r2 = start_build(pass_recipe, repo_root=REPO_ROOT, builds_root=builds_root)
    g1 = get_gate_report(r1.build_id, builds_root=builds_root)
    g2 = get_gate_report(r2.build_id, builds_root=builds_root)
    assert g1.passed == g2.passed and g1.summary == g2.summary
    assert g1.checks == g2.checks  # per-check observed/threshold/passed identical
    assert g1.fingerprint() == g2.fingerprint()

    # Inner: run_pipeline exposes passed + summary; both equal the outer verdict.
    token = approve(r1.build_id, "alice", builds_root=builds_root)
    result = promote(
        r1.build_id, token, repo_root=REPO_ROOT, output_root=output_root, builds_root=builds_root
    )
    assert result.gate_passed == g1.passed
    assert result.gate_summary == g1.summary


def test_promote_raises_on_inner_outer_divergence(
    pass_recipe, builds_root, output_root, monkeypatch
):
    # If a gated_pass build comes back registered=False from run_pipeline (inner verdict
    # disagreed), promote RAISES rather than silently no-op'ing; registry untouched.
    record = start_build(pass_recipe, repo_root=REPO_ROOT, builds_root=builds_root)
    token = approve(record.build_id, "alice", builds_root=builds_root)

    runner = ba._load_runner(REPO_ROOT / RUNNER)
    diverged = runner.PipelineResult(
        endpoint_id="ER", gate_passed=True, approved=True, trained=False, registered=False
    )
    monkeypatch.setattr(runner, "run_pipeline", lambda *a, **k: diverged)
    with pytest.raises(GateDivergenceError):
        promote(
            record.build_id,
            token,
            repo_root=REPO_ROOT,
            output_root=output_root,
            builds_root=builds_root,
        )
    assert _endpoints(output_root) == []  # stub never wrote the registry


# --- single source of truth: tamper invalidates the token (addition 2) ----------------


def test_tampering_gate_report_after_approve_invalidates_token(
    pass_recipe, builds_root, output_root
):
    record = start_build(pass_recipe, repo_root=REPO_ROOT, builds_root=builds_root)
    token = approve(record.build_id, "alice", builds_root=builds_root)

    # Flip the authoritative pass/fail bit in the fingerprinted report.
    gate_path = builds_root / record.build_id / "gate_report.json"
    data = json.loads(gate_path.read_text())
    data["passed"] = not data["passed"]
    gate_path.write_text(json.dumps(data))

    with pytest.raises(ApprovalRequiredError):  # fingerprint mismatch
        promote(
            record.build_id,
            token,
            repo_root=REPO_ROOT,
            output_root=output_root,
            builds_root=builds_root,
        )
    assert _endpoints(output_root) == []


# --- failed build leaves a workspace, registry untouched ------------------------------


def test_failed_build_leaves_workspace_but_no_registry_mutation(
    fail_recipe, builds_root, output_root
):
    record = start_build(fail_recipe, repo_root=REPO_ROOT, builds_root=builds_root)
    ws = builds_root / record.build_id
    assert (ws / "gate_report.json").is_file() and (ws / "build_status.json").is_file()
    assert _endpoints(output_root) == []  # the build never touched the registry
    assert not (output_root / "models" / "ER").exists()


# --- rejection is terminal-for-registration -------------------------------------------


def test_rejection_is_terminal(pass_recipe, builds_root, output_root):
    record = start_build(pass_recipe, repo_root=REPO_ROOT, builds_root=builds_root)
    reject(record.build_id, "alice", builds_root=builds_root, reason="not now")
    assert get_status(record.build_id, builds_root=builds_root).state is BuildState.rejected
    # A token forged over the (passed) report cannot promote a rejected build.
    fp = get_gate_report(record.build_id, builds_root=builds_root).fingerprint()
    with pytest.raises((BuildStateError, ApprovalRequiredError)):
        promote(
            record.build_id,
            _forge_token(record.build_id, fp),
            repo_root=REPO_ROOT,
            output_root=output_root,
            builds_root=builds_root,
        )
    assert _endpoints(output_root) == []


# --- registration requires all artifacts + derivable limitations ----------------------


def test_registration_requires_all_artifacts(pass_recipe, builds_root, output_root):
    record = start_build(pass_recipe, repo_root=REPO_ROOT, builds_root=builds_root)
    token = approve(record.build_id, "alice", builds_root=builds_root)
    promote(
        record.build_id,
        token,
        repo_root=REPO_ROOT,
        output_root=output_root,
        builds_root=builds_root,
    )
    # Remove a required artifact; the defense-in-depth verifier must refuse.
    (output_root / "models" / "ER" / "model_card.md").unlink()
    with pytest.raises(ba.IncompleteArtifactsError):
        ba._verify_registered_artifacts("ER", output_root)


# --- thresholds are read-only ---------------------------------------------------------


def test_recipe_cannot_carry_thresholds():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BuildRecipe.model_validate(
            {
                "name": "x",
                "pipeline_runner_path": RUNNER,
                "pipeline_config_path": "c.yaml",
                "min_overlap": 1,  # a (lowered) threshold knob is rejected by extra="forbid"
            }
        )


def test_agent_thresholds_match_yaml_and_are_not_mutated(fail_recipe):
    # The agent's thresholds for the strict config equal load_gates() of the yaml — no
    # in-memory lowering. And the agent source has no path to construct/write thresholds.
    _runner, config, _allow, thresholds, _data = ba._prepare(fail_recipe, REPO_ROOT)
    assert config.gate.thresholds_path == "registry/data/quality_gates.yaml"
    assert thresholds == load_gates(repo_root=REPO_ROOT)
    src = (AGENT_SRC / "builder_agent.py").read_text()
    assert "GateThresholds(" not in src  # agent never constructs/overrides thresholds
    # The agent never writes a thresholds file: no line both references gates and writes.
    writes_gates = [ln for ln in src.splitlines() if "quality_gates" in ln and ".write" in ln]
    assert writes_gates == []


# --- zero reimplemented science: import + source scan ---------------------------------


def _func_defs(src: str) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(src)
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def test_agent_imports_no_training_or_register_symbols():
    src = (AGENT_SRC / "builder_agent.py").read_text()
    tree = ast.parse(src)
    imported: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.Import):
            modules |= {a.name for a in node.names}
    # No static import of the registry WRITE path or any training symbol.
    assert "register_endpoint" not in imported
    assert not any("training" in m for m in modules)
    assert not {"build_model", "fit_balanced", "nested_group_cv", "run_pipeline"} & imported
    # The gate IS reused (imported), not reimplemented.
    assert "quality_gates" in imported


def test_only_promote_authorizes_training():
    # `approved=True` (the train authorization) must appear in exactly ONE function: promote.
    src = (AGENT_SRC / "builder_agent.py").read_text()
    funcs = _func_defs(src)
    offenders = set()
    for name, fn in funcs.items():
        for node in ast.walk(fn):
            # dict literal {"approved": True} or keyword approved=True
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values, strict=False):
                    if isinstance(k, ast.Constant) and k.value == "approved":
                        if isinstance(v, ast.Constant) and v.value is True:
                            offenders.add(name)
            if isinstance(node, ast.keyword) and node.arg == "approved":
                if isinstance(node.value, ast.Constant) and node.value.value is True:
                    offenders.add(name)
    assert offenders == {"promote"}, f"approved=True authorized outside promote: {offenders}"
