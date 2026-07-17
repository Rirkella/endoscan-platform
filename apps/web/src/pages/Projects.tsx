import { useState } from "react";
import { Link } from "react-router-dom";

import { predictionScore } from "../api/types";
import { ErrorNotice } from "../components/ErrorNotice";
import { guestAnalysisRepository, type GuestAnalysisRecord } from "../session/guestAnalysis";
import { useGuestAnalyses } from "../session/useGuestAnalyses";

export function Projects() {
  const { analyses, loading, error, reload } = useGuestAnalyses();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null);
  const [confirmingClear, setConfirmingClear] = useState(false);
  const [name, setName] = useState("");
  const [actionError, setActionError] = useState<unknown>(null);

  async function rename(record: GuestAnalysisRecord) {
    try {
      await guestAnalysisRepository.rename(record.id, name);
      setEditingId(null);
      reload();
    } catch (nextError) {
      setActionError(nextError);
    }
  }

  async function remove(record: GuestAnalysisRecord) {
    if (confirmingDeleteId !== record.id) {
      setConfirmingDeleteId(record.id);
      return;
    }
    try {
      await guestAnalysisRepository.delete(record.id);
      setConfirmingDeleteId(null);
      reload();
    } catch (nextError) {
      setActionError(nextError);
    }
  }

  async function clearSession() {
    if (!confirmingClear) {
      setConfirmingClear(true);
      return;
    }
    try {
      await guestAnalysisRepository.clearCurrentSession();
      setConfirmingClear(false);
      reload();
    } catch (nextError) {
      setActionError(nextError);
    }
  }

  return (
    <div className="session-projects">
      <div className="page-header">
        <div>
          <p className="eyebrow">Guest workspace</p>
          <h1>Session analyses</h1>
          <p className="page-copy">Analyses are stored locally in this browser session. No account is required.</p>
        </div>
        <Link className="button primary" to="/analyze">New analysis</Link>
      </div>

      <div className="session-projects-notice">
        <span>Stored locally in this browser for the current guest session.</span>
        {analyses.length > 0 && (
          <span className="session-confirm-actions">
            <button className={confirmingClear ? "button danger" : "detail-link"} type="button" onClick={clearSession}>
              {confirmingClear ? "Confirm clear" : "Clear session analyses"}
            </button>
            {confirmingClear && <button className="detail-link" type="button" onClick={() => setConfirmingClear(false)}>Cancel</button>}
          </span>
        )}
      </div>

      {(error != null || actionError != null) && <ErrorNotice error={actionError ?? error} />}
      {loading && <p className="check-plain">Loading session analyses…</p>}

      {!loading && analyses.length === 0 && (
        <section className="projects-empty">
          <h2>No analyses in this session yet.</h2>
          <p>Run an analysis and it will appear here automatically.</p>
          <Link className="button primary" to="/analyze">Start an analysis</Link>
        </section>
      )}

      {analyses.length > 0 && (
        <div className="session-analysis-list" aria-label="Saved analyses">
          {analyses.map((record) => (
            <article className="session-analysis-card" key={record.id}>
              <div className="session-analysis-main">
                <div className="session-analysis-heading">
                  {editingId === record.id ? (
                    <form onSubmit={(event) => { event.preventDefault(); void rename(record); }}>
                      <label>
                        Analysis name
                        <input value={name} onChange={(event) => setName(event.target.value)} autoFocus />
                      </label>
                      <button className="detail-link" type="submit">Save name</button>
                      <button className="detail-link" type="button" onClick={() => setEditingId(null)}>Cancel</button>
                    </form>
                  ) : <h2>{displayName(record)}</h2>}
                  <span className={`session-status status-${record.status}`}>{formatStatus(record.status)}</span>
                </div>
                <p className="session-analysis-source">{sourceLabel(record)} · {record.prepared_input.subtitle}</p>
                <p className="session-endpoint-summary">{endpointSummary(record)}</p>
                {record.experimental_context && <p className="session-context">{record.experimental_context}</p>}
                <dl className="session-analysis-times">
                  <div><dt>Created</dt><dd>{formatDate(record.created_at)}</dd></div>
                  <div><dt>Updated</dt><dd>{formatRelative(record.updated_at)}</dd></div>
                  <div><dt>Compatible endpoints</dt><dd>{record.parse_result.compatible_endpoint_ids.length}</dd></div>
                  <div><dt>Generated with</dt><dd>EndoScan {record.application_version}</dd></div>
                </dl>
              </div>
              <div className="session-analysis-actions">
                <Link className="button primary" to={analysisUrl(record)}>Open</Link>
                <button className="button secondary" type="button" onClick={() => { setEditingId(record.id); setName(displayName(record)); }}>Rename</button>
                <button className="button danger" type="button" onClick={() => void remove(record)}>
                  {confirmingDeleteId === record.id ? "Confirm delete" : "Delete"}
                </button>
                {confirmingDeleteId === record.id && (
                  <button className="detail-link" type="button" onClick={() => setConfirmingDeleteId(null)}>Cancel</button>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}

function displayName(record: GuestAnalysisRecord): string {
  return record.user_defined_name || record.title;
}

function analysisUrl(record: GuestAnalysisRecord): string {
  const params = new URLSearchParams({ tab: record.last_viewed_tab });
  if (record.selected_endpoint) params.set("endpoint", record.selected_endpoint);
  return `/analyze/${encodeURIComponent(record.id)}?${params.toString()}`;
}

function sourceLabel(record: GuestAnalysisRecord): string {
  if (record.input_source === "catalogue") return "Public measured signature";
  if (record.input_source === "reference") return "Aggregated reference profile";
  if (record.input_source === "file") return "Uploaded file";
  return "Pasted signature";
}

function endpointSummary(record: GuestAnalysisRecord): string {
  const signals = record.analysis_result?.signals ?? [];
  if (signals.length === 0) return `${record.parse_result.compatible_endpoint_ids.length} compatible endpoints · awaiting model run`;
  return signals.map((signal) => signal.result
    ? `${signal.endpoint_id} ${signal.result.call ? "above" : "below"} threshold (${predictionScore(signal.result).toFixed(2)})`
    : `${signal.endpoint_id} unavailable`).join(" · ");
}

function formatStatus(status: GuestAnalysisRecord["status"]): string {
  return status.replace(/_/g, " ").replace(/^./, (letter) => letter.toUpperCase());
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function formatRelative(value: string): string {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  return formatDate(value);
}
