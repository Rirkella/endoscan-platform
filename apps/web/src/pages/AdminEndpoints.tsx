import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api, EndoscanApiError } from "../api/client";
import type { AdminBuild } from "../api/types";

function slugify(value: string): string {
  return value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

export function AdminEndpoints() {
  const navigate = useNavigate();
  const [builds, setBuilds] = useState<AdminBuild[]>([]);
  const [name, setName] = useState("Oxidative stress");
  const [goal, setGoal] = useState(
    "Evaluate a response-defined oxidative-stress endpoint from transcriptomic signatures.",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      setBuilds(await api.adminListBuilds());
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to load endpoint builds.");
    }
  };

  useEffect(() => {
    void load();
  }, []);

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
      navigate(`/admin/endpoints/${encodeURIComponent(build.id)}`);
    } catch (reason) {
      setError(
        reason instanceof EndoscanApiError ? reason.detail : "Endpoint build could not be created.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="admin-page" aria-labelledby="admin-endpoints-title">
      <div className="admin-heading">
        <div>
          <span className="admin-eyebrow">Agent foundation / Phase 0</span>
          <h1 id="admin-endpoints-title">Endpoint builds</h1>
          <p>Durable workflow control, evidence review, and human approval.</p>
        </div>
        <span className="admin-simulation-label">Prepared deterministic agent simulation</span>
      </div>

      <div className="admin-notice" role="note">
        No live AI or dataset discovery is enabled. This local-development console uses a
        deterministic fake provider and cannot publish the production endpoint registry.
      </div>

      {error && <div className="admin-error" role="alert">{error}</div>}

      <form className="admin-create-card" onSubmit={create}>
        <div>
          <span className="admin-section-kicker">New governed workflow</span>
          <h2>Create endpoint draft</h2>
        </div>
        <label>
          Endpoint name
          <input value={name} onChange={(event) => setName(event.target.value)} required />
        </label>
        <label className="admin-wide-field">
          Biological goal
          <textarea value={goal} onChange={(event) => setGoal(event.target.value)} required />
        </label>
        <button className="admin-primary" disabled={busy}>{busy ? "Creating…" : "Create endpoint"}</button>
      </form>

      <div className="admin-list-heading">
        <h2>Persisted builds</h2>
        <button className="admin-secondary" onClick={() => void load()}>Refresh</button>
      </div>
      <div className="admin-build-list">
        {builds.length === 0 ? (
          <div className="admin-empty">No endpoint builds yet.</div>
        ) : (
          builds.map((build) => (
            <article className="admin-build-row" key={build.id}>
              <div>
                <span className={`admin-status admin-status-${build.status}`}>{build.status}</span>
                <h3>{build.endpoint_name}</h3>
                <p>{build.biological_goal}</p>
              </div>
              <div className="admin-row-stage">
                <span>Current stage</span>
                <strong>{build.current_stage.split("_").join(" ")}</strong>
                <div className="admin-progress" aria-label={`${build.progress}% complete`}>
                  <span style={{ width: `${build.progress}%` }} />
                </div>
              </div>
              <div className="admin-row-meta">
                {build.pending_approval_id && <span>Approval pending</span>}
                <time>{new Date(build.updated_at).toLocaleString()}</time>
                <Link className="admin-primary" to={`/admin/endpoints/${encodeURIComponent(build.id)}`}>
                  Open
                </Link>
              </div>
            </article>
          ))
        )}
      </div>
    </section>
  );
}
