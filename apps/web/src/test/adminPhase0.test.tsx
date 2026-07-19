import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AdminAgentRun,
  AdminApproval,
  AdminArtifact,
  AdminBuild,
  AdminProviderPreflight,
  AdminTimelineEvent,
  AdminTrainingDatasetWorkflow,
  AdminWorkflowError,
} from "../api/types";
import { installFetchMock } from "./mockApi";
import { renderApp } from "./renderApp";

const buildId = "build-00000000-0000-4000-8000-000000000001";
const artifactId = "art-00000000-0000-4000-8000-000000000002";
const approvalId = "approval-00000000-0000-4000-8000-000000000003";
const runId = "run-00000000-0000-4000-8000-000000000004";
const now = "2026-07-17T10:00:00Z";
const writeClipboard = vi.fn().mockResolvedValue(undefined);

function build(stage = "AWAITING_DATASET_APPROVAL", version = 3, overrides: Partial<AdminBuild> = {}): AdminBuild {
  return {
    schema_version: "1.0.0",
    id: buildId,
    endpoint_name: "Oxidative stress",
    endpoint_slug: "oxidative-stress",
    biological_goal: "Evaluate a response-defined oxidative-stress endpoint.",
    state: stage,
    status: stage === "PAUSED" ? "paused" : stage === "FAILED" ? "failed" : stage === "COMPLETED" ? "completed" : stage.startsWith("AWAITING") ? "waiting" : "active",
    current_stage: stage,
    version,
    progress: stage === "COMPLETED" ? 100 : 15,
    pending_approval_id: stage === "AWAITING_DATASET_APPROVAL" ? approvalId : null,
    paused_from_state: stage === "PAUSED" ? "CURATING_DATA" : null,
    failed_from_state: stage === "FAILED" ? "CURATING_DATA" : null,
    created_by: "local-admin",
    created_at: now,
    updated_at: now,
    paused_at: stage === "PAUSED" ? now : null,
    cancelled_at: null,
    completed_at: stage === "COMPLETED" ? now : null,
    ...overrides,
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
    schema_version: "1.0.0", id: "event-1", sequence: 1, event_type: "workflow.created",
    actor_type: "human", actor_id: "local-admin", from_state: null, to_state: "DRAFT",
    payload: {}, event_hash: "c".repeat(64), created_at: now,
  },
  {
    schema_version: "1.0.0", id: "event-2", sequence: 2, event_type: "approval.created",
    actor_type: "agent", actor_id: "dataset-discovery-agent", from_state: "DISCOVERING_DATA",
    to_state: "AWAITING_DATASET_APPROVAL", payload: {}, event_hash: "d".repeat(64), created_at: now,
  },
];

const run: AdminAgentRun = {
  schema_version: "1.0.0", id: runId, workflow_id: buildId, step_id: "step-discovery",
  agent_name: "Dataset Discovery Agent", provider: "fake", model_identifier: "fake-phase0-v1",
  run_mode: "replay", status: "approval_required", turns: 2, duration_ms: 18,
  usage: { input_tokens: 120, output_tokens: 80, estimated_cost_usd: 0 },
  tools: [
    { id: "tool-1", tool_name: "inspect_endpoint_registry", status: "completed", duration_ms: 2 },
    { id: "tool-2", tool_name: "create_dataset_candidate_artifact", status: "completed", duration_ms: 3 },
  ],
  trace: { events: [{ event_type: "agent.run.started" }, { event_type: "approval.requested" }] },
};

const workflowErrors: AdminWorkflowError[] = [{
  schema_version: "1.0.0", id: "error-1", step_id: "step-curation",
  code: "phase0_controlled_failure", category: "controlled_demo", retryable: true,
  safe_message: "A controlled retryable Phase-0 failure was recorded.", created_at: now,
}];

const candidates = [
  {
    candidate_id: "candidate-a", title: "Prepared oxidative-stress fixture A",
    source: "Prepared Phase-0 fixture", recommendation: "preferred",
    limitations: ["Not retrieved live"], sample_count: 168, controls_available: true,
    data_type: "Transcriptomic", context: "One prepared cell model",
  },
  {
    candidate_id: "candidate-b", title: "Prepared oxidative-stress fixture B",
    source: "Prepared Phase-0 fixture", recommendation: "alternative",
    limitations: ["Not retrieved live"], controls_available: false,
  },
];

const replayCapabilities = {
  schema_version: "1.0.0",
  provider: "fake",
  model: "prepared-fixture",
  run_mode: "replay",
  api_key_present: false,
  live_mode_enabled: false,
  source_tools_available: false,
  tracing_enabled: false,
  configured_budget: {
    maximum_turns: 6,
    maximum_tool_calls: 6,
    timeout_seconds: 120,
    maximum_input_tokens: 8000,
    maximum_output_tokens: 1500,
    maximum_cost_usd: 0.2,
    retry_count: 0,
    input_cost_per_million_usd: 0,
    output_cost_per_million_usd: 0,
  },
} as const;

function detailRoutes(
  current: () => AdminBuild,
  errors: AdminWorkflowError[] = [],
) {
  return {
    "GET /api/admin/capabilities": { body: replayCapabilities },
    [`GET /api/admin/endpoint-builds/${buildId}`]: () => ({ body: current() }),
    [`GET /api/admin/endpoint-builds/${buildId}/timeline`]: { body: events },
    [`GET /api/admin/endpoint-builds/${buildId}/artifacts`]: { body: [artifact] },
    [`GET /api/admin/endpoint-builds/${buildId}/approvals`]: { body: [approval] },
    [`GET /api/admin/endpoint-builds/${buildId}/agent-runs`]: { body: [run] },
    [`GET /api/admin/endpoint-builds/${buildId}/errors`]: { body: errors },
    [`GET /api/admin/endpoint-builds/${buildId}/training-dataset-workflow`]: {
      body: {
        schema_version: "1.0.0",
        workflow_id: buildId,
        workflow_kind: "legacy_single_source_discovery",
        legacy: true,
        label: "Legacy single-source discovery",
      },
    },
    [`GET /api/admin/artifacts/${artifactId}/preview`]: { body: { artifact, content: { live_discovery: false, candidates } } },
    [`GET /api/admin/agent-runs/${runId}`]: { body: run },
  };
}

beforeEach(() => {
  window.localStorage.clear();
  writeClipboard.mockClear();
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: writeClipboard },
  });
});

