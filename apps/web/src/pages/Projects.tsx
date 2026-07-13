// Projects — ported from prototype-v2. This is a PLANNED product area: there is no persistence /
// history backend yet, so every row is illustrative placeholder content from src/mock (never a
// real saved analysis). The screen is explicitly badged so it can never be mistaken for real data.

import { Link } from "react-router-dom";

import { MOCK_PROJECTS } from "../mock/prototypeMock";
import { PlannedBadge } from "../components/PlannedBadge";

export function Projects() {
  return (
    <div>
      <div className="page-header">
        <div>
          <p className="eyebrow">Workspace history</p>
          <h1>
            Projects and results <PlannedBadge />
          </h1>
          <p className="page-copy">
            A planned area for returning to a saved screening result with its input context, model
            versions and evidence. No saved-history backend exists yet — the rows below are
            illustrative examples, not real analyses.
          </p>
        </div>
        <Link className="button primary" to="/analyze">
          New analysis
        </Link>
      </div>

      <div className="project-toolbar">
        <input placeholder="Search results" aria-label="Search results" disabled />
        <select aria-label="Filter status" disabled>
          <option>All results</option>
        </select>
      </div>
      <div className="projects-table">
        <div className="project-head">
          <span>Result ID</span>
          <span>Input</span>
          <span>Context</span>
          <span>Summary</span>
          <span>Updated</span>
        </div>
        {MOCK_PROJECTS.map((row) => (
          <div className="projects-row-static" key={row.id}>
            <span>
              <strong>{row.id}</strong>
              <small>Example</small>
            </span>
            <span>{row.input}</span>
            <span>{row.context}</span>
            <span className={row.positive ? "call-positive" : ""}>{row.summary}</span>
            <span>{row.updated}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
