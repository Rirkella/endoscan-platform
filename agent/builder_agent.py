"""The EndoScan Builder Agent — a thin orchestrator over tested tools.

It SEQUENCES the M2 dataset-construction tools (``source_selector`` ... ``quality_gates``)
to produce a candidate dataset + dataset card + gate verdict in a scratch *workspace*,
then STOPS at the gate. Training/registration is reachable ONLY through the single
guarded :func:`promote`, which requires BOTH a passing gate (re-derived from the
fingerprinted gate report) AND a valid, issued approval token, and which delegates the
actual train/evaluate/card/register to the existing M3 ``run_pipeline`` — the agent
reimplements ZERO science and never edits ``run.py``.

Enforcement is two-layer: the agent's outer precondition (gate + token in ``promote``)
and ``run_pipeline``'s own inner ``verdict.passed and approval.approved`` boundary
(``pipelines/endpoints/ER/run.py``). Thresholds are read-only (``quality_gates.yaml``
via the runner's loader); provenance/``source_refs`` are derived only by ``run_pipeline``.
"""

from __future__ import annotations

import importlib.util
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from endoscan_core.datasets import (
    candidate_table_builder,
    compound_mapper,
    dataset_quality_report,
    label_retriever,
    load_sources,
    overlap_computer,
    quality_gates,
    signature_retriever,
    source_selector,
)
from endoscan_core.inference import build_limitations
from endoscan_core.registry import get_endpoint

from .build_state import (
    ApprovalRequiredError,
    ApprovalToken,
    BuildError,
    BuildRecipe,
    BuildRecord,
    BuildState,
    BuildStateError,
    GateDivergenceError,
    GateFailedError,
    GateReport,
    IncompleteArtifactsError,
    assert_transition,
)

_STATUS_FILE = "build_status.json"
_GATE_FILE = "gate_report.json"
_TOKEN_FILE = "approval.json"
_runner_cache: dict[str, ModuleType] = {}


