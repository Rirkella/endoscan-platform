import { KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, EndoscanApiError } from "../api/client";
import type {
  AdminAgentRun,
  AdminApproval,
  AdminArtifact,
  AdminBuild,
  AdminTimelineEvent,
  AdminWorkflowError,
} from "../api/types";
import {
  WORKFLOW_STAGES,
  activityLabel,
  actorLabel,
  buildStatusLabel,
  humanizeMachineValue,
  relativeTime,
  shortBuildId,
  stageLabel,
  stageProgress,
  toolLabel,
} from "../adminPresentation";
import { AdminModal } from "../components/AdminModal";

type Candidate = {
  candidate_id: string;
  accession?: string;
  title: string;
  source: string;
  recommendation?: string;
  recommendation_status?: string;
  limitations: string[];
  strengths?: string[];
  organism?: string[];
  sample_count?: number;
  controls_available?: boolean;
  data_type?: string;
  context?: string;
  biological_context?: string;
  treatment_control_evidence?: string;
  dose_time_evidence?: string;
  accession_verified?: boolean;
  description?: string;
};

type CandidateArtifact = {
  candidates?: Candidate[];
  run_mode?: "live" | "cached" | "replay";
  simulation_label?: string | null;
  live_discovery?: boolean;
  decision_summary?: string;
  unresolved_questions?: string[];
};

type Decision = "approve" | "reject" | "request_revision" | "choose_alternative";
type Confirmation = { kind: Decision | "cancel"; approval?: AdminApproval };

function candidateStatus(candidate: Candidate, index: number): string {
  const value = (candidate.recommendation_status ?? candidate.recommendation ?? "").toLowerCase();
  if (value.includes("reject")) return "Rejected";
  if (value.includes("review")) return index === 0 ? "Recommended" : "Needs review";
  if (value.includes("prefer") || value.includes("recommend")) return "Recommended";
  if (value.includes("alternative")) return "Alternative";
  return "Needs review";
}

function candidateLabel(candidate: Candidate | undefined, index = 0): string {
  return candidate?.accession ?? (candidate?.candidate_id.startsWith("SIM-") ? candidate.candidate_id : `SIM-OS-${String(index + 1).padStart(3, "0")}`);
}

function modeLabel(mode: "live" | "cached" | "replay"): string {
  return mode === "live" ? "Live agent mode" : mode === "cached" ? "Cached mode" : "Replay mode";
}

function usageNumber(run: AdminAgentRun, name: string): number {
  const value = run.usage[name];
  return typeof value === "number" ? value : 0;
}

