import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api, EndoscanApiError } from "../api/client";
import type { AdminAgentCapabilities, AdminBuild, AdminProviderPreflight } from "../api/types";
import {
  BuildFilter,
  buildFilter,
  buildStatusLabel,
  relativeTime,
  shortBuildId,
  stageLabel,
  stageProgress,
} from "../adminPresentation";
import { AdminModal } from "../components/AdminModal";

function slugify(value: string): string {
  return value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

const FILTERS: Array<{ value: BuildFilter; label: string }> = [
  { value: "all", label: "All" },
  { value: "review", label: "Needs review" },
  { value: "running", label: "Running" },
  { value: "paused", label: "Paused" },
  { value: "failed", label: "Failed" },
  { value: "completed", label: "Completed" },
];

function preflightLabel(result: AdminProviderPreflight): string {
  if (result.authentication_accepted && result.model_accessible) return "Provider access confirmed";
  if (!result.api_key_present) return "Invalid configuration";
  if (result.http_status === 401) return "Authentication rejected";
  if ([403, 404].includes(result.http_status ?? 0)) return "Model not accessible";
  if (result.http_status === 400) return "Invalid configuration";
  return "Provider temporarily unavailable";
}

export function AdminEndpoints() {
  const navigate = useNavigate();
  const newButton = useRef<HTMLButtonElement>(null);
  const [builds, setBuilds] = useState<AdminBuild[]>([]);
  const [capabilities, setCapabilities] = useState<AdminAgentCapabilities | null>(null);
  const [preflight, setPreflight] = useState<AdminProviderPreflight | null>(null);
  const [name, setName] = useState("Oxidative stress");
  const [goal, setGoal] = useState(
    "Evaluate a response-defined oxidative-stress endpoint from transcriptomic signatures.",
  );
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<BuildFilter>("all");
  const [createOpen, setCreateOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [checkingProvider, setCheckingProvider] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const next = await api.adminListBuilds();
      setBuilds([...next].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at)));
      setCapabilities(await api.adminCapabilities().catch(() => null));
      setError(null);
    } catch {
      setError("Endpoint builds could not be loaded. Check the local service and try again.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const visibleBuilds = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return builds.filter((build) => {
      const matchesQuery = !normalized || `${build.endpoint_name} ${build.id}`.toLowerCase().includes(normalized);
      const matchesFilter = filter === "all" || buildFilter(build) === filter;
      return matchesQuery && matchesFilter;
    });
  }, [builds, filter, query]);

  const needsReview = builds.filter((build) => buildFilter(build) === "review").length;
  const running = builds.filter((build) => buildFilter(build) === "running").length;

  async function create(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const build = await api.adminCreateBuild({
        endpoint_name: name,
        endpoint_slug: slugify(name),
        biological_goal: goal,
        created_by: "local-admin",
      });
      setCreateOpen(false);
      navigate(`/admin/endpoints/${encodeURIComponent(build.id)}`);
    } catch (reason) {
      setError(
        reason instanceof EndoscanApiError
          ? "The endpoint draft could not be created. Review the fields and try again."
          : "The endpoint draft could not be created. Try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function checkProviderAccess() {
    setCheckingProvider(true);
    setError(null);
    try {
      setPreflight(await api.adminProviderPreflight());
    } catch {
      setError("Provider access could not be checked. No agent run was started.");
    } finally {
      setCheckingProvider(false);
    }
  }

  return (
    <section className="admin-page" aria-labelledby="admin-endpoints-title">
      <header className="admin-heading admin-list-page-heading">
        <div>
          <h1 id="admin-endpoints-title">Endpoint builds</h1>
          <p>Review durable agent-assisted workflows and the human decisions waiting on you.</p>
        </div>
        <div className="admin-heading-actions">
          <button className="admin-secondary admin-icon-button" onClick={() => void load()} aria-label="Refresh endpoint builds">
            Refresh
          </button>
          <button ref={newButton} className="admin-primary" onClick={() => setCreateOpen(true)}>+ New endpoint</button>
        </div>
      </header>

      <div className="admin-mode-row" role="note">
        <strong>{capabilities?.run_mode === "live" ? "Live agent mode" : capabilities?.run_mode === "cached" ? "Cached mode" : "Replay mode"}</strong>
        <span>{capabilities ? `${capabilities.provider} · ${capabilities.model} · API key present: ${capabilities.api_key_present ? "yes" : "no"} · source tools: ${capabilities.source_tools_available ? "available" : "unavailable"} · tracing: ${capabilities.tracing_enabled ? "enabled" : "disabled"} · budget: ${capabilities.configured_budget.maximum_tool_calls} tools / $${capabilities.configured_budget.maximum_cost_usd.toFixed(2)}` : "Configuration status is unavailable; the Admin Console remains usable with replay data."}</span>
        {capabilities && !capabilities.live_mode_enabled && <span>Live runs are disabled because no OpenAI API key is configured. Replay remains available.</span>}
        <button className="admin-secondary" disabled={checkingProvider} onClick={() => void checkProviderAccess()}>
          {checkingProvider ? "Checking provider access..." : "Check provider access"}
        </button>
      </div>

      {preflight && (
        <section className="admin-provider-preflight" aria-live="polite">
          <strong>{preflightLabel(preflight)}</strong>
          <span>API credentials accepted: {preflight.authentication_accepted ? "yes" : "no"}</span>
          <span>Configured model accessible: {preflight.model_accessible ? "yes" : "no"}</span>
          <span>Billing and generation: not checked</span>
          <span>Checked {new Date(preflight.checked_at).toLocaleString()}</span>
        </section>
      )}

      {error && <div className="admin-error" role="alert">{error}</div>}

      <section className="admin-summary" aria-label="Build status summary">
        <div><strong>{builds.length}</strong><span>Total builds</span></div>
        <div><strong>{needsReview}</strong><span>Need review</span></div>
        <div><strong>{running}</strong><span>Running</span></div>
      </section>

      <div className="admin-list-toolbar">
        <label className="admin-search">
          <span className="sr-only">Search builds</span>
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search endpoint name or build ID"
          />
        </label>
        <div className="admin-filter-tabs" role="group" aria-label="Filter builds by status">
          {FILTERS.map((item) => (
            <button
              key={item.value}
              type="button"
              aria-pressed={filter === item.value}
              onClick={() => setFilter(item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <span className="admin-sort-label">Sorted by last updated</span>
      </div>

      <div className="admin-build-list" aria-live="polite" aria-busy={loading}>
        {loading ? (
          <div className="admin-list-skeleton" role="status">Loading endpoint builds...</div>
        ) : visibleBuilds.length === 0 && builds.length === 0 ? (
          <div className="admin-empty admin-empty-builds">
            <h2>No endpoint builds yet.</h2>
            <p>Create a new endpoint to start an agent-assisted workflow.</p>
            <button className="admin-primary" onClick={() => setCreateOpen(true)}>+ New endpoint</button>
          </div>
        ) : visibleBuilds.length === 0 ? (
          <div className="admin-empty"><strong>No builds match these filters.</strong><span>Try a different search or status.</span></div>
        ) : (
          visibleBuilds.map((build) => {
            const progress = stageProgress(build);
            return (
              <article className="admin-build-row" key={build.id}>
                <div className="admin-build-identity">
                  <div className="admin-card-title-row">
                    <h2>{build.endpoint_name}</h2>
                    <span className={`admin-status admin-status-${buildFilter(build)}`}>{buildStatusLabel(build)}</span>
                  </div>
                  <p className="admin-build-number">Build {shortBuildId(build.id)}</p>
                  <p>{build.biological_goal}</p>
                </div>
                <div className="admin-row-stage">
                  <span>Current stage</span>
                  <strong>{stageLabel(build.current_stage)}</strong>
                  <div className="admin-progress" role="progressbar" aria-valuemin={0} aria-valuemax={progress.total} aria-valuenow={progress.completed} aria-label={`${progress.completed} of ${progress.total} stages completed`}>
                    <span style={{ width: `${(progress.completed / progress.total) * 100}%` }} />
                  </div>
                  <small>{progress.completed} of {progress.total} stages completed</small>
                </div>
                <div className="admin-row-meta">
                  {build.pending_approval_id && <span className="admin-review-flag">Review required</span>}
                  <span>Updated {relativeTime(build.updated_at)}</span>
                  <span>Created {new Date(build.created_at).toLocaleDateString()}</span>
                  <Link className="admin-primary" to={`/admin/endpoints/${encodeURIComponent(build.id)}`} aria-label={`Open ${build.endpoint_name}, build ${shortBuildId(build.id)}`}>
                    Open build
                  </Link>
                </div>
              </article>
            );
          })
        )}
      </div>

      {createOpen && (
        <AdminModal
          title="Create endpoint draft"
          description="Start a governed workflow using the configured live, cached, or replay discovery mode."
          onClose={() => setCreateOpen(false)}
          returnFocus={newButton.current}
        >
          <form className="admin-create-form" onSubmit={create}>
            <label>
              Endpoint name
              <input data-autofocus value={name} onChange={(event) => setName(event.target.value)} required />
            </label>
            <label>
              Biological goal
              <textarea value={goal} onChange={(event) => setGoal(event.target.value)} required />
            </label>
            <div className="admin-modal-actions">
              <button type="button" className="admin-secondary" onClick={() => setCreateOpen(false)}>Cancel</button>
              <button className="admin-primary" disabled={busy}>{busy ? "Creating..." : "Create endpoint"}</button>
            </div>
          </form>
        </AdminModal>
      )}
    </section>
  );
}
