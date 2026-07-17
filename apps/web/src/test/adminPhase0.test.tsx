import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AdminAgentRun,
  AdminApproval,
  AdminArtifact,
  AdminBuild,
  AdminTimelineEvent,
  AdminWorkflowError,
} from "../api/types";
import { installFetchMock } from "./mockApi";
import { renderApp } from "./renderApp";

const buildId = "build-00000000-0000-4000-8000-000000000001";
const artifactId = "art-00000000-0000-4000-8000-000000000002";
const approvalId = "approval-00000000-0000-4000-8000-000000000003";
const runId = "run-00000000-0000-4000-8000-000000000004";
const now = "2026-07-17T10:00:00Z";

function build(stage = "AWAITING_DATASET_APPROVAL", version = 3): AdminBuild {
  return {
    schema_version: "1.0.0",
    id: buildId,
    endpoint_name: "Oxidative stress",
    endpoint_slug: "oxidative-stress",
    biological_goal: "Evaluate a response-defined oxidative-stress endpoint.",
    state: stage,
    status: stage === "PAUSED" ? "paused" : stage === "FAILED" ? "failed" : "active",
    current_stage: stage,
    version,
    progress: 14,
    pending_approval_id: stage === "AWAITING_DATASET_APPROVAL" ? approvalId : null,
    paused_from_state: stage === "PAUSED" ? "CURATING_DATA" : null,
    failed_from_state: stage === "FAILED" ? "CURATING_DATA" : null,
    created_by: "local-admin",
    created_at: now,
    updated_at: now,
    paused_at: stage === "PAUSED" ? now : null,
    cancelled_at: null,
    completed_at: null,
  };
}

const artifact: AdminArtifact = {
  schema_version: "1.0.0",
  id: artifactId,
  workflow_id: buildId,
  step_id: "step-discovery",
  sha256: "a".repeat(64),
  size_bytes: 1024,
  mime_type: "application/json",
  artifact_type: "dataset_candidates",
  logical_name: "phase-0-dataset-candidates.json",
  producer: "dataset-discovery-agent",
  original_source: "prepared-phase-0-fixtures",
  created_at: now,
};

const approval: AdminApproval = {
  schema_version: "1.0.0",
  id: approvalId,
  workflow_id: buildId,
  stage: "AWAITING_DATASET_APPROVAL",
  approval_type: "dataset_selection",
  status: "pending",
  proposal_hash: "b".repeat(64),
  request: {
    proposed_decision: "Select one prepared candidate for the Phase-0 demonstration.",
    evidence_summary: "Two deterministic candidate fixtures were compared.",
    source_references: ["prepared://phase-0/candidate-a"],
    limitations: ["Prepared fixture; not a live scientific discovery."],
    artifact_hashes: [artifact.sha256],
    agent_recommendation: "Candidate A is the deterministic default.",
    requested_action: "Approve, reject, request revision, or choose an alternative.",
  },
  decision: null,
  created_at: now,
  decided_at: null,
};

const events: AdminTimelineEvent[] = [
  {
    schema_version: "1.0.0",
    id: "event-1",
    sequence: 1,
    event_type: "workflow.created",
    actor_type: "human",
    actor_id: "local-admin",
    from_state: null,
    to_state: "DRAFT",
    payload: {},
    event_hash: "c".repeat(64),
    created_at: now,
  },
  {
    schema_version: "1.0.0",
    id: "event-2",
    sequence: 2,
    event_type: "approval.created",
    actor_type: "agent",
    actor_id: "dataset-discovery-agent",
    from_state: "DISCOVERING_DATA",
    to_state: "AWAITING_DATASET_APPROVAL",
    payload: {},
    event_hash: "d".repeat(64),
    created_at: now,
  },
];