export function AdminEndpointDetail() {
  const { buildId = "" } = useParams();
  const confirmationTrigger = useRef<HTMLElement | null>(null);
  const [build, setBuild] = useState<AdminBuild | null>(null);
  const [events, setEvents] = useState<AdminTimelineEvent[]>([]);
  const [artifacts, setArtifacts] = useState<AdminArtifact[]>([]);
  const [approvals, setApprovals] = useState<AdminApproval[]>([]);
  const [runs, setRuns] = useState<AdminAgentRun[]>([]);
  const [trace, setTrace] = useState<AdminAgentRun | null>(null);
  const [errors, setErrors] = useState<AdminWorkflowError[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [runMode, setRunMode] = useState<"live" | "cached" | "replay">("replay");
  const [simulationLabel, setSimulationLabel] = useState<string | null>(null);
  const [decisionSummary, setDecisionSummary] = useState("");
  const [unresolvedQuestions, setUnresolvedQuestions] = useState<string[]>([]);
  const [comment, setComment] = useState("");
  const [selected, setSelected] = useState("");
  const [activityTab, setActivityTab] = useState<"activity" | "audit">("activity");
  const [fullActivity, setFullActivity] = useState(false);
  const [showTrace, setShowTrace] = useState(false);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [nextBuild, nextEvents, nextArtifacts, nextApprovals, nextRuns, nextErrors] =
        await Promise.all([
          api.adminGetBuild(buildId),
          api.adminTimeline(buildId),
          api.adminArtifacts(buildId),
          api.adminApprovals(buildId),
          api.adminAgentRuns(buildId),
          api.adminErrors(buildId),
        ]);
      setBuild(nextBuild);
      setEvents(nextEvents);
      setArtifacts(nextArtifacts);
      setApprovals(nextApprovals);
      setRuns(nextRuns);
      setErrors(nextErrors);
      const candidateArtifact = [...nextArtifacts]
        .reverse()
        .find((item) => item.artifact_type === "dataset_candidates");
      if (candidateArtifact) {
        const preview = await api.adminArtifactPreview(candidateArtifact.id);
        const content = preview.content as CandidateArtifact;
        setCandidates(content.candidates ?? []);
        setSelected((current) => current || content.candidates?.[0]?.candidate_id || "");
        setRunMode(content.run_mode ?? (content.live_discovery ? "live" : "replay"));
        setSimulationLabel(content.simulation_label ?? null);
        setDecisionSummary(content.decision_summary ?? "");
        setUnresolvedQuestions(content.unresolved_questions ?? []);
      } else {
        setCandidates([]);
        setRunMode("replay");
        setSimulationLabel(null);
        setDecisionSummary("");
        setUnresolvedQuestions([]);
      }
      if (nextRuns.length > 0) setTrace(await api.adminAgentRun(nextRuns.at(-1)!.id));
      else setTrace(null);
      setError(null);
    } catch {
      setError("Build details could not be loaded. Check the local service and try again.");
    }
  }, [buildId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function command(action: "start" | "pause" | "resume" | "cancel" | "retry" | "simulate-failure") {
    if (!build) return;
    setBusy(true);
    try {
      await api.adminCommand(build.id, action, build.version);
      await load();
      setConfirmation(null);
    } catch (reason) {
      setError(reason instanceof EndoscanApiError ? reason.detail : `Unable to ${action}.`);
    } finally {
      setBusy(false);
    }
  }

  async function decide(approval: AdminApproval, decision: Decision) {
    if (!build) return;
    setBusy(true);
    try {
      await api.adminDecideApproval(
        approval,
        build.version,
        decision,
        comment,
        decision === "choose_alternative" ? selected : undefined,
      );
      setComment("");
      setConfirmation(null);
      await load();
    } catch (reason) {
      setError(reason instanceof EndoscanApiError ? reason.detail : "The review decision could not be recorded.");
    } finally {
      setBusy(false);
    }
  }

  async function refreshSourceMetadata() {
    if (!build) return;
    setBusy(true);
    try {
      await api.adminRefreshSourceMetadata(build.id, build.version);
      await load();
    } catch (reason) {
      setError(reason instanceof EndoscanApiError ? reason.detail : "Source metadata could not be refreshed.");
    } finally {
      setBusy(false);
    }
  }

  const pending = approvals.find((item) => item.status === "pending") ?? null;
  const recommended = useMemo(
    () => candidates.find((item, index) => candidateStatus(item, index) === "Recommended") ?? candidates[0],
    [candidates],
  );
  const agentTools = useMemo(
    () => trace?.tools.length ? trace.tools : [...runs].reverse().find((run) => run.tools.length > 0)?.tools ?? [],
    [runs, trace],
  );

  if (!build) {
    return <section className="admin-page"><div className="admin-empty" role="status">{error ?? "Loading build..."}</div></section>;
  }

  const progress = stageProgress(build);
  const displayEvents = fullActivity ? [...events].reverse() : [...events].reverse().slice(0, 6);
  const canFail = !["DRAFT", "PAUSED", "FAILED", "CANCELLED", "COMPLETED", "REGISTERING"].includes(build.current_stage);
  const canPause = !["DRAFT", "PAUSED", "FAILED", "CANCELLED", "COMPLETED", "REGISTERING"].includes(build.current_stage);
  const canCancel = !["CANCELLED", "COMPLETED"].includes(build.current_stage);
  const selectedIndex = Math.max(0, candidates.findIndex((candidate) => candidate.candidate_id === selected));
  const selectedCandidate = candidates[selectedIndex];

  function requestConfirmation(kind: Confirmation["kind"], approval?: AdminApproval, trigger?: HTMLElement) {
    confirmationTrigger.current = trigger ?? null;
    setConfirmation({ kind, approval });
  }

  function handleTabKey(event: KeyboardEvent<HTMLButtonElement>) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const order: Array<"activity" | "audit"> = ["activity", "audit"];
    let index = order.indexOf(activityTab);
    if (event.key === "Home") index = 0;
    else if (event.key === "End") index = order.length - 1;
    else index = (index + (event.key === "ArrowRight" ? 1 : -1) + order.length) % order.length;
    setActivityTab(order[index]);
    requestAnimationFrame(() => document.getElementById(`${order[index]}-tab`)?.focus());
  }

  return (
    <section className="admin-page admin-detail-page" aria-labelledby="admin-build-title">
      <Link className="admin-back" to="/admin/endpoints">Back to endpoint builds</Link>
      <header className="admin-heading admin-detail-heading">
        <div>
          <div className="admin-title-meta">
            <span>Build {shortBuildId(build.id)}</span>
            <span>Updated {relativeTime(build.updated_at)}</span>
          </div>
          <h1 id="admin-build-title">{build.endpoint_name}</h1>
          <p>{build.biological_goal}</p>
        </div>
        <div className="admin-build-status-summary">
          <span className="admin-status admin-status-detail">{buildStatusLabel(build)}</span>
          <strong>{stageLabel(build.current_stage)}</strong>
          <span>Local administrator</span>
          <span className="admin-mode-badge">{modeLabel(runMode)}</span>
        </div>
      </header>

      {error && <div className="admin-error" role="alert">{error}</div>}

      <section className="admin-stepper-panel" aria-labelledby="workflow-progress-title">
        <div className="admin-panel-heading">
          <div><h2 id="workflow-progress-title">Workflow progress</h2><p>{progress.completed} of {progress.total} stages completed</p></div>
          <span aria-live="polite">Current: {stageLabel(build.current_stage)}</span>
        </div>
        <ol className="admin-stepper" aria-label="Endpoint build stages">
          {WORKFLOW_STAGES.map((stage, index) => {
            const state = index < progress.index ? "completed" : index === progress.index ? (build.current_stage === "FAILED" ? "failed" : "current") : "waiting";
            return (
              <li key={stage.label} className={`admin-step admin-step-${state}`} aria-current={state === "current" ? "step" : undefined}>
                <span className="admin-step-marker" aria-hidden>{state === "completed" ? "Done" : index + 1}</span>
                <span><strong>{stage.label}</strong><small>{state}</small></span>
              </li>
            );
          })}
          {progress.unknown && (
            <li className="admin-step admin-step-current" aria-current="step">
              <span className="admin-step-marker" aria-hidden>+</span>
              <span><strong>{stageLabel(build.current_stage)}</strong><small>future stage</small></span>
            </li>
          )}
        </ol>
      </section>

      <div className="admin-detail-grid">
        <main className="admin-primary-column">
          <section className="admin-panel admin-decision-panel" aria-labelledby="decision-title">
            <div className="admin-decision-heading">
              <div>
                <span className="admin-section-kicker">Human decision</span>
                <h2 id="decision-title">{pending ? "Dataset review required" : "No review required"}</h2>
                <p>{pending ? "Review the bounded recommendation before any data curation can begin." : "This workflow is not currently waiting for a reviewer."}</p>
              </div>
              {pending && <span className="admin-review-flag">Action required</span>}
            </div>
            {pending ? (
              <div className="admin-approval-card">
                <div className="admin-recommendation">
                  <span>Recommended candidate</span>
                  <strong>{recommended ? candidateLabel(recommended, Math.max(0, candidates.indexOf(recommended))) : "No candidate"}</strong>
                  <h3>{recommended?.title ?? pending.request.proposed_decision}</h3>
                  <p>{recommended?.description ?? recommended?.biological_context ?? pending.request.evidence_summary}</p>
                </div>
                <div className="admin-decision-evidence">
                  <div>
                    <h3>Why it is recommended</h3>
                    <ul>{[...(recommended?.strengths ?? []), pending.request.agent_recommendation].map((item) => <li key={item}>{item}</li>)}</ul>
                  </div>
                  <div>
                    <h3>Limitations</h3>
                    <ul>{[...(recommended?.limitations ?? []), ...pending.request.limitations].filter((item, index, all) => all.indexOf(item) === index).map((item) => <li key={item}>{item}</li>)}</ul>
                  </div>
                </div>
                <label className="admin-comment-field">
                  Reviewer comment
                  <textarea value={comment} onChange={(event) => setComment(event.target.value)} placeholder="Required for rejection, revision, or selecting another candidate" />
                </label>
                <div className="admin-approval-actions">
                  <button className="admin-primary" disabled={busy} onClick={(event) => requestConfirmation("approve", pending, event.currentTarget)}>Approve dataset</button>
                  <button className="admin-secondary" disabled={busy || !comment} onClick={(event) => requestConfirmation("request_revision", pending, event.currentTarget)}>Request revision</button>
                  <button className="admin-danger-outline" disabled={busy || !comment} onClick={(event) => requestConfirmation("reject", pending, event.currentTarget)}>Reject</button>
                  {candidates.length > 1 && <button className="admin-link-button" disabled={busy || !selected || !comment} onClick={(event) => requestConfirmation("choose_alternative", pending, event.currentTarget)}>Select another candidate</button>}
                </div>
              </div>
            ) : <div className="admin-empty">The next workflow action is shown below.</div>}
          </section>

          {candidates.length > 0 && (
            <section className="admin-panel" aria-labelledby="candidate-comparison-title">
              <div className="admin-panel-heading">
                <div><h2 id="candidate-comparison-title">Candidate comparison</h2><p>{runMode === "replay" ? simulationLabel ?? "Prepared replay fixture; not live scientific discovery." : "Official-source metadata prepared for human scientific review."}</p></div>
                <span>{candidates.length} candidates</span>
              </div>
              <div className="admin-candidate-grid">
                {candidates.map((candidate, index) => {
                  const status = candidateStatus(candidate, index);
                  return (
                    <article className={`admin-candidate-card ${selected === candidate.candidate_id ? "candidate-selected" : ""}`} key={candidate.candidate_id}>
                      <div className="admin-card-title-row">
                        <span className={`admin-candidate-status admin-candidate-${status.toLowerCase().replace(/ /g, "-")}`}>{status}</span>
                        <span className="admin-fixture-label">{runMode === "replay" ? "Prepared replay fixture" : candidate.accession_verified ? "Official accession verified" : "Verification required"}</span>
                      </div>
                      <h3>{candidateLabel(candidate, index)}</h3>
                      <p>{candidate.title}</p>
                      <dl className="admin-candidate-facts">
                        <div><dt>Source</dt><dd>{candidate.source.startsWith("https://") ? <a href={candidate.source} target="_blank" rel="noreferrer">NCBI GEO</a> : candidate.source}</dd></div>
                        <div><dt>Organism</dt><dd>{candidate.organism?.join(", ") || "Not specified"}</dd></div>
                        <div><dt>Samples</dt><dd>{candidate.sample_count ?? "Not supplied"}</dd></div>
                        <div><dt>Controls</dt><dd>{candidate.treatment_control_evidence ?? (candidate.controls_available == null ? "Not verified" : candidate.controls_available ? "Available" : "Unavailable")}</dd></div>
                        <div><dt>Data type</dt><dd>{candidate.data_type ?? "Not specified"}</dd></div>
                        <div><dt>Context</dt><dd>{candidate.biological_context ?? candidate.context ?? "Not specified"}</dd></div>
                        <div><dt>Dose / time</dt><dd>{candidate.dose_time_evidence ?? "Not verified"}</dd></div>
                      </dl>
                      <h4>Cautions</h4>
                      <ul>{candidate.limitations.map((item) => <li key={item}>{item}</li>)}</ul>
                      <label className="admin-candidate-choice">
                        <input type="radio" name="candidate" checked={selected === candidate.candidate_id} onChange={() => setSelected(candidate.candidate_id)} />
                        Select {candidateLabel(candidate, index)}
                      </label>
                    </article>
                  );
                })}
              </div>
            </section>
          )}

          <section className="admin-panel" aria-labelledby="workflow-actions-title">
            <div className="admin-panel-heading"><h2 id="workflow-actions-title">Workflow actions</h2><span>Only valid actions are shown</span></div>
            <div className="admin-actions" aria-label="Workflow controls">
              {build.current_stage === "DRAFT" && <button disabled={busy} onClick={() => void command("start")}>Start workflow</button>}
              {canPause && <button disabled={busy} onClick={() => void command("pause")}>Pause</button>}
              {build.current_stage === "PAUSED" && <button disabled={busy} onClick={() => void command("resume")}>Resume</button>}
              {build.current_stage === "FAILED" && <button disabled={busy} onClick={() => void command("retry")}>Retry failed step</button>}
              {canCancel && <button className="admin-danger-outline" disabled={busy} onClick={(event) => requestConfirmation("cancel", undefined, event.currentTarget)}>Cancel workflow</button>}
              <button className="admin-secondary" disabled={busy} onClick={() => void load()}>Refresh</button>
              {import.meta.env.DEV && runMode !== "replay" && <button className="admin-secondary" disabled={busy} onClick={() => void refreshSourceMetadata()}>Refresh source metadata</button>}
            </div>
          </section>

          {(decisionSummary || unresolvedQuestions.length > 0) && (
            <section className="admin-panel" aria-labelledby="decision-summary-title">
              <div className="admin-panel-heading"><h2 id="decision-summary-title">Decision summary</h2><span>Human review required</span></div>
              {decisionSummary && <p>{decisionSummary}</p>}
              {unresolvedQuestions.length > 0 && <><h3>Unresolved questions</h3><ul>{unresolvedQuestions.map((item) => <li key={item}>{item}</li>)}</ul></>}
            </section>
          )}
        </main>

        <aside className="admin-secondary-column">
          <section className="admin-panel" aria-labelledby="agent-summary-title">
            <div className="admin-panel-heading"><h2 id="agent-summary-title">Agent activity</h2><span>{runs.length} run{runs.length === 1 ? "" : "s"}</span></div>
            {trace ? (
              <div className="admin-agent-summary">
                <h3>{trace.agent_name}</h3>
                <span className="admin-status">{trace.status === "completed" ? "Completed" : "Completed for review"}</span>
                <dl>
                  <div><dt>Output</dt><dd>{candidates.length} candidates prepared</dd></div>
                  <div><dt>Tools used</dt><dd>{agentTools.length}{trace.tools.length === 0 && agentTools.length > 0 ? " (replayed)" : ""}</dd></div>
                  <div><dt>Duration</dt><dd>{(trace.duration_ms / 1000).toFixed(2)} s</dd></div>
                  <div><dt>Provider</dt><dd>{trace.provider}</dd></div>
                  <div><dt>Model</dt><dd>{trace.model_identifier}</dd></div>
                  <div><dt>Input tokens</dt><dd>{usageNumber(trace, "input_tokens")}</dd></div>
                  <div><dt>Output tokens</dt><dd>{usageNumber(trace, "output_tokens")}</dd></div>
                  <div><dt>Estimated cost</dt><dd>${(usageNumber(trace, "cost_cents") / 100).toFixed(4)}</dd></div>
                </dl>
                <ol id="agent-tools" className="admin-tool-list">{agentTools.map((tool) => <li key={tool.id}><span>{toolLabel(tool.tool_name)}</span><small>{trace.tools.length === 0 ? "replayed" : tool.status}</small></li>)}</ol>
                <div className="admin-inline-actions">
                  <button className="admin-link-button" onClick={() => setShowTrace((value) => !value)}>{showTrace ? "Hide trace" : "View trace"}</button>
                  <a href="#agent-tools">View tools used</a>
                  <a href="#artifacts">View generated artifact</a>
                </div>
                {showTrace && <div className="admin-trace-summary" id="agent-trace"><h4>Trace events</h4><ol>{(trace.trace?.events ?? []).map((event, index) => <li key={index}>{typeof event.event_type === "string" ? humanizeMachineValue(event.event_type.replace(/\./g, "_")) : `Trace event ${index + 1}`}</li>)}</ol></div>}
              </div>
            ) : <div className="admin-empty">No agent run yet.</div>}
          </section>

          <section className="admin-panel" id="artifacts" aria-labelledby="artifacts-title">
            <div className="admin-panel-heading"><h2 id="artifacts-title">Generated artifacts</h2><span>{artifacts.length}</span></div>
            <div className="admin-artifact-list">{artifacts.map((artifact) => <article key={artifact.id}><div><strong>{artifact.logical_name}</strong><span>{humanizeMachineValue(artifact.artifact_type)} · {artifact.producer}</span></div></article>)}</div>
          </section>

          <section className="admin-panel" id="technical-audit" aria-labelledby="activity-title">
            <div className="admin-tabs" role="tablist" aria-label="Build history">
              <button id="activity-tab" role="tab" aria-selected={activityTab === "activity"} aria-controls="activity-panel" tabIndex={activityTab === "activity" ? 0 : -1} onKeyDown={handleTabKey} onClick={() => setActivityTab("activity")}>Activity</button>
              <button id="audit-tab" role="tab" aria-selected={activityTab === "audit"} aria-controls="audit-panel" tabIndex={activityTab === "audit" ? 0 : -1} onKeyDown={handleTabKey} onClick={() => setActivityTab("audit")}>Technical audit log</button>
            </div>
            {activityTab === "activity" ? (
              <div id="activity-panel" role="tabpanel" aria-labelledby="activity-tab">
                <h2 id="activity-title" className="sr-only">Activity</h2>
                <ol className="admin-activity-list">
                  {displayEvents.map((event) => <li key={event.id}><span className="admin-activity-marker" aria-hidden /><div><strong>{activityLabel(event)}</strong><p>{actorLabel(event.actor_type)} · {new Date(event.created_at).toLocaleString()}</p></div></li>)}
                </ol>
                {events.length > 6 && <button className="admin-link-button" onClick={() => setFullActivity((value) => !value)}>{fullActivity ? "Show recent activity" : "Show full activity"}</button>}
              </div>
            ) : (
              <div id="audit-panel" role="tabpanel" aria-labelledby="audit-tab">
                <ol className="admin-audit-list">
                  {[...events].reverse().map((event) => <li key={event.id}><div><strong>{event.event_type}</strong><time>{new Date(event.created_at).toLocaleString()}</time></div><dl><div><dt>Transition</dt><dd>{event.from_state ?? "none"} to {event.to_state ?? "none"}</dd></div><div><dt>Actor</dt><dd>{event.actor_type} / {event.actor_id}</dd></div><div><dt>Event ID</dt><dd>{event.id}</dd></div><div><dt>Event hash</dt><dd>{event.event_hash}</dd></div></dl></li>)}
                </ol>
              </div>
            )}
          </section>

          <details className="admin-panel admin-technical-details">
            <summary>Technical details</summary>
            <dl>
              <div><dt>Machine state</dt><dd>{build.current_stage}</dd></div>
              <div><dt>Workflow version</dt><dd>{build.version}</dd></div>
              <div><dt>Build ID</dt><dd>{build.id}</dd></div>
              {pending && <div><dt>Proposal binding</dt><dd>{pending.proposal_hash}</dd></div>}
            </dl>
          </details>

          {errors.length > 0 && <section className="admin-panel admin-errors-panel"><div className="admin-panel-heading"><h2>Workflow errors</h2><span>{errors.length}</span></div>{errors.map((item) => <article className="admin-error-row" key={item.id}><strong>{humanizeMachineValue(item.code)}</strong><p>{item.safe_message}</p><span>{item.retryable ? "Retryable" : "Terminal"}</span></article>)}</section>}

          {import.meta.env.DEV && (
            <details className="admin-panel admin-developer-tools">
              <summary>Developer tools <span>Test only</span></summary>
              <p>Simulation-only recovery controls. These actions are not part of the production workflow.</p>
              {canFail ? <button className="admin-danger-outline" disabled={busy} onClick={() => void command("simulate-failure")}>Trigger controlled failure</button> : <span>No test action is valid in this state.</span>}
            </details>
          )}
        </aside>
      </div>

      {confirmation && (
        <AdminModal
          title={confirmation.kind === "approve" ? "Confirm dataset approval" : confirmation.kind === "cancel" ? "Cancel this workflow?" : `Confirm ${humanizeMachineValue(confirmation.kind).toLowerCase()}`}
          description={confirmation.kind === "approve" ? "Review the selected candidate and the scope of this decision before continuing." : "This decision will be recorded in the immutable audit history."}
          onClose={() => setConfirmation(null)}
          returnFocus={confirmationTrigger.current}
        >
          {confirmation.kind === "cancel" ? (
            <div className="admin-confirmation">
              <p>Build {shortBuildId(build.id)} will be cancelled. Completed artifacts and audit history will remain available.</p>
              <div className="admin-modal-actions"><button className="admin-secondary" onClick={() => setConfirmation(null)}>Keep workflow</button><button className="admin-danger" disabled={busy} onClick={() => void command("cancel")}>Cancel workflow</button></div>
            </div>
          ) : (
            <div className="admin-confirmation">
              <dl>
                <div><dt>Selected candidate</dt><dd>{selectedCandidate ? `${candidateLabel(selectedCandidate, selectedIndex)} · ${selectedCandidate.title}` : "No candidate"}</dd></div>
                <div><dt>Evidence binding</dt><dd>Current immutable candidate artifact</dd></div>
                <div><dt>Approval scope</dt><dd>Dataset selection for this build only</dd></div>
                <div><dt>Next stage</dt><dd>{confirmation.kind === "approve" ? "Prepare the selected dataset" : confirmation.kind === "request_revision" ? "Revise the prepared comparison" : confirmation.kind === "choose_alternative" ? "Record the alternative selection" : "Stop this dataset proposal"}</dd></div>
              </dl>
              {comment && <p><strong>Reviewer comment:</strong> {comment}</p>}
              <div className="admin-modal-actions"><button className="admin-secondary" onClick={() => setConfirmation(null)}>Go back</button><button className={confirmation.kind === "reject" ? "admin-danger" : "admin-primary"} disabled={busy} onClick={() => confirmation.approval && void decide(confirmation.approval, confirmation.kind as Decision)}>Confirm decision</button></div>
            </div>
          )}
        </AdminModal>
      )}
    </section>
  );
}