describe("Phase-0 endpoint-build list and creation", () => {
  it("puts sorted persisted builds first and keeps duplicate names distinguishable", async () => {
    const older = build("AWAITING_DATASET_APPROVAL", 3, { id: `${buildId}-old`, updated_at: "2026-07-16T10:00:00Z" });
    const newer = build("FUTURE_SCIENCE_REVIEW", 4, { id: `${buildId}-new`, updated_at: "2026-07-18T10:00:00Z", pending_approval_id: null });
    installFetchMock({ "GET /api/admin/endpoint-builds": { body: [older, newer] } });
    renderApp("/admin/endpoints");

    const cards = await screen.findAllByRole("article");
    expect(cards).toHaveLength(2);
    expect(cards[0]).toHaveTextContent("Future science review");
    expect(within(cards[0]).getByText(/Build 00000000/)).toBeInTheDocument();
    expect(screen.getAllByText("Waiting for dataset review")).toHaveLength(2);
    expect(screen.getByText("Review required")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: /Open Oxidative stress, build/ })).toHaveLength(2);
    expect(screen.getAllByText("Replay mode")).toHaveLength(1);
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
  }, 10_000);

  it("opens creation in a focus-managed dialog and creates a durable route", async () => {
    const NativeRequest = globalThis.Request;
    class NavigationRequest extends NativeRequest {
      constructor(input: RequestInfo | URL, init?: RequestInit) { super(input, init ? { ...init, signal: undefined } : init); }
    }
    vi.stubGlobal("Request", NavigationRequest);
    installFetchMock({
      "GET /api/admin/endpoint-builds": { body: [] },
      "POST /api/admin/endpoint-builds": { body: build("DRAFT", 0) },
      ...detailRoutes(() => build("DRAFT", 0)),
    });
    renderApp("/admin/endpoints");

    await screen.findByText("No endpoint builds yet.");
    const open = screen.getAllByRole("button", { name: "+ New endpoint" })[0];
    await userEvent.click(open);
    const dialog = screen.getByRole("dialog", { name: "Create endpoint draft" });
    expect(within(dialog).getByLabelText("Endpoint name")).toHaveFocus();
    await userEvent.click(within(dialog).getByRole("button", { name: "Close dialog" }));
    expect(open).toHaveFocus();
    await userEvent.click(open);
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Create endpoint" }));
    expect(await screen.findByRole("heading", { name: "Oxidative stress", level: 1 })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start workflow" })).toBeInTheDocument();
  });

  it("searches and filters builds", async () => {
    installFetchMock({ "GET /api/admin/endpoint-builds": { body: [build(), build("FAILED", 4, { id: `${buildId}-failed`, endpoint_name: "Metabolic stress" })] } });
    renderApp("/admin/endpoints");
    await screen.findByText("Metabolic stress");
    await userEvent.type(screen.getByRole("searchbox", { name: "Search builds" }), "Oxidative");
    expect(screen.queryByText("Metabolic stress")).not.toBeInTheDocument();
    await userEvent.clear(screen.getByRole("searchbox", { name: "Search builds" }));
    await userEvent.click(screen.getByRole("button", { name: "Failed" }));
    expect(screen.getByText("Metabolic stress")).toBeInTheDocument();
    expect(screen.queryByText("Waiting for dataset review")).not.toBeInTheDocument();
  });

  it("shows a calm actionable backend error", async () => {
    installFetchMock({ "GET /api/admin/endpoint-builds": { status: 503, body: { error: "workflow_unavailable", detail: "raw backend exception" } } });
    renderApp("/admin/endpoints");
    expect(await screen.findByRole("alert")).toHaveTextContent("Check the local service and try again");
    expect(screen.queryByText("raw backend exception")).not.toBeInTheDocument();
  });
});

