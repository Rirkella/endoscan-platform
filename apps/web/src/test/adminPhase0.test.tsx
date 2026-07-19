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

function detailRoutes(
  current: () => AdminBuild,
  errors: AdminWorkflowError[] = [],
) {
  return {
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
    expect(screen.getByText("source â†’ rows")).toBeInTheDocument();
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

  it("uses the latest persisted run identifier when multiple attempts exist", async () => {
    const olderRun = { ...run, id: "run-11111111-1111-4111-8111-111111111111" };
    insëN¹¶‰ËkºwµçP¤¹Ñ½!…Ù•Q•áÑ½¹Ñ•¹Ğ ˆÈˆ¤ì(€€€•áÁ•Ğ¡İ¥Ñ¡¥¸¡…Ñ¥Ù¥Ñä¤¹•Ñ	åQ•áĞ ‰AÉ½Ù¥‘•ÈÉ•ÑÉ¥•Ìˆ¤¹Á…É•¹Ñ±•µ•¹Ğ¤¹Ñ½!…Ù•Q•áÑ½¹Ñ•¹Ğ ˆÀˆ¤ì(€€€•áÁ•Ğ¡İ¥Ñ¡¥¸¡…Ñ¥Ù¥Ñä¤¹•Ñ	åQ•áĞ ‰Q½½°…±±Ìˆ¤¹Á…É•¹Ñ±•µ•¹Ğ¤¹Ñ½!…Ù•Q•áÑ½¹Ñ•¹Ğ ˆÄˆ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ	åQ•áĞ ‰I•¹‘•É•<ÅÕ•É¥•Ìˆ¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ	åQ•áĞ ½½á¥‘…Ñ¥Ù”ÍÑÉ•ÍÌ¸©!½µ¼Í…Á¥•¹Ì¼¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ	åQ•áĞ ¼ÈÉ•ÍÕ±ÑÌ¼¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ	åI½±” ‰‰ÕÑÑ½¸ˆ°ì¹…µ”è€‰I•™É•Í Í½ÕÉ”µ•Ñ…‘…Ñ„ˆô¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹ÅÕ•Éå	åQ•áĞ ½¡…¥¸½˜Ñ¡½Õ¡Ğ½¤¤¤¹¹½Ğ¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ	åI½±” ‰‰ÕÑÑ½¸ˆ°ì¹…µ”è€‰ÁÁÉ½Ù”‘…Ñ…Í•Ğˆô¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€ô¤ì((€¥Ğ ‰ÁÉ•Í•¹ÑÌÍ…™”½ÁÑ¥½¹…°µ™¥±Ñ•È¹½Éµ…±¥é…Ñ¥½¸½¹±ä…ÌÍ•½¹‘…ÉäÑÉ…”‘•Ñ…¥°ˆ°…Íå¹Œ€ ¤€ôøì(€€€½¹ÍĞ½É¥¥¹…±ÉÕµ•¹ÑÌ€ôì(€€€€€Í¥•¹Ñ¥™¥}Ñ•ÉµÌèl‰½á¥‘…Ñ¥Ù”ÍÑÉ•ÍÌ‰t°(€€€€€½É…¹¥Íµ}…±Ñ•É¹…Ñ¥Ù•Ìèl‰!½µ¼Í…Á¥•¹Ì‰t°(€€€€€ÍÑÕ‘å}ÑåÁ•}…±Ñ•É¹…Ñ¥Ù•Ìèl‰•áÁÉ•ÍÍ¥½¸ÁÉ½™¥±¥¹œ‰ä…ÉÉ…ä‰t°(€€€€€•±±}Ñ¥ÍÍÕ•}Ñ•ÉµÌèlˆ‰t°(€€€€€ÑÉ•…Ñµ•¹Ñ}Ñ•ÉµÌèl‰I=L‰t°(€€€€€µ…á¥µÕµ}É•ÍÕ±ÑÌè€Ô°(€€€€€ÁÕ‰±¥…Ñ¥½¹}‘…Ñ•}ÍÑ…ÉĞè¹Õ±°°(€€€€€ÁÕ‰±¥…Ñ¥½¹}‘…Ñ•}•¹è¹Õ±°°(€€€€€ÍÑÉ…Ñ•å}É•…Í½¸è€‰½ÕÍ•½á¥‘…Ñ¥Ù”µÍÑÉ•ÍÌÍ•…É ¸ˆ°(€€€ôì(€€€½¹ÍĞ¹½Éµ…±¥é•‘IÕ¸è‘µ¥¹•¹ÑIÕ¸€ôì(€€€€€€¸¸¹ÉÕ¸°(€€€€€ÁÉ½Ù¥‘•Èè€‰½Á•¹…¤ˆ°(€€€€€µ½‘•±}¥‘•¹Ñ¥™¥•Èè€‰ÁĞ´Ô¸Ğµµ¥¹¤ˆ°(€€€€€ÉÕ¹}µ½‘”è€‰±¥Ù”ˆ°(€€€€€ÑÕÉ¹Ìè€È°(€€€€€Ñ½½±Ìèmì¥è€‰Ñ½½°µ¹½Éµ…±¥é•ˆ°Ñ½½±}¹…µ”è€‰Í•…É¡}•½}Í•É¥•Ìˆ°ÍÑ…ÑÕÌè€‰½µÁ±•Ñ•ˆ°‘ÕÉ…Ñ¥½¹}µÌè€ÈÀõt°(€€€€€Ñ½½±}…±±Ìèmì(€€€€€€€Ñ½½±}¹…µ”è€‰Í•…É¡}•½}Í•É¥•Ìˆ°(€€€€€€€É•ÍÕ±Ğèì(€€€€€€€€€½ÕÑÁÕĞèì(€€€€€€€€€€€É•¹‘•É•‘}ÅÕ•Éäè€œ‰½á¥‘…Ñ¥Ù”ÍÑÉ•ÍÌ‰m±°¥•±‘Ítœ°(€€€€€€€€€€€É•ÍÕ±Ñ}½Õ¹Ğè€Ä°(€€€€€€€€€€€…¡•}ÍÑ…ÑÕÌè€‰…¡•ˆ°(€€€€€€€€€€€ÍÑÉ…Ñ•å}É•…Í½¸è€‰½ÕÍ•½á¥‘…Ñ¥Ù”µÍÑÉ•ÍÌÍ•…É ¸ˆ°(€€€€€€€€€ô°(€€€€€€€€€½É¥¥¹…±}…ÉÕµ•¹ÑÌè½É¥¥¹…±ÉÕµ•¹ÑÌ°(€€€€€€€€€¹½Éµ…±¥é•‘}…ÉÕµ•¹ÑÌèì€¸¸¹½É¥¥¹…±ÉÕµ•¹ÑÌ°•±±}Ñ¥ÍÍÕ•}Ñ•ÉµÌèmtô°(€€€€€€€€€¹½Éµ…±¥é…Ñ¥½¹}İ…É¹¥¹Ìèmì(€€€€€€€€€€€Í¡•µ…}Ù•ÉÍ¥½¸è€ˆÄ¸À¸Àˆ°(€€€€€€€€€€€½‘”è€‰•µÁÑå}½ÁÑ¥½¹…±}Í•…É¡}Ñ•Éµ}É•µ½Ù•ˆ°(€€€€€€€€€€€™¥•±è€‰•±±}Ñ¥ÍÍÕ•}Ñ•ÉµÌˆ°(€€€€€€€€€€€½É¥¥¹…±}¥¹‘•àè€À°(€€€€€€€€€õt°(€€€€€€€ô°(€€€€€õt°(€€€ôì(€€€¥¹ÍÑ…±±•Ñ¡5½¬¡ì(€€€€€€¸¸¹‘•Ñ…¥±I½ÕÑ•Ì  ¤€ôø‰Õ¥± ¤¤°(€€€€€mP€½…Á¤½…‘µ¥¸½•¹‘Á½¥¹Ğµ‰Õ¥±‘Ì¼‘í‰Õ¥±‘%‘ô½…•¹ĞµÉÕ¹Ítèì‰½‘äèm¹½Éµ…±¥é•‘IÕ¹tô°(€€€€€mP€½…Á¤½…‘µ¥¸½…•¹ĞµÉÕ¹Ì¼‘íÉÕ¹%‘õtèì‰½‘äè¹½Éµ…±¥é•‘IÕ¸ô°(€€€ô¤ì(€€€½¹ÍĞÕÍ•È€ôÕÍ•ÉÙ•¹Ğ¹Í•ÑÕÀ ¤ì(€€€É•¹‘•ÉÁÀ¡€½…‘µ¥¸½•¹‘Á½¥¹ÑÌ¼‘í‰Õ¥±‘%‘õ€¤ì((€€€•áÁ•Ğ¡…İ…¥ĞÍÉ••¸¹™¥¹‘	åQ•áĞ ¼Ä•µÁÑä½ÁÑ¥½¹…°™¥±Ñ•Èİ…ÌÉ•µ½Ù•‰•™½É”•á•ÕÑ¥½¸½¤¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹ÅÕ•Éå	åQ•áĞ ½Ñ½½°¥¹ÁÕĞ¥¹Ù…±¥½¤¤¤¹¹½Ğ¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€…İ…¥ĞÕÍ•È¹±¥¬¡ÍÉ••¸¹•Ñ	åI½±” ‰‰ÕÑÑ½¸ˆ°ì¹…µ”è€‰Y¥•ÜÑÉ…”ˆô¤¤ì(€€€•áÁ•Ğ¡…İ…¥ĞÍÉ••¸¹™¥¹‘±±	åQ•áĞ ½•µÁÑå}½ÁÑ¥½¹…±}Í•…É¡}Ñ•Éµ}É•µ½Ù•¼¤¤¹Ñ½!…Ù•1•¹Ñ  È¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ	åQ•áĞ ¼‰•±±}Ñ¥ÍÍÕ•}Ñ•ÉµÌˆéqlˆ‰qt¼¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ	åQ•áĞ ¼‰•±±}Ñ¥ÍÍÕ•}Ñ•ÉµÌˆéqmqt¼¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€ô¤ì((€¥Ğ ‰ÁÉ•Í•¹ÑÌ½¹ÑÉ½±±•µÙ½…‰Õ±…Éä…¹½¹¥…±¥é…Ñ¥½¸…Ì„½µÁ…Ğ¹½Ñ”…¹‘•Ñ…¥±•ÑÉ…”ˆ°…Íå¹Œ€ ¤€ôøì(€€€½¹ÍĞ½É¥¥¹…±ÉÕµ•¹ÑÌ€ôì(€€€€€Í¥•¹Ñ¥™¥}Ñ•ÉµÌèl‰½á¥‘…Ñ¥Ù”ÍÑÉ•ÍÌˆ°€‰ÑÉ…¹ÍÉ¥ÁÑ½µ¥Œ‰t°(€€€€€½É…¹¥Íµ}…±Ñ•É¹…Ñ¥Ù•Ìèl‰!½µ¼Í…Á¥•¹Ìˆ°€‰5ÕÌµÕÍÕ±ÕÌ‰t°(€€€€€ÍÑÕ‘å}ÑåÁ•}…±Ñ•É¹…Ñ¥Ù•Ìèl‰•áÁÉ•ÍÍ¥½¸ÁÉ½™¥±¥¹œ‰ä…ÉÉ…äˆ°€‰¡¥ Ñ¡É½Õ¡ÁÕĞÍ•ÅÕ•¹¥¹œ‰t°(€€€€€•±±}Ñ¥ÍÍÕ•}Ñ•ÉµÌèmt°(€€€€€ÑÉ•…Ñµ•¹Ñ}Ñ•ÉµÌèmt°(€€€€€µ…á¥µÕµ}É•ÍÕ±ÑÌè€Ô°(€€€€€ÁÕ‰±¥…Ñ¥½¹}‘…Ñ•}ÍÑ…ÉĞè¹Õ±°°(€€€€€ÁÕ‰±¥…Ñ¥½¹}‘…Ñ•}•¹è¹Õ±°°(€€€€€ÍÑÉ…Ñ•å}É•…Í½¸è€‰¥¹‰½Õ¹‘•½á¥‘…Ñ¥Ù”µÍÑÉ•ÍÌÑÉ…¹ÍÉ¥ÁÑ½µ¥Œ<M•É¥•Ì¸ˆ°(€€€ôì(€€€½¹ÍĞ¹½Éµ…±¥é•‘IÕ¸è‘µ¥¹•¹ÑIÕ¸€ôì(€€€€€€¸¸¹ÉÕ¸°(€€€€€ÁÉ½Ù¥‘•Èè€‰½Á•¹…¤ˆ°(€€€€€µ½‘•±}¥‘•¹Ñ¥™¥•Èè€‰ÁĞ´Ô¸Ğµµ¥¹¤ˆ°(€€€€€ÉÕ¹}µ½‘”è€‰±¥Ù”ˆ°(€€€€€Ñ½½±Ìèmì¥è€‰Ñ½½°µÙ½…‰Õ±…Éäˆ°Ñ½½±}¹…µ”è€‰Í•…É¡}•½}Í•É¥•Ìˆ°ÍÑ…ÑÕÌè€‰½µÁ±•Ñ•ˆ°‘ÕÉ…Ñ¥½¹}µÌè€ÈÀõt°(€€€€€Ñ½½±}…±±Ìèmì(€€€€€€€Ñ½½±}¹…µ”è€‰Í•…É¡}•½}Í•É¥•Ìˆ°(€€€€€€€É•ÍÕ±Ğèì(€€€€€€€€€½ÕÑÁÕĞèì(€€€€€€€€€€€É•¹‘•É•‘}ÅÕ•Éäè€œ‰áÁÉ•ÍÍ¥½¸ÁÉ½™¥±¥¹œ‰ä…ÉÉ…äˆ=H€‰áÁÉ•ÍÍ¥½¸ÁÉ½™¥±¥¹œ‰ä¡¥ Ñ¡É½Õ¡ÁÕĞÍ•ÅÕ•¹¥¹œˆœ°(€€€€€€€€€€€É•ÍÕ±Ñ}½Õ¹Ğè€À°(€€€€€€€€€€€…¡•}ÍÑ…ÑÕÌè€‰…¡•ˆ°(€€€€€€€€€€€ÍÑÉ…Ñ•å}É•…Í½¸è€‰	½Õ¹‘•½¹ÑÉ½±±•µÙ½…‰Õ±…ÉäÍ•…É ¸ˆ°(€€€€€€€€€ô°(€€€€€€€€€½É¥¥¹…±}…ÉÕµ•¹ÑÌè½É¥¥¹…±ÉÕµ•¹ÑÌ°(€€€€€€€€€¹½Éµ…±¥é•‘}…ÉÕµ•¹ÑÌèì(€€€€€€€€€€€€¸¸¹½É¥¥¹…±ÉÕµ•¹ÑÌ°(€€€€€€€€€€€ÍÑÕ‘å}ÑåÁ•}…±Ñ•É¹…Ñ¥Ù•Ìèl(€€€€€€€€€€€€€€‰áÁÉ•ÍÍ¥½¸ÁÉ½™¥±¥¹œ‰ä…ÉÉ…äˆ°(€€€€€€€€€€€€€€‰áÁÉ•ÍÍ¥½¸ÁÉ½™¥±¥¹œ‰ä¡¥ Ñ¡É½Õ¡ÁÕĞÍ•ÅÕ•¹¥¹œˆ°(€€€€€€€€€€€t°(€€€€€€€€€ô°(€€€€€€€€€¹½Éµ…±¥é…Ñ¥½¹}İ…É¹¥¹Ìèl(€€€€€€€€€€€ì(€€€€€€€€€€€€€½‘”è€‰½¹ÑÉ½±±•‘}Ù½…‰Õ±…Éå}…±¥…Í}…¹½¹¥…±¥é•ˆ°(€€€€€€€€€€€€€™¥•±è€‰ÍÑÕ‘å}ÑåÁ•}…±Ñ•É¹…Ñ¥Ù•Ìˆ°(€€€€€€€€€€€€€½É¥¥¹…±}¥¹‘•àè€À°(€€€€€€€€€€€€€½É¥¥¹…°è€‰•áÁÉ•ÍÍ¥½¸ÁÉ½™¥±¥¹œ‰ä…ÉÉ…äˆ°(€€€€€€€€€€€€€¹½Éµ…±¥é•è€‰áÁÉ•ÍÍ¥½¸ÁÉ½™¥±¥¹œ‰ä…ÉÉ…äˆ°(€€€€€€€€€€€€€Á½±¥å}Ù•ÉÍ¥½¸è€‰Á¡…Í”Äµ½¹ÑÉ½±±•µÙ½…‰Õ±…ÉäµØÄˆ°(€€€€€€€€€€€ô°(€€€€€€€€€€€ì(€€€€€€€€€€€€€½‘”è€‰½¹ÑÉ½±±•‘}Ù½…‰Õ±…Éå}…±¥…Í}…¹½¹¥…±¥é•ˆ°(€€€€€€€€€€€€€™¥•±è€‰ÍÑÕ‘å}ÑåÁ•}…±Ñ•É¹…Ñ¥Ù•Ìˆ°(€€€€€€€€€€€€€½É¥¥¹…±}¥¹‘•àè€Ä°(€€€€€€€€€€€€€½É¥¥¹…°è€‰¡¥ Ñ¡É½Õ¡ÁÕĞÍ•ÅÕ•¹¥¹œˆ°(€€€€€€€€€€€€€¹½Éµ…±¥é•è€‰áÁÉ•ÍÍ¥½¸ÁÉ½™¥±¥¹œ‰ä¡¥ Ñ¡É½Õ¡ÁÕĞÍ•ÅÕ•¹¥¹œˆ°(€€€€€€€€€€€€€Á½±¥å}Ù•ÉÍ¥½¸è€‰Á¡…Í”Äµ½¹ÑÉ½±±•µÙ½…‰Õ±…ÉäµØÄˆ°(€€€€€€€€€€€ô°(€€€€€€€€€t°(€€€€€€€ô°(€€€€€õt°(€€€ôì(€€€¥¹ÍÑ…±±•Ñ¡5½¬¡ì(€€€€€€¸¸¹‘•Ñ…¥±I½ÕÑ•Ì  ¤€ôø‰Õ¥± ¤¤°(€€€€€mP€½…Á¤½…‘µ¥¸½•¹‘Á½¥¹Ğµ‰Õ¥±‘Ì¼‘í‰Õ¥±‘%‘ô½…•¹ĞµÉÕ¹Ítèì‰½‘äèm¹½Éµ…±¥é•‘IÕ¹tô°(€€€€€mP€½…Á¤½…‘µ¥¸½…•¹ĞµÉÕ¹Ì¼‘íÉÕ¹%‘õtèì‰½‘äè¹½Éµ…±¥é•‘IÕ¸ô°(€€€ô¤ì(€€€½¹ÍĞÕÍ•È€ôÕÍ•ÉÙ•¹Ğ¹Í•ÑÕÀ ¤ì(€€€É•¹‘•ÉÁÀ¡€½…‘µ¥¸½•¹‘Á½¥¹ÑÌ¼‘í‰Õ¥±‘%‘õ€¤ì((€€€•áÁ•Ğ¡…İ…¥ĞÍÉ••¸¹™¥¹‘	åQ•áĞ ˆÈ½¹ÑÉ½±±•µÙ½…‰Õ±…ÉäÙ…±Õ•Ìİ•É”¹½Éµ…±¥é•‰•™½É”•á•ÕÑ¥½¸¸ˆ¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹ÅÕ•Éå	åQ•áĞ ½Ñ½½°¥¹ÁÕĞ¥¹Ù…±¥½¤¤¤¹¹½Ğ¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€…İ…¥ĞÕÍ•È¹±¥¬¡ÍÉ••¸¹•Ñ	åI½±” ‰‰ÕÑÑ½¸ˆ°ì¹…µ”è€‰Y¥•ÜÑÉ…”ˆô¤¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ±±	åQ•áĞ ½Á¡…Í”Äµ½¹ÑÉ½±±•µÙ½…‰Õ±…ÉäµØÄ¼¤¤¹Ñ½!…Ù•1•¹Ñ  È¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ	åQ•áĞ ½¡¥ Ñ¡É½Õ¡ÁÕĞÍ•ÅÕ•¹¥¹œƒŠHáÁÉ•ÍÍ¥½¸ÁÉ½™¥±¥¹œ‰ä¡¥ Ñ¡É½Õ¡ÁÕĞÍ•ÅÕ•¹¥¹œ¼¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€ô¤ì((€¥Ğ ‰Í¡½İÌ„ÍÑÉÕÑÕÉ•¹¼µ…¹‘¥‘…Ñ”½ÕÑ½µ”İ¥Ñ¡½ÕĞ„‘…Ñ…Í•Ğ…ÁÁÉ½Ù…°…Ñ¥½¸ˆ°…Íå¹Œ€ ¤€ôøì(€€€½¹ÍĞÍ•…É¡I•Ù¥•Üè‘µ¥¹ÁÁÉ½Ù…°€ôì(€€€€€€¸¸¹…ÁÁÉ½Ù…°°(€€€€€ÍÑ…”è€‰]%Q%9}MI!}IY%\ˆ°(€€€€€…ÁÁÉ½Ù…±}ÑåÁ”è€‰Í•…É¡}É•Ù¥Í¥½¸ˆ°(€€€€€É•ÅÕ•ÍĞèì(€€€€€€€€¸¸¹…ÁÁÉ½Ù…°¹É•ÅÕ•ÍĞ°(€€€€€€€•Ù¥‘•¹•}ÍÕµµ…Éäè€‰¥Ù”ÁÕ‰±¥ŒµÙ…±¥…¹‘¥‘…Ñ•Ìİ•É”¥¹ÍÁ•Ñ•İ¥Ñ¡½ÕĞ„ÍÕ¥Ñ…‰±”É•ÍÕ±Ğ¸ˆ°(€€€€€€€…•¹Ñ}É•½µµ•¹‘…Ñ¥½¸è€‰Q…É•Ğ‘¥É•Ğ½á¥‘…¹ĞÁ•ÉÑÕÉ‰…Ñ¥½¹Ìİ¥Ñ µ…Ñ¡•½¹ÑÉ½±Ì¸ˆ°(€€€€€€€É•ÅÕ•ÍÑ•‘}…Ñ¥½¸è€‰I•ÅÕ•ÍĞ„É•Ù¥Í•Í•…É ½È…¹•°Ñ¡”İ½É­™±½Ü¸ˆ°(€€€€€ô°(€€€ôì(€€€½¹ÍĞ¹½…¹‘¥‘…Ñ•IÕ¸è‘µ¥¹•¹ÑIÕ¸€ôì(€€€€€€¸¸¹ÉÕ¸°(€€€€€ÁÉ½Ù¥‘•Èè€‰½Á•¹…¤ˆ°(€€€€€µ½‘•±}¥‘•¹Ñ¥™¥•Èè€‰ÁĞ´Ô¸Ğµµ¥¹¤ˆ°(€€€€€ÉÕ¹}µ½‘”è€‰±¥Ù”ˆ°(€€€€€ÍÑ…ÑÕÌè€‰½µÁ±•Ñ•ˆ°(€€€€€ÑÕÉ¹Ìè€Ì°(€€€€€Ñ½½±Ìèl(€€€€€€€ì¥è€‰Ñ½½°µÍ•…É ´Äˆ°Ñ½½±}¹…µ”è€‰Í•…É¡}•½}Í•É¥•Ìˆ°ÍÑ…ÑÕÌè€‰½µÁ±•Ñ•ˆ°‘ÕÉ…Ñ¥½¹}µÌè€ÈÀô°(€€€€€€€ì¥è€‰Ñ½½°µÙ…±¥‘…Ñ¥½¸ˆ°Ñ½½±}¹…µ”è€‰Ù…±¥‘…Ñ•}•½}…•ÍÍ¥½¹Ìˆ°ÍÑ…ÑÕÌè€‰½µÁ±•Ñ•ˆ°‘ÕÉ…Ñ¥½¹}µÌè€ÈÀô°(€€€€€€€ì¥è€‰Ñ½½°µ¥¹ÍÁ•Ñ¥½¸ˆ°Ñ½½±}¹…µ”è€‰¥¹ÍÁ•Ñ}•½}…¹‘¥‘…Ñ•Ìˆ°ÍÑ…ÑÕÌè€‰½µÁ±•Ñ•ˆ°‘ÕÉ…Ñ¥½¹}µÌè€ÈÀô°(€€€€€t°(€€€€€Ñ½½±}…±±Ìèmì(€€€€€€€Ñ½½±}¹…µ”è€‰¥¹ÍÁ•Ñ}•½}…¹‘¥‘…Ñ•Ìˆ°(€€€€€€€É•ÍÕ±Ğèì½ÕÑÁÕĞèì¥¹ÍÁ•Ñ•‘}½Õ¹Ğè€Ô°™…¥±•‘}½Õ¹Ğè€Àôô°(€€€€€õt°(€€€€€ÑÉ…”èì•Ù•¹ÑÌèl(€€€€€€€ì•Ù•¹Ñ}ÑåÁ”è€‰ÁÉ½Ù¥‘•È¹ÑÕÉ¸¹ÍÑ…ÉÑ•ˆ°‘•Ñ…¥°èìÑÕÉ¸è€Ä°‘¥Í½Ù•Éå}ÍÕ‰ÍÑ…”è€‰Í•…É¡}Á±…¹¹¥¹œˆ°Ñ½½±Í}•áÁ½Í•èl‰Í•…É¡}•½}Í•É¥•Ì‰tôô°(€€€€€€€ì•Ù•¹Ñ}ÑåÁ”è€‰ÁÉ½Ù¥‘•È¹ÑÕÉ¸¹ÍÑ…ÉÑ•ˆ°‘•Ñ…¥°èìÑÕÉ¸è€È°‘¥Í½Ù•Éå}ÍÕ‰ÍÑ…”è€‰…¹‘¥‘…Ñ•}Ù…±¥‘…Ñ¥½¸ˆ°Ñ½½±Í}•áÁ½Í•èl‰Ù…±¥‘…Ñ•}•½}…•ÍÍ¥½¹Ì‰tôô°(€€€€€€€ì•Ù•¹Ñ}ÑåÁ”è€‰ÁÉ½Ù¥‘•È¹ÑÕÉ¸¹ÍÑ…ÉÑ•ˆ°‘•Ñ…¥°èìÑÕÉ¸è€Ì°‘¥Í½Ù•Éå}ÍÕ‰ÍÑ…”è€‰…¹‘¥‘…Ñ•}¥¹ÍÁ•Ñ¥½¸ˆ°Ñ½½±Í}•áÁ½Í•èl‰¥¹ÍÁ•Ñ}•½}…¹‘¥‘…Ñ•Ì‰tôô°(€€€€€€€ì•Ù•¹Ñ}ÑåÁ”è€‰ÁÉ½Ù¥‘•È¹ÑÕÉ¸¹ÍÑ…ÉÑ•ˆ°‘•Ñ…¥°èìÑÕÉ¸è€Ğ°‘¥Í½Ù•Éå}ÍÕ‰ÍÑ…”è€‰™¥¹…±}½ÕÑÁÕĞˆ°Ñ½½±Í}•áÁ½Í•èmtôô°(€€€€€tô°(€€€ôì(€€€¥¹ÍÑ…±±•Ñ¡5½¬¡ì(€€€€€€¸¸¹‘•Ñ…¥±I½ÕÑ•Ì  ¤€ôø‰Õ¥± ‰]%Q%9}MI!}IY%\ˆ°€Ğ°ìÁ•¹‘¥¹}…ÁÁÉ½Ù…±}¥è…ÁÁÉ½Ù…±%ô¤¤°(€€€€€mP€½…Á¤½…‘µ¥¸½…ÉÑ¥™…ÑÌ¼‘í…ÉÑ¥™…Ñ%‘ô½ÁÉ•Ù¥•İtèì(€€€€€€€‰½‘äèì(€€€€€€€€€…ÉÑ¥™…Ğ°(€€€€€€€€€½¹Ñ•¹Ğèì(€€€€€€€€€€€ÉÕ¹}µ½‘”è€‰±¥Ù”ˆ°(€€€€€€€€€€€±¥Ù•}‘¥Í½Ù•ÉäèÑÉÕ”°(€€€€€€€€€€€…¹‘¥‘…Ñ•Ìèmt°(€€€€€€€€€€€É•½µµ•¹‘•‘}…¹‘¥‘…Ñ•}¥è¹Õ±°°(€€€€€€€€€ô°(€€€€€€€ô°(€€€€€ô°(€€€€€mP€½…Á¤½…‘µ¥¸½•¹‘Á½¥¹Ğµ‰Õ¥±‘Ì¼‘í‰Õ¥±‘%‘ô½…ÁÁÉ½Ù…±Ítèì‰½‘äèmÍ•…É¡I•Ù¥•İtô°(€€€€€mP€½…Á¤½…‘µ¥¸½•¹‘Á½¥¹Ğµ‰Õ¥±‘Ì¼‘í‰Õ¥±‘%‘ô½…•¹ĞµÉÕ¹Ítèì‰½‘äèm¹½…¹‘¥‘…Ñ•IÕ¹tô°(€€€€€mP€½…Á¤½…‘µ¥¸½…•¹ĞµÉÕ¹Ì¼‘íÉÕ¹%‘õtèì‰½‘äè¹½…¹‘¥‘…Ñ•IÕ¸ô°(€€€ô¤ì(€€€É•¹‘•ÉÁÀ¡€½…‘µ¥¸½•¹‘Á½¥¹ÑÌ¼‘í‰Õ¥±‘%‘õ€¤ì(€€€•áÁ•Ğ¡…İ…¥ĞÍÉ••¸¹™¥¹‘	åQ•áĞ ‰M•…É É•Ù¥•ÜÉ•ÅÕ¥É•ˆ¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ	åQ•áĞ ‰	½Õ¹‘•<Í•…É¡•Ì™½Õ¹¹¼ÍÕ¥Ñ…‰±”…¹‘¥‘…Ñ”ˆ¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ	åI½±” ‰‰ÕÑÑ½¸ˆ°ì¹…µ”è€‰I•ÅÕ•ÍĞÉ•Ù¥Í•Í•…É ˆô¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ	åQ•áĞ ‰Q…É•Ğ‘¥É•Ğ½á¥‘…¹ĞÁ•ÉÑÕÉ‰…Ñ¥½¹Ìİ¥Ñ µ…Ñ¡•½¹ÑÉ½±Ì¸ˆ¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ	åQ•áĞ ‰Q½½±Ì•áÁ½Í•Á•ÈÑÕÉ¸ˆ¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ	åQ•áĞ ˆÔ¥¹ÍÁ•Ñ•€¼€ÀÕ¹É•Í½±Ù•ˆ¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹ÅÕ•Éå	åI½±” ‰‰ÕÑÑ½¸ˆ°ì¹…µ”è€‰ÁÁÉ½Ù”‘…Ñ…Í•Ğˆô¤¤¹¹½Ğ¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹ÅÕ•Éå	åQ•áĞ ‰…¥±•ˆ¤¤¹¹½Ğ¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹ÅÕ•Éå	åQ•áĞ ‰½µÁ±•Ñ•™½ÈÉ•Ù¥•Üˆ¤¤¹¹½Ğ¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€ô¤ì((€¥Ğ ‰Í¡½İÌ„™…¥±•±¥Ù”ÉÕ¸™É½´Á•ÉÍ¥ÍÑ•ÉÕ¸ÑÉÕÑ İ¥Ñ¡½ÕĞÉ•Á±…ä½ÈÉ•Ù¥•Ü±…¥µÌˆ°…Íå¹Œ€ ¤€ôøì(€€€½¹ÍĞ™…¥±•‘IÕ¸è‘µ¥¹•¹ÑIÕ¸€ôì(€€€€€€¸¸¹ÉÕ¸°(€€€€€ÁÉ½Ù¥‘•Èè€‰½Á•¹…¤ˆ°(€€€€€µ½‘•±}¥‘•¹Ñ¥™¥•Èè€‰ÁĞ´Ô¸Ğµµ¥¹¤ˆ°(€€€€€ÉÕ¹}µ½‘”è€‰±¥Ù”ˆ°(€€€€€ÍÑ…ÑÕÌè€‰™…¥±•ˆ°(€€€€€ÑÕÉ¹Ìè€Ä°(€€€€€‘ÕÉ…Ñ¥½¹}µÌè€ÄØ°(€€€€€ÕÍ…”èì¥¹ÁÕÑ}Ñ½­•¹Ìè€À°½ÕÑÁÕÑ}Ñ½­•¹Ìè€À°…¡•‘}Ñ½­•¹Ìè€À°½ÍÑ}•¹ÑÌè€Àô°(€€€€€Ñ½½±Ìèmt°(€€€€€ÑÉ…”èì(€€€€€€€•Ù•¹ÑÌèl(€€€€€€€€€ì(€€€€€€€€€€€•Ù•¹Ñ}ÑåÁ”è€‰ÁÉ½Ù¥‘•È¹ÑÕÉ¸¹™…¥±•ˆ°(€€€€€€€€€€€‘•Ñ…¥°èì(€€€€€€€€€€€€€É•ÑÉå…‰±”è™…±Í”°(€€€€€€€€€€€€€•á•ÁÑ¥½¹}±…ÍÌè€‰UÍ•ÉÉÉ½Èˆ°(€€€€€€€€€€€€€‘•Ù•±½Á•É}µ•ÍÍ…”è€‰…‘‘¥Ñ¥½¹…±AÉ½Á•ÉÑ¥•ÌÍ¡½Õ±¹½Ğ‰”Í•Ğ™½È½‰©•ĞÑåÁ•Ì¸ˆ°(€€€€€€€€€€€ô°(€€€€€€€€€ô°(€€€€€€€t°(€€€€€ô°(€€€ôì(€€€½¹ÍĞÑ•Éµ¥¹…±ÉÉ½Èè‘µ¥¹]½É­™±½İÉÉ½È€ôì(€€€€€€¸¸¹İ½É­™±½İÉÉ½ÉÍlÁt°(€€€€€½‘”è€‰ÁÉ½Ù¥‘•É}™…¥±ÕÉ”ˆ°(€€€€€É•ÑÉå…‰±”è™…±Í”°(€€€€€Í…™•}µ•ÍÍ…”è€‰AÉ½Ù¥‘•È™…¥±•…™Ñ•È‰½Õ¹‘•É•ÑÉ¥•Ì¸ˆ°(€€€ôì(€€€¥¹ÍÑ…±±•Ñ¡5½¬¡ì(€€€€€€¸¸¹‘•Ñ…¥±I½ÕÑ•Ì  ¤€ôø‰Õ¥± ‰%1ˆ°€Ğ¤°mÑ•Éµ¥¹…±ÉÉ½Ét¤°(€€€€€mP€½…Á¤½…‘µ¥¸½•¹‘Á½¥¹Ğµ‰Õ¥±‘Ì¼‘í‰Õ¥±‘%‘ô½…ÉÑ¥™…ÑÍtèì‰½‘äèmtô°(€€€€€mP€½…Á¤½…‘µ¥¸½•¹‘Á½¥¹Ğµ‰Õ¥±‘Ì¼‘í‰Õ¥±‘%‘ô½…ÁÁÉ½Ù…±Ítèì‰½‘äèmtô°(€€€€€mP€½…Á¤½…‘µ¥¸½•¹‘Á½¥¹Ğµ‰Õ¥±‘Ì¼‘í‰Õ¥±‘%‘ô½…•¹ĞµÉÕ¹Ítèì‰½‘äèm™…¥±•‘IÕ¹tô°(€€€€€mP€½…Á¤½…‘µ¥¸½…•¹ĞµÉÕ¹Ì¼‘íÉÕ¹%‘õtèì‰½‘äè™…¥±•‘IÕ¸ô°(€€€ô¤ì(€€€É•¹‘•ÉÁÀ¡€½…‘µ¥¸½•¹‘Á½¥¹ÑÌ¼‘í‰Õ¥±‘%‘õ€¤ì(€€€•áÁ•Ğ¡…İ…¥ĞÍÉ••¸¹™¥¹‘	åQ•áĞ ‰1¥Ù”…•¹ĞÉÕ¸™…¥±•ˆ¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ	åQ•áĞ ‰1¥Ù”…•¹Ğµ½‘”ˆ¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ	åQ•áĞ ‰9¼É•½µµ•¹‘…Ñ¥½¸…Ù…¥±…‰±”ˆ¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ	åQ•áĞ ‰…‘‘¥Ñ¥½¹…±AÉ½Á•ÉÑ¥•ÌÍ¡½Õ±¹½Ğ‰”Í•Ğ™½È½‰©•ĞÑåÁ•Ì¸ˆ¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ	åQ•áĞ ‰9¼É•Ù¥•ÜÉ•ÅÕ¥É•ˆ¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹ÅÕ•Éå	åQ•áĞ ‰I•Á±…äµ½‘”ˆ¤¤¹¹½Ğ¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹ÅÕ•Éå	åQ•áĞ ‰½µÁ±•Ñ•™½ÈÉ•Ù¥•Üˆ¤¤¹¹½Ğ¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹ÅÕ•Éå	åI½±” ‰‰ÕÑÑ½¸ˆ°ì¹…µ”è€‰I•ÑÉä™…¥±•ÍÑ•Àˆô¤¤¹¹½Ğ¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹ÅÕ•Éå	åI½±” ‰‰ÕÑÑ½¸ˆ°ì¹…µ”è€‰ÁÁÉ½Ù”‘…Ñ…Í•Ğˆô¤¤¹¹½Ğ¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€½¹ÍĞ…Ñ¥Ù¥Ñä€ôÍÉ••¸¹•Ñ	åI½±” ‰¡•…‘¥¹œˆ°ì¹…µ”è€‰•¹Ğ…Ñ¥Ù¥Ñäˆô¤¹±½Í•ÍĞ ‰Í•Ñ¥½¸ˆ¤„ì(€€€•áÁ•Ğ¡İ¥Ñ¡¥¸¡…Ñ¥Ù¥Ñä¤¹•Ñ	åQ•áĞ ‰AÉ½Ù¥‘•ÈÉ•ÑÉ¥•Ìˆ¤¹Á…É•¹Ñ±•µ•¹Ğ¤¹Ñ½!…Ù•Q•áÑ½¹Ñ•¹Ğ ˆÀˆ¤ì(€€€•áÁ•Ğ¡İ¥Ñ¡¥¸¡…Ñ¥Ù¥Ñä¤¹•Ñ	åQ•áĞ ‰Q½½°…±±Ìˆ¤¹Á…É•¹Ñ±•µ•¹Ğ¤¹Ñ½!…Ù•Q•áÑ½¹Ñ•¹Ğ ˆÀˆ¤ì(€€€•áÁ•Ğ¡İ¥Ñ¡¥¸¡…Ñ¥Ù¥Ñä¤¹•Ñ	åQ•áĞ ‰5½‘•°ÑÕÉ¹Ìˆ¤¹Á…É•¹Ñ±•µ•¹Ğ¤¹Ñ½!…Ù•Q•áÑ½¹Ñ•¹Ğ ˆÄˆ¤ì(€ô¤ì((€¥Ğ ‰É•¹‘•ÉÌ½¹±ä…±±½İ±¥ÍÑ•Í¥•¹Ñ¥™¥ŒµÍ½ÕÉ”‘¥…¹½ÍÑ¥Ìˆ°…Íå¹Œ€ ¤€ôøì(€€€½¹ÍĞÍ½ÕÉ•¥…¹½ÍÑ¥Œ€ôì(€€€€€Ñ½½±}¹…µ”è€‰Ù…±¥‘…Ñ•}•½}…•ÍÍ¥½¹Ìˆ°(€€€€€Í½ÕÉ•}¡½ÍĞè€‰İİÜ¹¹‰¤¹¹±´¹¹¥ ¹½Øˆ°(€€€€€Í…™•}ÕÉ±}Á…Ñ è€ˆ½•¼½ÅÕ•Éä½…Œ¹¤ˆ°(€€€€€¡ÑÑÁ}µ•Ñ¡½è€‰Pˆ…Ì½¹ÍĞ°(€€€€€¡ÑÑÁ}ÍÑ…ÑÕÌè€ÈÀÀ°(€€€€€™¥¹…±}…ÁÁÉ½Ù•‘}¡½ÍĞè€‰İİÜ¹¹‰¤¹¹±´¹¹¥ ¹½Øˆ°(€€€€€½¹Ñ•¹Ñ}ÑåÁ”è€‰•¼½Ñ•áĞˆ°(€€€€€…ÉÑ¥™…Ñ}½¹Ñ•¹Ñ}ÑåÁ”è€‰Ñ•áĞ½Á±…¥¸ˆ°(€€€€€É•ÍÁ½¹Í•}‰åÑ•}½Õ¹Ğè€ÔÄÈ°(€€€€€Á…ÉÍ•É}½ÕÑ½µ”è€‰ÁÕ‰±¥}Ù…±¥ˆ°(€€€€€Í½ÕÉ•}…ÉÑ¥™…Ñ}¥è€‰…ÉĞµ•¼µÍ½ÕÉ”ˆ°(€€€€€…¡•}ÍÑ…ÑÕÌè€‰±¥Ù”ˆ…Ì½¹ÍĞ°(€€€€€•á•ÁÑ¥½¹}±…ÍÌè€‰M½ÕÉ•½Éµ…ÑÉÉ½Èˆ°(€€€€€Í½ÕÉ•}•ÉÉ½É}…Ñ•½Éäè€‰Õ¹•áÁ•Ñ•‘}½¹Ñ•¹Ñ}ÑåÁ”ˆ°(€€€€€É•ÑÉå…‰±”è™…±Í”°(€€€€€…ÑÑ•µÁÑ}¹Õµ‰•Èè€Ä°(€€€€€É•ÅÕ•ÍÑ}‘ÕÉ…Ñ¥½¹}µÌè€ÔØÈ°(€€€€€‘•Ù•±½Á•É}µ•ÍÍ…”è€‰áÁ•Ñ•„µ…¡¥¹”µÉ•…‘…‰±”<É•ÍÁ½¹Í”¸ˆ°(€€€ôì(€€€½¹ÍĞ™…¥±•‘IÕ¸è‘µ¥¹•¹ÑIÕ¸€ôì(€€€€€€¸¸¹ÉÕ¸°(€€€€€ÁÉ½Ù¥‘•Èè€‰½Á•¹…¤ˆ°(€€€€€µ½‘•±}¥‘•¹Ñ¥™¥•Èè€‰ÁĞ´Ô¸Ğµµ¥¹¤ˆ°(€€€€€ÉÕ¹}µ½‘”è€‰±¥Ù”ˆ°(€€€€€ÍÑ…ÑÕÌè€‰™…¥±•ˆ°(€€€€€ÑÕÉ¹Ìè€Ä°(€€€€€Ñ½½±Ìèmì(€€€€€€€¥è€‰Ñ½½°µÍ½ÕÉ”µ™…¥±ÕÉ”ˆ°(€€€€€€€Ñ½½±}¹…µ”è€‰Ù…±¥‘…Ñ•}•½}…•ÍÍ¥½¹Ìˆ°(€€€€€€€ÍÑ…ÑÕÌè€‰™…¥±•ˆ°(€€€€€€€‘ÕÉ…Ñ¥½¹}µÌè€ÔØÈ°(€€€€€õt°(€€€€€Ñ½½±}…±±Ìèmì(€€€€€€€Ñ½½±}¹…µ”è€‰Ù…±¥‘…Ñ•}•½}…•ÍÍ¥½¹Ìˆ°(€€€€€€€É•ÍÕ±ĞèìÍ½ÕÉ•}‘¥…¹½ÍÑ¥ŒèÍ½ÕÉ•¥…¹½ÍÑ¥Œô°(€€€€€õt°(€€€ôì(€€€½¹ÍĞÍ½ÕÉ•ÉÉ½Èè‘µ¥¹]½É­™±½İÉÉ½È€ôì(€€€€€€¸¸¹İ½É­™±½İÉÉ½ÉÍlÁt°(€€€€€½‘”è€‰Í½ÕÉ•}Õ¹•áÁ•Ñ•‘}½¹Ñ•¹Ñ}ÑåÁ”ˆ°(€€€€€…Ñ•½Éäè€‰Í½ÕÉ•}Ñ½½°ˆ°(€€€€€É•ÑÉå…‰±”è™…±Í”°(€€€€€Í…™•}µ•ÍÍ…”è€‰M¥•¹Ñ¥™¥ŒÍ½ÕÉ”É•ÑÕÉ¹•…¸Õ¹•áÁ•Ñ•½¹Ñ•¹ĞÑåÁ”¸ˆ°(€€€€€‘•Ñ…¥°èìÍ½ÕÉ•}‘¥…¹½ÍÑ¥ŒèÍ½ÕÉ•¥…¹½ÍÑ¥Œô°(€€€ôì(€€€¥¹ÍÑ…±±•Ñ¡5½¬¡ì(€€€€€€¸¸¹‘•Ñ…¥±I½ÕÑ•Ì  ¤€ôø‰Õ¥± ‰%1ˆ°€Ğ¤°mÍ½ÕÉ•ÉÉ½Ét¤°(€€€€€mP€½…Á¤½…‘µ¥¸½•¹‘Á½¥¹Ğµ‰Õ¥±‘Ì¼‘í‰Õ¥±‘%‘ô½…ÉÑ¥™…ÑÍtèì‰½‘äèmtô°(€€€€€mP€½…Á¤½…‘µ¥¸½•¹‘Á½¥¹Ğµ‰Õ¥±‘Ì¼‘í‰Õ¥±‘%‘ô½…ÁÁÉ½Ù…±Ítèì‰½‘äèmtô°(€€€€€mP€½…Á¤½…‘µ¥¸½•¹‘Á½¥¹Ğµ‰Õ¥±‘Ì¼‘í‰Õ¥±‘%‘ô½…•¹ĞµÉÕ¹Ítèì‰½‘äèm™…¥±•‘IÕ¹tô°(€€€€€mP€½…Á¤½…‘µ¥¸½…•¹ĞµÉÕ¹Ì¼‘íÉÕ¹%‘õtèì‰½‘äè™…¥±•‘IÕ¸ô°(€€€ô¤ì(€€€É•¹‘•ÉÁÀ¡€½…‘µ¥¸½•¹‘Á½¥¹ÑÌ¼‘í‰Õ¥±‘%‘õ€¤ì(€€€•áÁ•Ğ¡…İ…¥ĞÍÉ••¸¹™¥¹‘	åQ•áĞ ‰M¥•¹Ñ¥™¥ŒµÍ½ÕÉ”‘¥…¹½ÍÑ¥Ìˆ¤¤¹Ñ½	•%¹Q¡•½Õµ•¹Ğ ¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ±±	åQ•áĞ ‰İİÜ¹¹‰¤¹¹±´¹¹¥ ¹½Ø½•¼½ÅÕ•Éä½…Œ¹¤ˆ¤¤¹Ñ½!…Ù•1•¹Ñ  È¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ±±	åQ•áĞ ½P¸¨ÈÀÀ¼¤¤¹Ñ½!…Ù•1•¹Ñ  È¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ±±	åQ•áĞ ½•½p½Ñ•áĞ¸¨ÔÄÈ‰åÑ•Ì¼¤¤¹Ñ½!…Ù•1•¹Ñ  È¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ±±	åQ•áĞ ‰Ñ•áĞ½Á±…¥¸ˆ¤¤¹Ñ½!…Ù•1•¹Ñ  È¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ±±	åQ•áĞ ‰AÕ‰±¥ŒÙ…±¥ˆ¤¤¹Ñ½!…Ù•1•¹Ñ  È¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ±±	åQ•áĞ ‰…ÉĞµ•¼µÍ½ÕÉ”ˆ¤¤¹Ñ½!…Ù•1•¹Ñ  È¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ±±	åQ•áĞ ‰M½ÕÉ•½Éµ…ÑÉÉ½Èˆ¤¤¹Ñ½!…Ù•1•¹Ñ  È¤ì(€€€•áÁ•Ğ¡ÍÉ••¸¹•Ñ±±	åQ•áĞ ‰áÁ•Ñ•„µ…¡¥¹”µÉ•…‘…‰±”<É•ÍÁ½¹Í”¸ˆ¤¤¹Ñ½!…Ù•1•¹Ñ  È¤ì(€€€•áÁ•Ğ¡‘½Õµ•¹Ğ¹‰½‘ä¤¹¹½Ğ¹Ñ½!…Ù•Q•áÑ½¹Ñ•¹Ğ ‰‘¼µ¹½ĞµÍÑ½É”ˆ¤ì(€€€•áÁ•Ğ¡‘½Õµ•¹Ğ¹‰½‘ä¤¹¹½Ğ¹Ñ½!…Ù•Q•áÑ½¹Ñ•¹Ğ ‰ÕÑ¡½É¥é…Ñ¥½¸ˆ¤ì(€ô¤ì)ô¤ì