# --------------------------------------------------------------------------- runner load
def _load_runner(runner_path: Path) -> ModuleType:
    """Load the M3 pipeline runner module by file path (it is not an importable package).

    Cached by absolute path. The runner is the reused tool (it may import training +
    registry write code); the AGENT statically imports none of that — see the import-scan
    test. ``run_pipeline`` is obtained dynamically here, never as a static import.
    """
    key = str(runner_path.resolve())
    if key in _runner_cache:
        return _runner_cache[key]
    spec = importlib.util.spec_from_file_location(
        f"endoscan_runner_{runner_path.stem}", runner_path
    )
    if not spec or not spec.loader:
        raise BuildError(f"cannot load pipeline runner at {runner_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # register before exec so pydantic resolves forward refs
    spec.loader.exec_module(module)
    _runner_cache[key] = module
    return module


# --------------------------------------------------------------------------- persistence
def _workspace(builds_root: Path, build_id: str) -> Path:
    return Path(builds_root) / build_id


def _save_record(ws: Path, record: BuildRecord) -> None:
    (ws / _STATUS_FILE).write_text(record.model_dump_json(indent=2), encoding="utf-8")


def _load_record(ws: Path) -> BuildRecord:
    path = ws / _STATUS_FILE
    if not path.is_file():
        raise BuildError(f"no build found at {ws}")
    return BuildRecord.model_validate_json(path.read_text(encoding="utf-8"))


def _load_gate_report(ws: Path) -> GateReport:
    path = ws / _GATE_FILE
    if not path.is_file():
        raise BuildError(f"no gate report for build at {ws}")
    return GateReport.model_validate_json(path.read_text(encoding="utf-8"))


def _load_token(ws: Path) -> ApprovalToken | None:
    path = ws / _TOKEN_FILE
    if not path.is_file():
        return None
    return ApprovalToken.model_validate_json(path.read_text(encoding="utf-8"))


def _now() -> datetime:
    return datetime.now(UTC)


def _advance(ws: Path, record: BuildRecord, target: BuildState, *, gate_passed=...) -> BuildRecord:
    """Validate + apply a state transition and persist the record."""
    assert_transition(record.state, target)
    update: dict = {"state": target, "updated_at": _now()}
    if gate_passed is not ...:
        update["gate_passed"] = gate_passed
    record = record.model_copy(update=update)
    _save_record(ws, record)
    return record


# --------------------------------------------------------------------------- gate phase
def _compute_gate(config, allow_list, adapter, thresholds, cards_dir: Path):
    """Sequence the tested M2 tools and return ``(report, verdict, dataset_card_path)``.

    REIMPLEMENTS NO SCIENCE — every step is a call to an existing ``endoscan_core`` tool,
    mirroring the pre-gate sequence in ``run_pipeline`` (run.py): source_selector ->
    label_retriever -> signature_retriever -> compound_mapper -> overlap_computer ->
    candidate_table_builder -> dataset_quality_report -> quality_gates.
    """
    target = config.data.target
    label_sources = source_selector(target, allow_list, types=["labels"])
    sig_sources = source_selector(target, allow_list, types=["signatures"])
    map_source = source_selector(target, allow_list, types=["mapping"])[0]

    present_ids = {s.id for s in label_sources if adapter.has_source(s)}
    required = set(config.data.required_label_sources)
    missing_required = sorted(
        s.id for s in label_sources if s.id in required and s.id not in present_ids
    )
    if missing_required:
        raise BuildError(f"required label source(s) not staged for {target}: {missing_required}")
    present_label_sources = [s for s in label_sources if s.id in present_ids]

    labels = label_retriever(target, present_label_sources, adapter, allow_list=allow_list)
    signatures = signature_retriever(sig_sources, adapter, allow_list=allow_list)
    ids = [(r.compound_id, r.compound_id_type) for r in labels.records]
    ids += [(s.compound_id, s.compound_id_type) for s in signatures.records]
    mapping = compound_mapper(ids, map_source, adapter, allow_list=allow_list)
    overlap = overlap_computer(labels, signatures, mapping)
    table = candidate_table_builder(
        labels, signatures, overlap, mapping, n_groups=config.data.n_groups, seed=config.data.seed
    )
    report = dataset_quality_report(
        target,
        table,
        labels,
        signatures,
        overlap.n_labeled,
        overlap.n_with_signature,
        overlap.n_overlap,
        cards_dir,
        write_card=True,
    )
    verdict = quality_gates(report, thresholds)  # the SAME gate function run_pipeline uses
    return report, verdict, report.dataset_card_path


def _prepare(record_or_recipe, repo_root: Path):
    """Resolve a recipe to ``(runner, config, allow_list, thresholds, data_dir)``."""
    recipe = (
        record_or_recipe.recipe if isinstance(record_or_recipe, BuildRecord) else record_or_recipe
    )
    runner = _load_runner(repo_root / recipe.pipeline_runner_path)
    config = runner.load_config(repo_root / recipe.pipeline_config_path)
    allow_list = load_sources(repo_root=repo_root)
    thresholds = runner.load_thresholds(repo_root / config.gate.thresholds_path)
    data_dir = repo_root / (config.data.staged_dir or config.data.fixtures_dir or ".")
    return runner, config, allow_list, thresholds, data_dir


# --------------------------------------------------------------------------- public API
def start_build(
    recipe: BuildRecipe | str | Path, *, repo_root: Path, builds_root: Path
) -> BuildRecord:
    """Run the dataset build + gate into a workspace and STOP. Never trains/registers.

    On a PASS the build ends in ``awaiting_approval``; on a FAIL in ``failed_qc`` with a
    complete workspace (gate report + dataset card) and the registry left untouched.
    """
    repo_root, builds_root = Path(repo_root), Path(builds_root)
    if isinstance(recipe, str | Path):
        recipe = BuildRecipe.model_validate_json(_read_yaml_as_json(Path(recipe)))

    runner, config, allow_list, thresholds, data_dir = _prepare(recipe, repo_root)
    build_id = f"{recipe.name}-{_now().strftime('%Y%m%dT%H%M%S%f')}"
    ws = _workspace(builds_root, build_id)
    ws.mkdir(parents=True, exist_ok=True)

    record = BuildRecord(
        build_id=build_id,
        name=recipe.name,
        state=BuildState.created,
        endpoint_id=config.endpoint_id,
        target=config.data.target,
        recipe=recipe,
        created_at=_now(),
        updated_at=_now(),
    )
    _save_record(ws, record)
    record = _advance(ws, record, BuildState.building)

    adapter = runner.make_adapter(config, data_dir)
    cards_dir = ws / "registry" / "data" / "dataset_cards"
    _report, verdict, card_path = _compute_gate(config, allow_list, adapter, thresholds, cards_dir)

    gate_report = GateReport(
        passed=verdict.passed,
        summary=verdict.summary,
        checks=[check.model_dump() for check in verdict.checks],
        dataset_card_path=str(card_path) if card_path else None,
    )
    (ws / _GATE_FILE).write_text(gate_report.model_dump_json(indent=2), encoding="utf-8")

    record = _advance(ws, record, BuildState.dataset_ready)
    if verdict.passed:
        record = _advance(ws, record, BuildState.gated_pass, gate_passed=True)
        record = _advance(ws, record, BuildState.awaiting_approval, gate_passed=True)
    else:
        record = _advance(ws, record, BuildState.gated_fail, gate_passed=False)
        record = _advance(ws, record, BuildState.failed_qc, gate_passed=False)
    return record


def get_status(build_id: str, *, builds_root: Path) -> BuildRecord:
    """Read-only: the current build record."""
    return _load_record(_workspace(builds_root, build_id))


def get_gate_report(build_id: str, *, builds_root: Path) -> GateReport:
    """Read-only: the fingerprinted gate report (authoritative pass/fail)."""
    return _load_gate_report(_workspace(builds_root, build_id))


def approve(build_id: str, approver: str, *, builds_root: Path, note: str = "") -> ApprovalToken:
    """Issue an approval token for a PASSED build. Refuses unless the build is
    ``awaiting_approval`` (so a failed/not-yet-gated build can never be approved). The
    token is bound to the current gate report's fingerprint."""
    ws = _workspace(builds_root, build_id)
    record = _load_record(ws)
    if record.state is not BuildState.awaiting_approval:
        raise BuildStateError(
            f"cannot approve build in state {record.state.value!r}; only 'awaiting_approval'"
        )
    gate_report = _load_gate_report(ws)
    if not gate_report.passed:  # defensive; awaiting_approval implies a PASS
        raise GateFailedError("cannot approve a build whose gate verdict is FAIL")
    token = ApprovalToken(
        build_id=build_id,
        gate_fingerprint=gate_report.fingerprint(),
        approver=approver,
        value=secrets.token_hex(16),
        issued_at=_now(),
        note=note,
    )
    (ws / _TOKEN_FILE).write_text(token.model_dump_json(indent=2), encoding="utf-8")
    _advance(ws, record, BuildState.approved)
    return token


def reject(build_id: str, approver: str, *, builds_root: Path, reason: str = "") -> BuildRecord:
    """Reject a pending build (terminal for registration)."""
    ws = _workspace(builds_root, build_id)
    record = _load_record(ws)
    if record.state is not BuildState.awaiting_approval:
        raise BuildStateError(
            f"cannot reject build in state {record.state.value!r}; only 'awaiting_approval'"
        )
    (ws / "rejection.json").write_text(
        json.dumps({"approver": approver, "reason": reason, "at": _now().isoformat()}, indent=2),
        encoding="utf-8",
    )
    return _advance(ws, record, BuildState.rejected)


def promote(
    build_id: str,
    token: ApprovalToken | None,
    *,
    repo_root: Path,
    output_root: Path,
    builds_root: Path,
):
    """THE CHOKEPOINT — the only path to training/registration.

    Refuses unless BOTH the fingerprinted gate report says PASS AND a valid, issued token
    is presented. Gate failure is checked from the fingerprinted report (not the
    display-only ``record.gate_passed``), so approval is necessary but NOT sufficient.
    Delegates train/evaluate/card/register to ``run_pipeline`` (reused, not reimplemented);
    raises on inner divergence and leaves the registry untouched on any refusal.
    """
    repo_root, output_root = Path(repo_root), Path(output_root)
    ws = _workspace(builds_root, build_id)
    record = _load_record(ws)

    if token is None:
        raise ApprovalRequiredError("an approval token is required to promote")

    gate_report = _load_gate_report(ws)  # SINGLE SOURCE OF TRUTH
    fingerprint = gate_report.fingerprint()  # recomputed from content; tamper-evident
    if token.build_id != build_id or token.gate_fingerprint != fingerprint:
        raise ApprovalRequiredError(
            "approval token does not match the current gate report (forged or tampered)"
        )
    if not gate_report.passed:  # derived from the fingerprinted report, never record.gate_passed
        raise GateFailedError(
            "gate verdict is FAIL; approval cannot authorize training past a failed gate"
        )
    issued = _load_token(ws)
    if issued is None or issued.value != token.value:
        raise ApprovalRequiredError("no matching issued approval token for this build")
    if record.state is not BuildState.approved:
        raise BuildStateError(f"build must be 'approved' to promote; is {record.state.value!r}")

    runner, config, allow_list, thresholds, data_dir = _prepare(record, repo_root)
    # The ONLY place in the agent that authorizes training: approved=True set here alone.
    approved_config = config.model_copy(
        update={
            "approval": config.approval.model_copy(
                update={"approved": True, "approved_by": token.approver, "note": token.note}
            )
        }
    )
    result = runner.run_pipeline(
        approved_config,
        allow_list=allow_list,
        data_dir=data_dir,
        thresholds=thresholds,
        output_root=output_root,
    )
    if not result.registered:  # inner run_pipeline verdict disagreed with the outer PASS
        raise GateDivergenceError(
            "run_pipeline declined to register a gated_pass build (inner/outer verdict divergence)"
        )
    _verify_registered_artifacts(record.endpoint_id, output_root)
    _advance(ws, record, BuildState.registered, gate_passed=True)
    return result


def _verify_registered_artifacts(endpoint_id: str, output_root: Path) -> None:
    """Defense-in-depth: refuse a registration missing any artifact or whose limitations
    block is not derivable (ties M5 to M4's limitations guarantee)."""
    entry = get_endpoint(endpoint_id, repo_root=output_root)
    for rel in entry.artifact_paths():
        if not (output_root / rel).is_file():
            raise IncompleteArtifactsError(f"registered {endpoint_id} missing artifact: {rel}")
    metrics = json.loads((output_root / entry.metrics_path).read_text(encoding="utf-8"))
    try:
        build_limitations(entry, metrics)  # must produce a limitations block
    except Exception as exc:  # noqa: BLE001 - surface any limitations failure as incomplete
        raise IncompleteArtifactsError(
            f"limitations not derivable for {endpoint_id}: {exc}"
        ) from exc


def _read_yaml_as_json(path: Path) -> str:
    import yaml  # local import; only needed when a recipe is given as a file

    return json.dumps(yaml.safe_load(path.read_text(encoding="utf-8")))