const run: AdminAgentRun = {
  schema_version: "1.0.0",
  id: runId,
  workflow_id: buildId,
  step_id: "step-discovery",
  agent_name: "Dataset Discovery Agent",
  provider: "fake",
  model_identifier: "fake-phase0-v1",
  status: "interrupted_for_approval",
  turns: 2,
  duration_ms: 18,
  usage: { input_tokens: 120, output_tokens: 80, estimated_cost_usd: 0 },
  tools: [
    { id: "tool-1", tool_name: "inspect_endpoint_registry", status: "completed", duration_ms: 2 },
    { id: "tool-2", tool_name: "create_dataset_candidate_artifact", status: "completed", duration_ms: 3 },
  ],
  trace: { events: [{ event_type: "agent.run.started" }, { event_type: "approval.requested" }] },
};

const workflowErrors: AdminWorkflowError[] = [
  {
    schema_version: "1.0.0",
    id: "error-1",
    step_id: "step-curation",
    code: "phase0_controlled_failure",
    category: "controlled_demo",
    retryable: true,
    safe_message: "A controlled retryable Phase-0 failure was recorded.",
    created_at: now,
  },
];

function detailRoutes(current: () => AdminBuild, errors: AdminWorkflowError[] = []) {
  return {
    [`GET /api/admin/endpoint-builds/${buildId}`]: () => ({ body: current() }),
    [`GET /api/admin/endpoint-builds/${buildId}/timeline`]: { body: events },
    [`GET /api/admin/endpoint-builds/${buildId}/artifacts`]: { body: [artifact] },
    [`GET /api/admin/endpoint-builds/${buildId}/approvals`]: { body: [approval] },
    [`GET /api/admin/endpoint-builds/${buildId}/agent-runs`]: { body: [run] },
    [`GET /api/admin/endpoint-builds/${buildId}/errors`]: { body: errors },
    [`GET /api/admin/artifacts/${artifactId}/preview`]: {
      body: {
        artifact,
        content: {
          live_discovery: false,
          candidates: [
            {
              candidate_id: "candidate-a",
              title: "Prepared oxidative-stress fixture A",
              source: "Prepared Phase-0 fixture",
              recommendation: "preferred",
              limitations: ["Not retrieved live"],
            },
            {
              candidate_id: "candidate-b",
              title: "Prepared oxidative-stress fixture B",
              source: "Prepared Phase-0 fixture",
              recommendation: "alternative",
              limitations: ["Not retrieved live"],
            },
          ],
        },
      },
    },
    [`GET /api/admin/agent-runs/${runId}`]: { body: run },
  };
}

beforeEach(() => {
  window.localStorage.clear();
});

describe("Phase-0 endpoint-build list and creation", () => {
  it("renders persisted builds, progress, pending approval, and a safe future stage", async () => {
    installFetchMock({
      "GET /api/admin/endpoint-builds": {
        body: [build(), { ...build("FUTURE_SCIENCE_REVIEW"), id: `${buildId}-future` }],
      },
    });
    renderApp("/admin/endpoints");

    expect(await screen.findAllByRole("heading", { name: "Oxidative stress" })).toHaveLength(2);
    expect(screen.getByText("AWAITING DATASET APPROVAL")).toBeInTheDocument();
    expect(screen.getByText("FUTURE SCIENCE REVIEW")).toBeInTheDocument();
    expect(screen.getAllByText("Approval pending")).toHaveLength(1);
    expect(screen.getAllByText("Prepared deterministic agent simulation").length).toBeGreaterThan(0);
    expect(screen.getAllByRole("link", { name: "Open" })).toHaveLength(2);
  });

  it("creates a build and navigates to its durable detail route", async () => {
    // React Router passes jsdom's AbortSignal to Node's native Request during
    // programmatic navigation.  This narrow test shim preserves the request
    // while avoiding that cross-realm constructor check.
    const NativeRequest = globalThis.Request;
    class NavigationRequest extends NativeRequest {
      constructor(input: RequestInfo | URL, init?: RequestInit) {
        super(input, init ? { ...init, signal: undefined } : init);
      }
    }
    vi.stubGlobal("Request", NavigationRequest);
    let created = false;
    installFetchMock({
      "GET /api/admin/endpoint-builds": { body: [] },
      "POST /api/admin/endpoint-builds": () => {
        created = true;
        return { body: build("DRAFT", 0) };
      },
      ...detailRoutes(() => build("DRAFT", 0)),
    });
    renderApp("/admin/endpoints");

    await screen.findByText("No endpoint builds yet.");
    await userEvent.click(screen.getByRole("button", { name: "Create endpoint" }));
    expect(await screen.findByRole("heading", { name: "Oxidative stress" })).toBeInTheDocument();
    expect(created).toBe(true);
    expect(screen.getByRole("button", { name: "Start" })).toBeInTheDocument();
  });

  it("shows a calm backend error state", async () => {
    installFetchMock({
      "GET /api/admin/endpoint-builds": {
        status: 503,
        body: { error: "workflow_unavailable", detail: "Workflow database is unavailable." },
      },
    });
    renderApp("/admin/endpoints");
    expect(await screen.findByRole("alert")).toHaveTextContent("Workflow database is unavailable.");
  });
});

