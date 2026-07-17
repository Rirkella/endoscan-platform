import { useCallback, useEffect, useState } from "react";
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

type Candidate = {
  candidate_id: string;
  title: string;
  source: string;
  recommendation: string;
  limitations: string[];
};

export function AdminEndpointDetail() {
  const { buildId = "" } = useParams();
  const [build, setBuild] = useState<AdminBuild | null>(null);
  const [events, setEvents] = useState<AdminTimelineEvent[]>([]);
  const [artifacts, setArtifacts] = useState<AdminArtifact[]>([]);
  const [approvals, setApprovals] = useState<AdminApproval[]>([]);
  const [runs, setRuns] = useState<AdminAgentRun[]>([]);
  const [trace, setTrace] = useState<AdminAgentRun | null>(null);
  const [errors, setErrors] = useState<AdminWorkflowError[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [comment, setComment] = useState("");
  const [selected, setSelected] = useState("");
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
        const content = preview.content as { candidates?: Candidate[] };
        setCandidates(content.candidates ?? []);
        setSelected((current) => current || content.candidates?.[0]?.candidate_id || "");
      }
      if (nextRuns.length > 0) setTrace(await api.adminAgentRun(nextRuns.at(-1)!.id));
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to load build detail.");
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
    } catch (reason) {
      setError(reason instanceof EndoscanApiError ? reason.detail : `Unable to ${action}.`);
    } finally {
      setBusy(false);
    }
  }

  async function decide(
    approval: AdminApproval,
    decision: "approve" | "reject" | "request_revision" | "choose_alternative",
  ) {
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
      await load();
    } catch (reason) {
      setError(reason instanceof EndoscanApiError ? reason.detail : "Approval could not be recorded.");
    } finally {
      setBusy(false);
    }
  }

  if (!build) {
    return <section className="admin-page"><div className="admin-empty">{error ?? "Loading build…"}</div></section>;
  }
  const pending = approvals.find((item) => item.status === "pending") ?? null;
  const canFail = !["DRAFT", "PAUSED", "FAILED", "CANCELLED", "COMPLETED", "REGISTERING"].includes(build.current_stage);

  return (
    <section className="admin-page" aria-labelledby="admin-build-title">
      <Link className="admin-back" to="/admin/endpoints">← Endpoint builds</Link>
      <div className="admin-heading admin-detail-heading">
        <div>
          <span className="admin-eyebrow">{build.endpoint_slug}</span>
          <h1 id="admin-build-title">{build.endpoint_name}</h1>
          <p>{build.biological_goal}</p>
        </div>
        <div className="admin-current-stage">
          <span>Current stage</span>
          <strong>{build.current_stage.split("_").join(" ")}</strong>
          <small>Workflow version {build.version}</small>
        </div>
      </div>
      <div className="admin-simulation-banner">
        <strong>Prepared deterministic agent simulation</strong>
        <span>No live LLM or scientific discovery occurred.</span>
      </div>
      {error && <div className="admin-error" role="alert">{error}</div>}

      <div className="admin-actions" aria-label="Workflow controls">
        {build.current_stage === "DRAFT" && <button disabled={busy} onClick={() => void command("start")}>Start</button>}
        {!["DRAFT", "PAUSED", "FAILED", "CANCELLED", "COMPLETED", "REGISTERING"].includes(build.current_stage) && (
          <button disabled={busy} onClick={() => void command("pause")}>Pause</button>
        )}
        {build.current_stage === "PAUSED" && <button disabled={busy} onClick={() => void command("resume")}>Resume</button>}
        {build.current_stage === "FAILED" && <button disabled={busy} onClick={() => void command("retry")}>Retry failed step</button>}
        {canFail && <button disabled={busy} onClick={() => void command("simulate-failure")}>Trigger controlled failure</button>}
        {!['CANCELLED', 'COMPLETED'].includes(build.current_stage) && (
          <button className="admin-danger" disabled={busy} onClick={() => void command("cancel")}>Cancel</button>
        )}
        <button className="admin-secondary" disabled={busy} onClick={() => void load()}>Refresh</button>
      </div>

      <div className="admin-detail-grid">
        <section className="admin-panel admin-timeline-panel">
          <div className="admin-panel-heading"><h2>Stage timeline</h2><span>{events.length} events</span></div>
          <ol className="admin-timeline">
            {events.map((event) => (
              <li key={event.id}>
                <span className="admin-event-index">{event.sequence}</span>
                <div><strong>{event.event_type.split(".").join(" ")}</strong><p>{event.from_state ?? "—"} → {event.to_state ?? "—"}</p><small>{event.actor_type} / {new Date(event.created_at).toLocaleTimeString()}</small></div>
              </li>
            ))}
          </ol>
        </section>

        <section className="admin-panel">
          <div className="admin-panel-heading"><h2>Pending approval</h2><span>{pending ? pending.approval_type : "None"}</span></div>
          {pending ? (
            <div className="admin-approval-card">
              <span className="admin-hash">Proposal {pending.proposal_hash.slice(0, 12)}…</span>
              <h3>{pending.request.proposed_decision}</h3>
              <p>{pending.request.evidence_summary}</p>
              <strong>Agent recommendation</strong><p>{pending.request.agent_recommendation}</p>
              <strong>Limitations</strong>
              <ul>{pending.request.limitations.map((item) => <li key={item}>{item}</li>)}</ul>
              {candidates.length > 0 && (
                <div className="admin-candidates">
                  {candidates.map((candidate) => (
                    <label key={candidate.candidate_id} className={selected === candidate.candidate_id ? "candidate-selected" : ""}>
                      <input type="radio" name="candidate" checked={selected === candidate.candidate_id} onChange={() => setSelected(candidate.candidate_id)} />
                      <span><strong>{candidate.title}</strong><small>{candidate.source} · {candidate.recommendation}</small></span>
                    </label>
                  ))}
                </div>
              )}
              <label>Reviewer comment<textarea value={comment} onChange={(event) => setComment(event.target.value)} placeholder="Required for rejection, revision, or alternative selection" /></label>
              <div className="admin-approval-actions">
                <button disabled={busy} onClick={() => void decide(pending, "approve")}>Approve</button>
                <button disabled={busy || !selected || !comment} onClick={() => void decide(pending, "choose_alternative")}>Choose alternative</button>
                <button disabled={busy || !comment} onClick={() => void decide(pending, "request_revision")}>Request revision</button>
                <button className="admin-danger" disabled={busy || !comment} onClick={() => void decide(pending, "reject")}>Reject</button>
              </div>
            </div>
          ) : <div className="admin-empty">No human decision is currently required.</div>}
        </section>

        <section className="admin-panel">
          <div className="admin-panel-heading"><h2>Agent run</h2><span>{runs.length} run{runs.length === 1 ? "" : "s"}</span></div>
          {trace ? (
            <div className="admin-trace">
              <h3>{trace.agent_name}</h3><p>{trace.provider} / {trace.model_identifier}</p>
              <div className="admin-stat-row"><span>{trace.turns} turns</span><span>{trace.duration_ms} ms</span><span>{trace.status}</span></div>
              <h4>Tools used</h4>
              <ul>{trace.tools.map((tool) => <li key={tool.id}><strong>{tool.tool_name}</strong><span>{tool.status} · {tool.duration_ms} ms</span></li>)}</ul>
              <details><summary>Trace events</summary><pre>{JSON.stringify(trace.trace?.events ?? [], null, 2)}</pre></details>
            </div>
          ) : <div className="admin-empty">No agent run yet.</div>}
        </section>

        <section className="admin-panel">
          <div className="admin-panel-heading"><h2>Artifacts</h2><span>{artifacts.length}</span></div>
          <div className="admin-artifact-list">{artifacts.map((artifact) => <article key={artifact.id}><div><strong>{artifact.logical_name}</strong><span>{artifact.artifact_type} · {artifact.producer}</span></div><code>{artifact.sha256.slice(0, 16)}…</code></article>)}</div>
        </section>

        <section className="admin-panel admin-errors-panel">
          <div className="admin-panel-heading"><h2>Errors</h2><span>{errors.length}</span></div>
          {errors.length === 0 ? <div className="admin-empty">No workflow errors.</div> : errors.map((item) => <article className="admin-error-row" key={item.id}><strong>{item.code}</strong><p>{item.safe_message}</p><span>{item.retryable ? "Retryable" : "Terminal"}</span></article>)}
        </section>
      </div>
    </section>
  );
}