describe("Phase-0 build detail information architecture", () => {
  it("renders a deterministic specification draft and pending human review gate", async () => {
    const specificationApproval: AdminApproval = {
      ...approval,
      stage: "AWAITING_DATASET_SPECIFICATION_REVIEW",
      approval_type: "dataset_specification",
      request: {
        ...approval.request,
        proposed_decision: "Approve this target training-dataset specification before public-source discovery begins.",
        evidence_summary: "Draft produced deterministically from the request and approved platform contract.",
      },
    };
    const training: AdminTrainingDatasetWorkflow = {
      schema_version: "1.0.0",
      contract_version: "1.0.0",
      workflow_id: buildId,
      workflow_kind: "training_dataset_discovery",
      benchmark_mode: "blind_training_dataset_discovery",
      legacy: false,
      specification_compilation_outcome: {
        compiler_version: "1.0.0",
        deterministic_hash: "e".repeat(64),
        limitations: ["No scientific source was consulted."],
      },
      specification_review: {
        status: "not_run",
        safe_summary: "Optional AI review has not been requested.",
      },
      specification_draft: {
        biological_target: "Thyroid hormone receptor",
        endpoint_modality: "antagonism",
        intended_prediction_task: "Predict compound activity relative to thyroid hormone receptor antagonism.",
        explicit_prediction_grain: "compound × transcriptomic experimental context",
        mandatory_target_table_fields: ["canonical_compound_id", "canonical_smiles", "inchikey"],
        compound_identity_requirements: ["canonical compound identifier", "InChIKey"],
        chemical_structure_requirements: ["canonical SMILES"],
        acceptable_transcriptomic_evidence_types: ["compound-induced perturbational gene-expression signature"],
        acceptable_activity_evidence_types: ["continuous_activity"],
        experimental_context_requirements: ["cell or tissue model", "dose", "exposure duration"],
        intended_scope_of_claim: "Source-neutral thyroid hormone receptor antagonism only.",
        explicit_exclusions: ["receptor agonism", "thyroid peroxidase inhibition"],
        blocking_questions: [],
        approval_questions: ["Should continuous activity values be preserved, classes derived, or both?"],
        assumptions: ["Final observation grain requires human approval."],
        field_provenance: [
          { field_name: "biological_target", origins: ["user_request", "semantic_hint"] },
          { field_name: "endpoint_modality", origins: ["semantic_hint"] },
        ],
      },
      target_specification: null,
      component_requirements: null,
      verified_source_inventory: null,
      capability_matrix: null,
      assembly_strategies: null,
    };
    installFetchMock({
      ...detailRoutes(() => build("AWAITING_DATASET_SPECIFICATION_REVIEW", 2, {
        workflow_kind: "training_dataset_discovery",
        pending_approval_id: approvalId,
      })),
      [`GET /api/admin/endpoint-builds/${buildId}/approvals`]: { body: [specificationApproval] },
      [`GET /api/admin/endpoint-builds/${buildId}/agent-runs`]: { body: [] },
      [`GET /api/admin/endpoint-builds/${buildId}/training-dataset-workflow`]: { body: training },
    });
    renderApp(`/admin/endpoints/${buildId}`);

    expect(await screen.findAllByRole("heading", { name: "Review target training dataset" })).toHaveLength(2);
    expect(
      screen.getAllByText("Draft produced deterministically from the request and approved platform contract."),
    ).toHaveLength(2);
    expect(screen.getAllByText("Thyroid hormone receptor", { exact: false }).length).toBeGreaterThan(0);
    expect(screen.getByText("compound × transcriptomic experimental context")).toBeInTheDocument();
    expect(screen.getByText("Should continuous activity values be preserved, classes derived, or both?")).toBeInTheDocument();
    expect(screen.getByText("Optional AI review has not been requested.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run optional AI review" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve target specification" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit approval choices" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Approve dataset" })).not.toBeInTheDocument();
    expect(screen.getByText("No agent run yet.")).toBeInTheDocument();
  });

  it("renders a source-neutral multi-source training-dataset workspace", async () => {
    const training: AdminTrainingDatasetWorkflow = {
      schema_version: "1.0.0",
      contract_version: "1.0.0",
      workflow_id: buildId,
      workflow_kind: "training_dataset_discovery",
      benchmark_mode: "blind_training_dataset_discovery",
      legacy: false,
      target_specification: {
        intended_prediction_task: "Predict endpoint-relative activity",
        prediction_unit: "compound_context",
        acceptable_activity_representations: ["continuous_activity"],
        mandatory_output_fields: ["canonical_compound_id", "transcriptomic_signature"],
      },
      component_requirements: {
        requirements: [{ requirement_id: "activity", role: "endpoint_activity", mandatory: true }],
      },
      verified_source_inventory: {
        sources: [{
          source_id: "source-a",
          source_system: "official_activity_source",
          stable_accession: "A-1",
          source_roles: ["endpoint_activity"],
          limitations: ["Full records require an approved download."],
        }],
      },
      capability_matrix: {
        cells: [{ source_id: "source-a", component: "endpoint_activity", status: "verified_available" }],
      },
      assembly_review: {
        recommended_strategy_id: "strategy-1",
        strategies: [{
          strategy_id: "strategy-1",
          scientific_risks: ["Context coverage requires review."],
          source_graph: {
            nodes: [
              { node_id: "source", label: "Activity source", node_type: "source" },
              { node_id: "rows", label: "Training rows", node_type: "final_dataset" },
            ],
            edges: [{ from_node: "source", to_node: "rows" }],
          },
          preparation_plan: {
            steps: [{ step_id: "retrieve", action: "Retrieve approved records", status: "requires_download" }],
          },
        }],
      },
      gap_report: { gaps: [{ gap_id: "identity", description: "Identity bridge is unresolved." }] },
    };
    installFetchMock({
      ...detailRoutes(() => build("BUILDING_SOURCE_INVENTORY", 4, {
        workflow_kind: "training_dataset_discovery",
        benchmark_mode: "blind_training_dataset_discovery",
      })),
      [`GET /api/admin/endpoint-builds/${buildId}/approvals`]: { body: [] },
      [`GET /api/admin/endpoint-builds/${buildId}/training-dataset-workflow`]: { body: training },
    });
    renderApp(`/admin/endpoints/${buildId}`);

    expect(await screen.findByTestId("training-dataset-workspace")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Target training dataset" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Verified source inventory" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Capability matrix" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Recommended assembly graph" })).toBeInTheDocument();
    expect(screen.getByText("source → rows")).toBeInTheDocument();
    expect(screen.getByText(/Identity bridge is unresolved/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve dataset" })).not.toBeInTheDocument();
  });

  it("shows compact build and current-run IDs and copies their complete values", async () => {
    installFetchMock(detailRoutes(() => build()));
    renderApp(`/admin/endpoints/${buildId}`);

    expect(await screen.findByText(`Build 00000000`)).toBeInTheDocument();
    expect(screen.getByText(`Run 00000000`)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: `Copy full build ID ${buildId}` }));
    fireEvent.click(screen.getByRole("button", { name: `Copy full run ID ${runId}` }));
    await waitFor(() => expect(writeClipboard).toHaveBeenCalledTimes(2));
    expect(writeClipboard).toHaveBeenNthCalledWith(1, buildId);
    expect(writeClipboard).toHaveBeenNthCalledWith(2, runId);
  });

  it("does not render a run identifier before an agent run exists", async () => {
    installFetchMock({
      ...detailRoutes(() => build("DRAFT", 0)),
      [`GET /api/admin/endpoint-builds/${buildId}/agent-runs`]: { body: [] },
    });
    renderApp(`/admin/endpoints/${buildId}`);

    expect(await screen.findByText("No agent run yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Copy full run ID/ })).not.toBeInTheDocument();
  });

  it("uses authoritative live capabilities before the first training agent run", async () => {
    installFetchMock({
      ...detailRoutes(() => build("SPECIFYING_TARGET_DATASET", 1, {
        workflow_kind: "training_dataset_discovery",
        benchmark_mode: "blind_training_dataset_discovery",
      })),
      "GET /api/admin/capabilities": {
        body: {
          ...replayCapabilities,
          provider: "openai",
          model: "gpt-5.4-mini",
          run_mode: "live",
        },
      },
      [`GET /api/admin/endpoint-builds/${buildId}/agent-runs`]: { body: [] },
    });
    renderApp(`/admin/endpoints/${buildId}`);

    expect(await screen.findByText("Live agent mode")).toBeInTheDocument();
    expect(screen.queryByText("Replay mode")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue active stage" })).toBeInTheDocument();
  });

  it("uses the latest persisted run identifier when multiple attempts exist", async () => {
    const olderRun = { ...run, id: "run-11111111-1111-4111-8111-111111111111" };
    installFetchMock({
      ...detailRoutes(() => build()),
      [`GET /api/admin/endpoint-builds/${buildId}/agent-runs`]: { body: [olderRun, run] },
      [`GET /api/admin/agent-runs/${runId}`]: { body: run },
    });
    renderApp(`/admin/endpoints/${buildId}`);

    expect(await screen.findByRole("button", { name: `Copy full run ID ${runId}` })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: `Copy full run ID ${olderRun.id}` })).not.toBeInTheDocument();
  });

  it("leads with the stepper and decision, compares fixtures, and keeps machine detail secondary", async () => {
    installFetchMock(detailRoutes(() => build(), workflowErrors));
    renderApp(`/admin/endpoints/${buildId}`);

    expect(await screen.findByRole("heading", { name: "Dataset review required" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Workflow progress" })).toBeInTheDocument();
    expect(screen.getByLabelText("Endpoint build stages").children).toHaveLength(10);
    expect(screen.getAllByText("SIM-OS-001")).toHaveLength(2);
    expect(screen.getByText("SIM-OS-002")).toBeInTheDocument();
    expect(screen.getAllByText("Prepared replay fixture")).toHaveLength(2);
    expect(screen.getByText("168")).toBeInTheDocument();
    expect(screen.getByText("Why it is recommended")).toBeInTheDocument();
    expect(screen.getAllByText("Limitations").length).toBeGreaterThan(0);
    expect(screen.getByRole("heading", { name: "Agent activity" })).toBeInTheDocument();
    expect(screen.getByText("Inspected endpoint registry")).toBeInTheDocument();
    expect(screen.getByText("120")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Activity" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Dataset review requested")).toBeInTheDocument();
    expect(document.querySelector(".admin-timeline")).not.toBeInTheDocument();

    const technical = screen.getByText("Technical details").closest("details");
    expect(technical).not.toHaveAttribute("open");
    expect(within(technical!).getByText("AWAITING_DATASET_APPROVAL")).toBeInTheDocument();
    const developer = screen.getByText(/Developer tools/).closest("details");
    expect(developer).not.toHaveAttribute("open");
    expect(within(developer!).getByRole("button", { name: "Trigger controlled failure" })).toBeInTheDocument();
  });

  it("separates readable activity from the complete immutable technical log", async () => {
    installFetchMock(detailRoutes(() => build()));
    renderApp(`/admin/endpoints/${buildId}`);
    await screen.findByText("Dataset review requested");
    expect(screen.queryByText("approval.created")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Technical audit log" }));
    expect(screen.getByText("approval.created")).toBeInTheDocument();
    expect(screen.getByText("workflow.created")).toBeInTheDocument();
    expect(screen.getByText("d".repeat(64))).toBeInTheDocument();
    expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", "audit-tab");
  });

  it("supports keyboard navigation between Activity and Technical audit log", async () => {
    installFetchMock(detailRoutes(() => build()));
    renderApp(`/admin/endpoints/${buildId}`);
    const activity = await screen.findByRole("tab", { name: "Activity" });
    activity.focus();
    fireEvent.keyDown(activity, { key: "ArrowRight" });
    await waitFor(() => expect(screen.getByRole("tab", { name: "Technical audit log" })).toHaveFocus());
    expect(screen.getByRole("tab", { name: "Technical audit log" })).toHaveAttribute("aria-selected", "true");
  });

  it("confirms approval scope before preserving the existing immutable decision request", async () => {
    let payload: Record<string, unknown> | null = null;
    installFetchMock({
      ...detailRoutes(() => build()),
      [`POST /api/admin/approvals/${approvalId}/decisions`]: (body) => { payload = body as Record<string, unknown>; return { body: build("CURATING_DATA", 4) }; },
    });
    renderApp(`/admin/endpoints/${buildId}`);
    await userEvent.click(await screen.findByRole("button", { name: "Approve dataset" }));
    const dialog = screen.getByRole("dialog", { name: "Confirm dataset approval" });
    expect(dialog).toHaveTextContent("SIM-OS-001");
    expect(dialog).toHaveTextContent("Current immutable candidate artifact");
    expect(dialog).toHaveTextContent("Dataset selection for this build only");
    expect(dialog).toHaveTextContent("Prepare the selected dataset");
    await userEvent.click(within(dialog).getByRole("button", { name: "Confirm decision" }));
    await waitFor(() => expect(payload).toMatchObject({ decision: "approve", expected_version: 3 }));
  });

  it("requires a comment and confirms revision and alternative decisions", async () => {
    const decisions: Array<Record<string, unknown>> = [];
    installFetchMock({
      ...detailRoutes(() => build()),
      [`POST /api/admin/approvals/${approvalId}/decisions`]: (body) => { decisions.push(body as Record<string, unknown>); return { body: build("DISCOVERING_DATA", 4) }; },
    });
    renderApp(`/admin/endpoints/${buildId}`);
    const revision = await screen.findByRole("button", { name: "Request revision" });
    expect(revision).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Reviewer comment"), { target: { value: "Clarify the fixture limits." } });
    await userEvent.click(revision);
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Confirm decision" }));
    await waitFor(() => expect(decisions[0]).toMatchObject({ decision: "request_revision", reviewer_comment: "Clarify the fixture limits." }));
  });

  it.each([
    { action: "Reject", decision: "reject", selectAlternative: false },
    { action: "Select another candidate", decision: "choose_alternative", selectAlternative: true },
  ])("records $decision only after comment and confirmation", async ({ action, decision, selectAlternative }) => {
    let payload: Record<string, unknown> | null = null;
    installFetchMock({
      ...detailRoutes(() => build()),
      [`POST /api/admin/approvals/${approvalId}/decisions`]: (body) => { payload = body as Record<string, unknown>; return { body: build("DISCOVERING_DATA", 4) }; },
    });
    renderApp(`/admin/endpoints/${buildId}`);
    await screen.findByRole("heading", { name: "Dataset review required" });
    if (selectAlternative) await userEvent.click(screen.getByLabelText("Select SIM-OS-002"));
    await userEvent.type(screen.getByLabelText("Reviewer comment"), "Reviewer evidence comment.");
    await userEvent.click(screen.getByRole("button", { name: action }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Confirm decision" }));
    await waitFor(() => expect(payload).toMatchObject({
      decision,
      reviewer_comment: "Reviewer evidence comment.",
      ...(selectAlternative ? { selected_alternative_id: "candidate-b" } : {}),
    }));
  });

  it("maps workflow controls to human language without changing versioned commands", async () => {
    let current = build("DRAFT", 0);
    installFetchMock({
      ...detailRoutes(() => current),
      [`POST /api/admin/endpoint-builds/${buildId}/start`]: () => { current = build("AWAITING_DATASET_APPROVAL", 3); return { body: current }; },
      [`POST /api/admin/endpoint-builds/${buildId}/pause`]: () => { current = build("PAUSED", 4); return { body: current }; },
      [`POST /api/admin/endpoint-builds/${buildId}/resume`]: () => { current = build("CURATING_DATA", 5); return { body: current }; },
    });
    renderApp(`/admin/endpoints/${buildId}`);
    await userEvent.click(await screen.findByRole("button", { name: "Start workflow" }));
    await screen.findAllByText("Waiting for dataset review");
    await userEvent.click(screen.getByRole("button", { name: "Pause" }));
    await screen.findAllByText("Paused");
    await userEvent.click(screen.getByRole("button", { name: "Resume" }));
    await screen.findAllByText("Preparing the selected dataset");
    const versions = vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === "POST").map(([, init]) => JSON.parse(String(init?.body)).expected_version);
    expect(versions).toEqual([0, 3, 4]);
  });

  it.each([
    { retryable: true, visible: true },
    { retryable: false, visible: false },
  ])("shows Retry only when the latest workflow error is retryable", async ({ retryable, visible }) => {
    installFetchMock(detailRoutes(
      () => build("FAILED", 4),
      [{ ...workflowErrors[0], retryable }],
    ));
    renderApp(`/admin/endpoints/${buildId}`);
    await screen.findByRole("heading", { name: "Dataset review required" });
    if (visible) {
      expect(await screen.findByRole("button", { name: "Retry failed step" })).toBeInTheDocument();
    } else {
      expect(screen.queryByRole("button", { name: "Retry failed step" })).not.toBeInTheDocument();
    }
  });
});

describe("Phase-1 live discovery presentation", () => {
  it("runs provider preflight only after an explicit administrator action", async () => {
    let preflightCalls = 0;
    const result: AdminProviderPreflight = {
      schema_version: "1.0.0",
      provider: "openai",
      configured_model: "gpt-5.4-mini",
      api_key_present: true,
      authentication_accepted: true,
      model_accessible: true,
      http_status: 200,
      provider_error_code: null,
      provider_error_type: null,
      request_id: "req_preflight-safe",
      billing_status: "not_checked",
      generation_capability: "not_checked",
      checked_at: "2026-07-18T12:00:00Z",
    };
    installFetchMock({
      "GET /api/admin/endpoint-builds": { body: [] },
      "GET /api/admin/capabilities": {
        body: {
          schema_version: "1.0.0",
          provider: "openai",
          model: "gpt-5.4-mini",
          run_mode: "live",
          api_key_present: true,
          live_mode_enabled: true,
          source_tools_available: true,
          tracing_enabled: false,
          configured_budget: {
            maximum_turns: 6,
            maximum_tool_calls: 6,
            timeout_seconds: 120,
            maximum_input_tokens: 8000,
            maximum_output_tokens: 1500,
            maximum_cost_usd: 0.2,
            retry_count: 0,
            input_cost_per_million_usd: 0.75,
            output_cost_per_million_usd: 4.5,
          },
        },
      },
      "POST /api/admin/agent-provider/preflight": () => {
        preflightCalls += 1;
        return { body: result };
      },
    });
    renderApp("/admin/endpoints");
    const button = await screen.findByRole("button", { name: "Check provider access" });
    expect(preflightCalls).toBe(0);
    expect(screen.queryByText("Provider access confirmed")).not.toBeInTheDocument();
    await userEvent.click(button);
    expect(await screen.findByText("Provider access confirmed")).toBeInTheDocument();
    expect(screen.getByText("API credentials accepted: yes")).toBeInTheDocument();
    expect(screen.getByText("Configured model accessible: yes")).toBeInTheDocument();
    expect(screen.getByText("Billing and generation: not checked")).toBeInTheDocument();
    expect(screen.getByText(/provider retries: 0/)).toBeInTheDocument();
    expect(preflightCalls).toBe(1);
  });

  it("renders safe preflight failure categories without raw provider text", async () => {
    const failed: AdminProviderPreflight = {
      schema_version: "1.0.0",
      provider: "openai",
      configured_model: "gpt-5.4-mini",
      api_key_present: true,
      authentication_accepted: false,
      model_accessible: false,
      http_status: 401,
      provider_error_code: "invalid_api_key",
      provider_error_type: "authentication_error",
      request_id: "req_preflight-failed",
      billing_status: "not_checked",
      generation_capability: "not_checked",
      checked_at: "2026-07-18T12:00:00Z",
    };
    installFetchMock({
      "GET /api/admin/endpoint-builds": { body: [] },
      "POST /api/admin/agent-provider/preflight": { body: failed },
    });
    renderApp("/admin/endpoints");
    await userEvent.click(await screen.findByRole("button", { name: "Check provider access" }));
    expect(await screen.findByText("Authentication rejected")).toBeInTheDocument();
    expect(screen.queryByText(/raw provider/i)).not.toBeInTheDocument();
  });

  it("shows provider-unavailable status while keeping replay usable", async () => {
    installFetchMock({
      "GET /api/admin/endpoint-builds": { body: [] },
      "GET /api/admin/capabilities": {
        body: {
          schema_version: "1.0.0",
          provider: "openai",
          model: "gpt-5.4-mini",
          run_mode: "replay",
          api_key_present: false,
          live_mode_enabled: false,
          source_tools_available: true,
          tracing_enabled: false,
          configured_budget: {
            maximum_turns: 8,
            maximum_tool_calls: 12,
            timeout_seconds: 90,
            maximum_input_tokens: 12000,
            maximum_output_tokens: 3000,
            maximum_cost_usd: 0.5,
            retry_count: 0,
            input_cost_per_million_usd: 0.75,
            output_cost_per_million_usd: 4.5,
          },
        },
      },
    });
    renderApp("/admin/endpoints");
    expect(await screen.findByText("Replay mode")).toBeInTheDocument();
    expect(screen.getByText(/API key present: no/)).toBeInTheDocument();
    expect(screen.getByText(/Live runs are disabled/)).toBeInTheDocument();
  });

  it.each([
    ["live", "Live agent mode"],
    ["cached", "Cached mode"],
    ["replay", "Replay mode"],
  ])("renders the %s badge", async (mode, label) => {
    const modeRun = { ...run, run_mode: mode as AdminAgentRun["run_mode"] };
    installFetchMock({
      ...detailRoutes(() => build()),
      [`GET /api/admin/artifacts/${artifactId}/preview`]: {
        body: { artifact, content: { run_mode: mode, candidates } },
      },
      [`GET /api/admin/endpoint-builds/${buildId}/agent-runs`]: { body: [modeRun] },
      [`GET /api/admin/agent-runs/${runId}`]: { body: modeRun },
    });
    renderApp(`/admin/endpoints/${buildId}`);
    expect(await screen.findByText(label)).toBeInTheDocument();
  });

  it("presents real candidate evidence, source links, usage, and unresolved questions", async () => {
    const liveRun = {
      ...run,
      provider: "openai",
      model_identifier: "gpt-5.4-mini",
      run_mode: "live" as const,
      turns: 2,
      duration_ms: 1540,
      usage: { input_tokens: 3880, output_tokens: 500, cached_tokens: 120, cost_cents: 0.3 },
      tools: [{ id: "tool-live", tool_name: "search_geo_series", status: "completed", duration_ms: 40 }],
      tool_calls: [{
        tool_name: "search_geo_series",
        result: { output: {
          rendered_query: '"oxidative stress"[All Fields] AND "Homo sapiens"[Organism]',
          result_count: 2,
          cache_status: "live",
          strategy_reason: "Focused human oxidative-stress search.",
        } },
      }],
      trace: { events: [
        { event_type: "provider.turn.completed" },
        { event_type: "tool_call.completed" },
        { event_type: "provider.turn.completed" },
      ] },
    };
    const liveCandidates = [
      {
        candidate_id: "candidate-gse-12345",
        accession: "GSE12345",
        title: "Oxidative stress response in human cells",
        source: "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE12345",
        organism: ["Homo sapiens"],
        data_type: "RNA sequencing",
        sample_count: 12,
        biological_context: "HepG2 cells",
        treatment_control_evidence: "Vehicle and treatment groups were detected.",
        dose_time_evidence: "10 uM for 24 h",
        strengths: ["Official accession validated"],
        limitations: ["Human label review required"],
        recommendation_status: "recommended_for_human_review",
        accession_verified: true,
      },
      {
        candidate_id: "candidate-gse-12346",
        accession: "GSE12346",
        title: "Alternative series",
        source: "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE12346",
        organism: ["Homo sapiens"],
        data_type: "Microarray",
        sample_count: 8,
        biological_context: "Primary cells",
        treatment_control_evidence: "Control evidence is ambiguous.",
        dose_time_evidence: "Not reported",
        limitations: ["Control metadata is unclear"],
        recommendation_status: "alternative",
        accession_verified: true,
      },
    ];
    installFetchMock({
      ...detailRoutes(() => build()),
      [`GET /api/admin/artifacts/${artifactId}/preview`]: {
        body: {
          artifact,
          content: {
            run_mode: "live",
            live_discovery: true,
            candidates: liveCandidates,
            decision_summary: "GSE12345 has the stronger verifiable design.",
            unresolved_questions: ["Are the treatment labels scientifically acceptable?"],
          },
        },
      },
      [`GET /api/admin/endpoint-builds/${buildId}/agent-runs`]: { body: [liveRun] },
      [`GET /api/admin/agent-runs/${runId}`]: { body: liveRun },
    });
    renderApp(`/admin/endpoints/${buildId}`);
    expect(await screen.findByText("Live agent mode")).toBeInTheDocument();
    expect(screen.getAllByText("GSE12345").length).toBeGreaterThan(0);
    expect(screen.getAllByRole("link", { name: "NCBI GEO" })[0]).toHaveAttribute(
      "href",
      "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE12345",
    );
    expect(screen.getAllByText("Homo sapiens").length).toBeGreaterThan(0);
    expect(screen.getByText("GSE12345 has the stronger verifiable design.")).toBeInTheDocument();
    expect(screen.getByText("Are the treatment labels scientifically acceptable?")).toBeInTheDocument();
    expect(screen.getByText("gpt-5.4-mini")).toBeInTheDocument();
    expect(screen.getByText("$0.0030")).toBeInTheDocument();
    const activity = screen.getByRole("heading", { name: "Agent activity" }).closest("section")!;
    expect(within(activity).getByText("Model turns").parentElement).toHaveTextContent("2");
    expect(within(activity).getByText("Provider retries").parentElement).toHaveTextContent("0");
    expect(within(activity).getByText("Tool calls").parentElement).toHaveTextContent("1");
    expect(screen.getByText("Rendered GEO queries")).toBeInTheDocument();
    expect(screen.getByText(/oxidative stress.*Homo sapiens/)).toBeInTheDocument();
    expect(screen.getByText(/2 results/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh source metadata" })).toBeInTheDocument();
    expect(screen.queryByText(/chain of thought/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve dataset" })).toBeInTheDocument();
  });

  it("presents safe optional-filter normalization only as secondary trace detail", async () => {
    const originalArguments = {
      scientific_terms: ["oxidative stress"],
      organism_alternatives: ["Homo sapiens"],
      study_type_alternatives: ["expression profiling by array"],
      cell_tissue_terms: [""],
      treatment_terms: ["ROS"],
      maximum_results: 5,
      publication_date_start: null,
      publication_date_end: null,
      strategy_reason: "Focused oxidative-stress search.",
    };
    const normalizedRun: AdminAgentRun = {
      ...run,
      provider: "openai",
      model_identifier: "gpt-5.4-mini",
      run_mode: "live",
      turns: 2,
      tools: [{ id: "tool-normalized", tool_name: "search_geo_series", status: "completed", duration_ms: 20 }],
      tool_calls: [{
        tool_name: "search_geo_series",
        result: {
          output: {
            rendered_query: '"oxidative stress"[All Fields]',
            result_count: 1,
            cache_status: "cached",
            strategy_reason: "Focused oxidative-stress search.",
          },
          original_arguments: originalArguments,
          normalized_arguments: { ...originalArguments, cell_tissue_terms: [] },
          normalization_warnings: [{
            schema_version: "1.0.0",
            code: "empty_optional_search_term_removed",
            field: "cell_tissue_terms",
            original_index: 0,
          }],
        },
      }],
    };
    installFetchMock({
      ...detailRoutes(() => build()),
      [`GET /api/admin/endpoint-builds/${buildId}/agent-runs`]: { body: [normalizedRun] },
      [`GET /api/admin/agent-runs/${runId}`]: { body: normalizedRun },
    });
    const user = userEvent.setup();
    renderApp(`/admin/endpoints/${buildId}`);

    expect(await screen.findByText(/1 empty optional filter was removed before execution/i)).toBeInTheDocument();
    expect(screen.queryByText(/tool input invalid/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "View trace" }));
    expect(await screen.findAllByText(/empty_optional_search_term_removed/)).toHaveLength(2);
    expect(screen.getByText(/"cell_tissue_terms":\[""\]/)).toBeInTheDocument();
    expect(screen.getByText(/"cell_tissue_terms":\[\]/)).toBeInTheDocument();
  });

  it("presents controlled-vocabulary canonicalization as a compact note and detailed trace", async () => {
    const originalArguments = {
      scientific_terms: ["oxidative stress", "transcriptomic"],
      organism_alternatives: ["Homo sapiens", "Mus musculus"],
      study_type_alternatives: ["expression profiling by array", "high throughput sequencing"],
      cell_tissue_terms: [],
      treatment_terms: [],
      maximum_results: 5,
      publication_date_start: null,
      publication_date_end: null,
      strategy_reason: "Find bounded oxidative-stress transcriptomic GEO Series.",
    };
    const normalizedRun: AdminAgentRun = {
      ...run,
      provider: "openai",
      model_identifier: "gpt-5.4-mini",
      run_mode: "live",
      tools: [{ id: "tool-vocabulary", tool_name: "search_geo_series", status: "completed", duration_ms: 20 }],
      tool_calls: [{
        tool_name: "search_geo_series",
        result: {
          output: {
            rendered_query: '"Expression profiling by array" OR "Expression profiling by high throughput sequencing"',
            result_count: 0,
            cache_status: "cached",
            strategy_reason: "Bounded controlled-vocabulary search.",
          },
          original_arguments: originalArguments,
          normalized_arguments: {
            ...originalArguments,
            study_type_alternatives: [
              "Expression profiling by array",
              "Expression profiling by high throughput sequencing",
            ],
          },
          normalization_warnings: [
            {
              code: "controlled_vocabulary_alias_canonicalized",
              field: "study_type_alternatives",
              original_index: 0,
              original: "expression profiling by array",
              normalized: "Expression profiling by array",
              policy_version: "phase1-controlled-vocabulary-v1",
            },
            {
              code: "controlled_vocabulary_alias_canonicalized",
              field: "study_type_alternatives",
              original_index: 1,
              original: "high throughput sequencing",
              normalized: "Expression profiling by high throughput sequencing",
              policy_version: "phase1-controlled-vocabulary-v1",
            },
          ],
        },
      }],
    };
    installFetchMock({
      ...detailRoutes(() => build()),
      [`GET /api/admin/endpoint-builds/${buildId}/agent-runs`]: { body: [normalizedRun] },
      [`GET /api/admin/agent-runs/${runId}`]: { body: normalizedRun },
    });
    const user = userEvent.setup();
    renderApp(`/admin/endpoints/${buildId}`);

    expect(await screen.findByText("2 controlled-vocabulary values were normalized before execution.")).toBeInTheDocument();
    expect(screen.queryByText(/tool input invalid/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "View trace" }));
    expect(screen.getAllByText(/phase1-controlled-vocabulary-v1/)).toHaveLength(2);
    expect(screen.getByText(/high throughput sequencing → Expression profiling by high throughput sequencing/)).toBeInTheDocument();
  });

  it("shows a structured no-candidate outcome without a dataset approval action", async () => {
    const searchReview: AdminApproval = {
      ...approval,
      stage: "AWAITING_SEARCH_REVIEW",
      approval_type: "search_revision",
      request: {
        ...approval.request,
        evidence_summary: "Five public-valid candidates were inspected without a suitable result.",
        agent_recommendation: "Target direct oxidant perturbations with matched controls.",
        requested_action: "Request a revised search or cancel the workflow.",
      },
    };
    const noCandidateRun: AdminAgentRun = {
      ...run,
      provider: "openai",
      model_identifier: "gpt-5.4-mini",
      run_mode: "live",
      status: "completed",
      turns: 3,
      tools: [
        { id: "tool-search-1", tool_name: "search_geo_series", status: "completed", duration_ms: 20 },
        { id: "tool-validation", tool_name: "validate_geo_accessions", status: "completed", duration_ms: 20 },
        { id: "tool-inspection", tool_name: "inspect_geo_candidates", status: "completed", duration_ms: 20 },
      ],
      tool_calls: [{
        tool_name: "inspect_geo_candidates",
        result: { output: { inspected_count: 5, failed_count: 0 } },
      }],
      trace: { events: [
        { event_type: "provider.turn.started", detail: { turn: 1, discovery_substage: "search_planning", tools_exposed: ["search_geo_series"] } },
        { event_type: "provider.turn.started", detail: { turn: 2, discovery_substage: "candidate_validation", tools_exposed: ["validate_geo_accessions"] } },
        { event_type: "provider.turn.started", detail: { turn: 3, discovery_substage: "candidate_inspection", tools_exposed: ["inspect_geo_candidates"] } },
        { event_type: "provider.turn.started", detail: { turn: 4, discovery_substage: "final_output", tools_exposed: [] } },
      ] },
    };
    installFetchMock({
      ...detailRoutes(() => build("AWAITING_SEARCH_REVIEW", 4, { pending_approval_id: approvalId })),
      [`GET /api/admin/artifacts/${artifactId}/preview`]: {
        body: {
          artifact,
          content: {
            run_mode: "live",
            live_discovery: true,
            candidates: [],
            recommended_candidate_id: null,
          },
        },
      },
      [`GET /api/admin/endpoint-builds/${buildId}/approvals`]: { body: [searchReview] },
      [`GET /api/admin/endpoint-builds/${buildId}/agent-runs`]: { body: [noCandidateRun] },
      [`GET /api/admin/agent-runs/${runId}`]: { body: noCandidateRun },
    });
    renderApp(`/admin/endpoints/${buildId}`);
    expect(await screen.findByText("Search review required")).toBeInTheDocument();
    expect(screen.getByText("Bounded GEO searches found no suitable candidate")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Request revised search" })).toBeInTheDocument();
    expect(screen.getByText("Target direct oxidant perturbations with matched controls.")).toBeInTheDocument();
    expect(screen.getByText("Tools exposed per turn")).toBeInTheDocument();
    expect(screen.getByText("5 inspected / 0 unresolved")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve dataset" })).not.toBeInTheDocument();
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
    expect(screen.queryByText("Completed for review")).not.toBeInTheDocument();
  });

  it("shows a failed live run from persisted run truth without replay or review claims", async () => {
    const failedRun: AdminAgentRun = {
      ...run,
      provider: "openai",
      model_identifier: "gpt-5.4-mini",
      run_mode: "live",
      status: "failed",
      turns: 1,
      duration_ms: 16,
      usage: { input_tokens: 0, output_tokens: 0, cached_tokens: 0, cost_cents: 0 },
      tools: [],
      trace: {
        events: [
          {
            event_type: "provider.turn.failed",
            detail: {
              retryable: false,
              exception_class: "UserError",
              developer_message: "additionalProperties should not be set for object types.",
            },
          },
        ],
      },
    };
    const terminalError: AdminWorkflowError = {
      ...workflowErrors[0],
      code: "provider_failure",
      retryable: false,
      safe_message: "Provider failed after bounded retries.",
    };
    installFetchMock({
      ...detailRoutes(() => build("FAILED", 4), [terminalError]),
      [`GET /api/admin/endpoint-builds/${buildId}/artifacts`]: { body: [] },
      [`GET /api/admin/endpoint-builds/${buildId}/approvals`]: { body: [] },
      [`GET /api/admin/endpoint-builds/${buildId}/agent-runs`]: { body: [failedRun] },
      [`GET /api/admin/agent-runs/${runId}`]: { body: failedRun },
    });
    renderApp(`/admin/endpoints/${buildId}`);
    expect(await screen.findByText("Live agent run failed")).toBeInTheDocument();
    expect(screen.getByText("Live agent mode")).toBeInTheDocument();
    expect(screen.getByText("No recommendation available")).toBeInTheDocument();
    expect(screen.getByText("additionalProperties should not be set for object types.")).toBeInTheDocument();
    expect(screen.getByText("No review required")).toBeInTheDocument();
    expect(screen.queryByText("Replay mode")).not.toBeInTheDocument();
    expect(screen.queryByText("Completed for review")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry failed step" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve dataset" })).not.toBeInTheDocument();
    const activity = screen.getByRole("heading", { name: "Agent activity" }).closest("section")!;
    expect(within(activity).getByText("Provider retries").parentElement).toHaveTextContent("0");
    expect(within(activity).getByText("Tool calls").parentElement).toHaveTextContent("0");
    expect(within(activity).getByText("Model turns").parentElement).toHaveTextContent("1");
  });

  it("shows the specification revision gate without fake usage or source claims", async () => {
    const revisionRun: AdminAgentRun = {
      ...run,
      agent_name: "Dataset Specification Agent",
      provider: "openai",
      model_identifier: "gpt-5.4-mini",
      run_mode: "live",
      status: "completed",
      turns: 1,
      tools: [],
      usage: {
        usage_status: "usage_unavailable",
        input_tokens: 0,
        output_tokens: 0,
        cached_tokens: 0,
        cost_cents: 0,
        provider_invocations: 1,
      },
      trace: {
        events: [{
          event_type: "provider.turn.completed",
          detail: {
            structured_output_diagnostic: {
              developer_message: "The model response did not match the required structured schema.",
              failure_classification: "schema_validation_failed",
              output_schema_name: "DatasetSpecificationAgentOutcome",
              output_schema_version: "1.0.0",
              provider_request_ids: ["req_safe_123"],
              provider_response_ids: ["resp_safe_456"],
              usage: { usage_status: "usage_unavailable" },
              error_handler: "invalid_final_output",
            },
          },
        }],
      },
    };
    installFetchMock({
      ...detailRoutes(() => build("AWAITING_DATASET_SPECIFICATION_REVISION", 2, {
        workflow_kind: "training_dataset_discovery",
        pending_approval_id: null,
      })),
      [`GET /api/admin/endpoint-builds/${buildId}/artifacts`]: { body: [] },
      [`GET /api/admin/endpoint-builds/${buildId}/approvals`]: { body: [] },
      [`GET /api/admin/endpoint-builds/${buildId}/agent-runs`]: { body: [revisionRun] },
      [`GET /api/admin/agent-runs/${runId}`]: { body: revisionRun },
      [`GET /api/admin/endpoint-builds/${buildId}/training-dataset-workflow`]: {
        body: {
          schema_version: "1.0.0",
          workflow_id: buildId,
          workflow_kind: "training_dataset_discovery",
          benchmark_mode: "blind_training_dataset_discovery",
          legacy: false,
          specification_draft: null,
          target_specification: null,
          verified_source_inventory: null,
          assembly_strategies: null,
          specification_agent_outcome: {
            status: "invalid_model_output",
            failure_category: "schema_validation_failed",
          },
        } satisfies AdminTrainingDatasetWorkflow,
      },
    });
    renderApp(`/admin/endpoints/${buildId}`);
    expect(await screen.findByRole("heading", { name: "Dataset specification needs revision" })).toBeInTheDocument();
    expect(screen.getByText("The agent response did not match the required structured contract. No source discovery was started.")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Revise endpoint request" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: "Retry specification" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: "Use another planner model" }).length).toBeGreaterThan(0);
    expect(screen.getByText("Usage unavailable after structured-output failure")).toBeInTheDocument();
    expect(screen.getByText("req_safe_123")).toBeInTheDocument();
    expect(screen.getByText("resp_safe_456")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Verified source inventory" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve dataset" })).not.toBeInTheDocument();
    expect(screen.queryByText("Bounded GEO searches found no suitable candidate")).not.toBeInTheDocument();
  });

  it("renders a valid historical insufficient outcome without a schema-failure claim", async () => {
    const historicalRun: AdminAgentRun = {
      ...run,
      agent_name: "Dataset Specification Agent",
      provider: "openai",
      model_identifier: "gpt-5.4-mini",
      run_mode: "live",
      status: "completed",
      turns: 1,
      duration_ms: 1234,
      tools: [],
      usage: {
        usage_status: "usage_recorded",
        input_tokens: 321,
        output_tokens: 123,
        cached_tokens: 45,
        cost_cents: 0.42,
        provider_invocations: 1,
        provider_request_ids: ["req_historical_safe"],
        provider_response_ids: ["resp_historical_safe"],
      },
      trace: { events: [] },
    };
    installFetchMock({
      ...detailRoutes(() => build("AWAITING_DATASET_SPECIFICATION_REVISION", 2, {
        workflow_kind: "training_dataset_discovery",
        pending_approval_id: null,
      })),
      [`GET /api/admin/endpoint-builds/${buildId}/artifacts`]: { body: [] },
      [`GET /api/admin/endpoint-builds/${buildId}/approvals`]: { body: [] },
      [`GET /api/admin/endpoint-builds/${buildId}/agent-runs`]: { body: [historicalRun] },
      [`GET /api/admin/agent-runs/${runId}`]: { body: historicalRun },
      [`GET /api/admin/endpoint-builds/${buildId}/training-dataset-workflow`]: {
        body: {
          schema_version: "1.0.0",
          workflow_id: buildId,
          workflow_kind: "training_dataset_discovery",
          benchmark_mode: "blind_training_dataset_discovery",
          legacy: false,
          specification_draft: null,
          specification_agent_outcome: {
            status: "insufficient_endpoint_definition",
            specification: null,
            decision_summary: "A construction-policy choice was treated as blocking.",
            unresolved_questions: ["Should activity be binary or continuous?"],
            limitations: ["No source discovery was started."],
            failure_category: null,
          },
        } satisfies AdminTrainingDatasetWorkflow,
      },
    });
    renderApp(`/admin/endpoints/${buildId}`);
    expect(await screen.findByRole("heading", { name: "Endpoint definition needs clarification" })).toBeInTheDocument();
    expect(screen.getByText("The agent produced a valid structured response but could not create a dataset-specification draft from the endpoint definition.")).toBeInTheDocument();
    expect(screen.getByText("A construction-policy choice was treated as blocking.")).toBeInTheDocument();
    expect(screen.getByText("Should activity be binary or continuous?")).toBeInTheDocument();
    expect(screen.queryByText(/schema mismatch/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Unknown model behavior/i)).not.toBeInTheDocument();
    await userEvent.click(screen.getByText("Technical audit"));
    expect(screen.getByText("req_historical_safe")).toBeInTheDocument();
    expect(screen.getByText("resp_historical_safe")).toBeInTheDocument();
  });

  it("presents semantic-policy mismatch separately from malformed output", async () => {
    const semanticRun: AdminAgentRun = {
      ...run,
      agent_name: "Dataset Specification Agent",
      status: "completed",
      tools: [],
      trace: { events: [] },
    };
    installFetchMock({
      ...detailRoutes(() => build("AWAITING_DATASET_SPECIFICATION_REVISION", 2, {
        workflow_kind: "training_dataset_discovery",
        pending_approval_id: null,
      })),
      [`GET /api/admin/endpoint-builds/${buildId}/artifacts`]: { body: [] },
      [`GET /api/admin/endpoint-builds/${buildId}/approvals`]: { body: [] },
      [`GET /api/admin/endpoint-builds/${buildId}/agent-runs`]: { body: [semanticRun] },
      [`GET /api/admin/agent-runs/${runId}`]: { body: semanticRun },
      [`GET /api/admin/endpoint-builds/${buildId}/training-dataset-workflow`]: {
        body: {
          schema_version: "1.0.0",
          workflow_id: buildId,
          workflow_kind: "training_dataset_discovery",
          legacy: false,
          specification_agent_outcome: {
            status: "insufficient_endpoint_definition",
            decision_summary: "Policy choices prevented a draft.",
          },
          specification_semantic_validation: {
            status: "semantic_contract_violation",
            violation_code: "non_blocking_policy_treated_as_core_missing",
          },
        } satisfies AdminTrainingDatasetWorkflow,
      },
    });
    renderApp(`/admin/endpoints/${buildId}`);
    expect(await screen.findByRole("heading", { name: "Dataset specification policy needs revision" })).toBeInTheDocument();
    expect(screen.getByText("The endpoint already contains an explicit target and modality, but the agent treated non-blocking dataset-policy choices as blocking.")).toBeInTheDocument();
    expect(screen.queryByText(/schema mismatch/i)).not.toBeInTheDocument();
  });

  it("renders only allowlisted scientific-source diagnostics", async () => {
    const sourceDiagnostic = {
      tool_name: "validate_geo_accessions",
      source_host: "www.ncbi.nlm.nih.gov",
      safe_url_path: "/geo/query/acc.cgi",
      http_method: "GET" as const,
      http_status: 200,
      final_approved_host: "www.ncbi.nlm.nih.gov",
      content_type: "geo/text",
      artifact_content_type: "text/plain",
      response_byte_count: 512,
      parser_outcome: "public_valid",
      source_artifact_id: "art-geo-source",
      cache_status: "live" as const,
      exception_class: "SourceFormatError",
      source_error_category: "unexpected_content_type",
      retryable: false,
      attempt_number: 1,
      request_duration_ms: 562,
      developer_message: "Expected a machine-readable GEO response.",
    };
    const failedRun: AdminAgentRun = {
      ...run,
      provider: "openai",
      model_identifier: "gpt-5.4-mini",
      run_mode: "live",
      status: "failed",
      turns: 1,
      tools: [{
        id: "tool-source-failure",
        tool_name: "validate_geo_accessions",
        status: "failed",
        duration_ms: 562,
      }],
      tool_calls: [{
        tool_name: "validate_geo_accessions",
        result: { source_diagnostic: sourceDiagnostic },
      }],
    };
    const sourceError: AdminWorkflowError = {
      ...workflowErrors[0],
      code: "source_unexpected_content_type",
      category: "source_tool",
      retryable: false,
      safe_message: "Scientific source returned an unexpected content type.",
      detail: { source_diagnostic: sourceDiagnostic },
    };
    installFetchMock({
      ...detailRoutes(() => build("FAILED", 4), [sourceError]),
      [`GET /api/admin/endpoint-builds/${buildId}/artifacts`]: { body: [] },
      [`GET /api/admin/endpoint-builds/${buildId}/approvals`]: { body: [] },
      [`GET /api/admin/endpoint-builds/${buildId}/agent-runs`]: { body: [failedRun] },
      [`GET /api/admin/agent-runs/${runId}`]: { body: failedRun },
    });
    renderApp(`/admin/endpoints/${buildId}`);
    expect(await screen.findByText("Scientific-source diagnostics")).toBeInTheDocument();
    expect(screen.getAllByText("www.ncbi.nlm.nih.gov/geo/query/acc.cgi")).toHaveLength(2);
    expect(screen.getAllByText(/GET.*200/)).toHaveLength(2);
    expect(screen.getAllByText(/geo\/text.*512 bytes/)).toHaveLength(2);
    expect(screen.getAllByText("text/plain")).toHaveLength(2);
    expect(screen.getAllByText("Public valid")).toHaveLength(2);
    expect(screen.getAllByText("art-geo-source")).toHaveLength(2);
    expect(screen.getAllByText("SourceFormatError")).toHaveLength(2);
    expect(screen.getAllByText("Expected a machine-readable GEO response.")).toHaveLength(2);
    expect(document.body).not.toHaveTextContent("do-not-store");
    expect(document.body).not.toHaveTextContent("Authorization");
  });
});