describe("Phase-0 workflow controls and evidence", () => {
  it("starts, pauses, and resumes using the latest persisted workflow version", async () => {
    let current = build("DRAFT", 0);
    installFetchMock({
      ...detailRoutes(() => current),
      [`POST /api/admin/endpoint-builds/${buildId}/start`]: () => {
        current = build("AWAITING_DATASET_APPROVAL", 3);
        return { body: current };
      },
      [`POST /api/admin/endpoint-builds/${buildId}/pause`]: () => {
        current = build("PAUSED", 4);
        return { body: current };
      },
      [`POST /api/admin/endpoint-builds/${buildId}/resume`]: () => {
        current = build("CURATING_DATA", 5);
        return { body: current };
      },
    });
    renderApp(`/admin/endpoints/${buildId}`);

    await userEvent.click(await screen.findByRole("button", { name: "Start" }));
    await screen.findByText("AWAITING DATASET APPROVAL");
    await userEvent.click(screen.getByRole("button", { name: "Pause" }));
    await screen.findByText("PAUSED");
    await userEvent.click(screen.getByRole("button", { name: "Resume" }));
    await screen.findByText("CURATING DATA");

    const payloads = vi.mocked(fetch).mock.calls
      .filter(([, init]) => init?.method === "POST")
      .map(([, init]) => JSON.parse(String(init?.body)) as { expected_version: number });
    expect(payloads.map((item) => item.expected_version)).toEqual([0, 3, 4]);
  });

  it("renders candidates, timeline, artifacts, tools, trace, errors, and submits revision", async () => {
    let revisionDecision = "";
    installFetchMock({
      ...detailRoutes(() => build(), workflowErrors),
      [`POST /api/admin/approvals/${approvalId}/decisions`]: (body) => {
        revisionDecision = (body as { decision: string }).decision;
        return { body: build("DISCOVERING_DATA", 4) };
      },
    });
    renderApp(`/admin/endpoints/${buildId}`);

    expect(await screen.findByText("Prepared oxidative-stress fixture A")).toBeInTheDocument();
    expect(screen.getByText("Prepared oxidative-stress fixture B")).toBeInTheDocument();
    expect(screen.getByText("workflow created")).toBeInTheDocument();
    expect(screen.getByText("phase-0-dataset-candidates.json")).toBeInTheDocument();
    expect(screen.getByText("inspect_endpoint_registry")).toBeInTheDocument();
    expect(screen.getByText("phase0_controlled_failure")).toBeInTheDocument();
    expect(screen.getByText("2 events")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Reviewer comment"), {
      target: { value: "Please clarify fixture selection limits." },
    });
    await userEvent.click(screen.getByRole("button", { name: "Request revision" }));
    await waitFor(() => expect(revisionDecision).toBe("request_revision"));
    expect(document.querySelector(".admin-detail-grid")).toBeInTheDocument();
  });
});
