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
        explicit_prediction_grain: "compound Ã— transcriptomic experimental context",
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
    expect(screen.getByText("compound Ã— transcriptomic experimental context")).toBeInTheDocument();
    expect(screen.getByText("Should continuous activity values be preserved, classes derived, or both?")).toBeInTheDocument();
    expect(screen.getByText("Optional AI review has not been requested.")).toBeInThe×OtîÚ$z{-®éÜj×¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒövVçB×'Vç6Ó¢²&öG“¢¶æô6æF–FFU'VåÒÒÀ¢¶tUBö’öFÖ–âövVçB×'Vç2òG·'Vä–GÖÓ¢²&öG“¢æô6æF–FFU'VâÒÀ¢Ò“°¢&VæFW$†öFÖ–âöVæGö–çG2òG¶'V–ÆD–GÖ“°¢W‡V7B†v—B67&VVâæf–æD'•FW‡B‚%6V&6‚&Wf–Wr&WV—&VB"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWD'•FW‡B‚$&÷VæFVBtTò6V&6†W2f÷VæBæò7V—F&ÆR6æF–FFR"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWD'•&öÆR‚&'WGFöâ"Â²æÖS¢%&WVW7B&Wf—6VB6V&6‚"Ò’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWD'•FW‡B‚%F&vWBF—&V7B÷†–FçBW'GW&&F–öç2v—F‚ÖF6†VB6öçG&öÇ2â"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWD'•FW‡B‚%FööÇ2W‡÷6VBW"GW&â"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWD'•FW‡B‚#R–ç7V7FVBòVç&W6öÇfVB"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâçVW'”'•&öÆR‚&'WGFöâ"Â²æÖS¢$&÷fRFF6WB"Ò’’ææ÷BçFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâçVW'”'•FW‡B‚$f–ÆVB"’’ææ÷BçFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâçVW'”'•FW‡B‚$6ö×ÆWFVBf÷"&Wf–Wr"’’ææ÷BçFô&T–åF†TFö7VÖVçB‚“°¢Ò“° ¢—B‚'6†÷w2f–ÆVBÆ—fR'Vâg&öÒW'6—7FVB'VâG'WF‚v—F†÷WB&WÆ’÷"&Wf–Wr6Æ–×2"Â7–æ2‚’Óâ°¢6öç7Bf–ÆVE'Vã¢FÖ–ävVçE'VâÒ°¢ââç'VâÀ¢&÷f–FW#¢&÷Væ’"À¢ÖöFVÅö–FVçF–f–W#¢&wBÓRãBÖÖ–æ’"À¢'VåöÖöFS¢&Æ—fR"À¢7FGW3¢&f–ÆVB"À¢GW&ç3¢À¢GW&F–öåö×3¢bÀ¢W6vS¢²–çWE÷Fö¶Vç3¢Â÷WGWE÷Fö¶Vç3¢Â66†VE÷Fö¶Vç3¢Â6÷7Eö6VçG3¢ÒÀ¢FööÇ3¢µÒÀ¢G&6S¢°¢WfVçG3¢°¢°¢WfVçE÷G—S¢'&÷f–FW"çGW&âæf–ÆVB"À¢FWF–Ã¢°¢&WG'–&ÆS¢fÇ6RÀ¢W†6WF–öåö6Æ73¢%W6W$W'&÷""À¢FWfVÆ÷W%öÖW76vS¢&FF—F–öæÅ&÷W'F–W26†÷VÆBæ÷B&R6WBf÷"ö&¦V7BG—W2â"À¢ÒÀ¢ÒÀ¢ÒÀ¢ÒÀ¢Ó°¢6öç7BFW&Ö–æÄW'&÷#¢FÖ–åv÷&¶fÆ÷tW'&÷"Ò°¢ââçv÷&¶fÆ÷tW'&÷'5³ÒÀ¢6öFS¢'&÷f–FW%öf–ÇW&R"À¢&WG'–&ÆS¢fÇ6RÀ¢6fUöÖW76vS¢%&÷f–FW"f–ÆVBgFW"&÷VæFVB&WG&–W2â"À¢Ó°¢–ç7FÆÄfWF6„Öö6²‡°¢ââæFWF–Å&÷WFW2‚‚’Óâ'V–ÆB‚$d”ÄTB"ÂB’Â·FW&Ö–æÄW'&÷%Ò’À¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒö'F–f7G6Ó¢²&öG“¢µÒÒÀ¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒö&÷fÇ6Ó¢²&öG“¢µÒÒÀ¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒövVçB×'Vç6Ó¢²&öG“¢¶f–ÆVE'VåÒÒÀ¢¶tUBö’öFÖ–âövVçB×'Vç2òG·'Vä–GÖÓ¢²&öG“¢f–ÆVE'VâÒÀ¢Ò“°¢&VæFW$†öFÖ–âöVæGö–çG2òG¶'V–ÆD–GÖ“°¢W‡V7B†v—B67&VVâæf–æD'•FW‡B‚$Æ—fRvVçB'Vâf–ÆVB"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWD'•FW‡B‚$Æ—fRvVçBÖöFR"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWD'•FW‡B‚$æò&V6öÖÖVæFF–öâf–Æ&ÆR"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWD'•FW‡B‚&FF—F–öæÅ&÷W'F–W26†÷VÆBæ÷B&R6WBf÷"ö&¦V7BG—W2â"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWD'•FW‡B‚$æò&Wf–Wr&WV—&VB"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâçVW'”'•FW‡B‚%&WÆ’ÖöFR"’’ææ÷BçFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâçVW'”'•FW‡B‚$6ö×ÆWFVBf÷"&Wf–Wr"’’ææ÷BçFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâçVW'”'•&öÆR‚&'WGFöâ"Â²æÖS¢%&WG'’f–ÆVB7FW"Ò’’ææ÷BçFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâçVW'”'•&öÆR‚&'WGFöâ"Â²æÖS¢$&÷fRFF6WB"Ò’’ææ÷BçFô&T–åF†TFö7VÖVçB‚“°¢6öç7B7F—f—G’Ò67&VVâævWD'•&öÆR‚&†VF–ær"Â²æÖS¢$vVçB7F—f—G’"Ò’æ6Æ÷6W7B‚'6V7F–öâ"’°¢W‡V7B‡v—F†–â†7F—f—G’’ævWD'•FW‡B‚%&÷f–FW"&WG&–W2"’ç&VçDVÆVÖVçB’çFô†fUFW‡D6öçFVçB‚#"“°¢W‡V7B‡v—F†–â†7F—f—G’’ævWD'•FW‡B‚%FööÂ6ÆÇ2"’ç&VçDVÆVÖVçB’çFô†fUFW‡D6öçFVçB‚#"“°¢W‡V7B‡v—F†–â†7F—f—G’’ævWD'•FW‡B‚$ÖöFVÂGW&ç2"’ç&VçDVÆVÖVçB’çFô†fUFW‡D6öçFVçB‚#"“°¢Ò“° ¢—B‚'6†÷w2F†R7V6–f–6F–öâ&Wf—6–öâvFRv—F†÷WBf¶RW6vR÷"6÷W&6R6Æ–×2"Â7–æ2‚’Óâ°¢6öç7B&Wf—6–öå'Vã¢FÖ–ävVçE'VâÒ°¢ââç'VâÀ¢vVçEöæÖS¢$FF6WB7V6–f–6F–öâvVçB"À¢&÷f–FW#¢&÷Væ’"À¢ÖöFVÅö–FVçF–f–W#¢&wBÓRãBÖÖ–æ’"À¢'VåöÖöFS¢&Æ—fR"À¢7FGW3¢&6ö×ÆWFVB"À¢GW&ç3¢À¢FööÇ3¢µÒÀ¢W6vS¢°¢W6vU÷7FGW3¢'W6vU÷Væf–Æ&ÆR"À¢–çWE÷Fö¶Vç3¢À¢÷WGWE÷Fö¶Vç3¢À¢66†VE÷Fö¶Vç3¢À¢6÷7Eö6VçG3¢À¢&÷f–FW%ö–çfö6F–öç3¢À¢ÒÀ¢G&6S¢°¢WfVçG3¢·°¢WfVçE÷G—S¢'&÷f–FW"çGW&âæ6ö×ÆWFVB"À¢FWF–Ã¢°¢7G'V7GW&VEö÷WGWEöF–væ÷7F–3¢°¢FWfVÆ÷W%öÖW76vS¢%F†RÖöFVÂ&W7öç6RF–Bæ÷BÖF6‚F†R&WV—&VB7G'V7GW&VB66†VÖâ"À¢f–ÇW&Uö6Æ76–f–6F–öã¢'66†VÖ÷fÆ–FF–öåöf–ÆVB"À¢÷WGWE÷66†VÖöæÖS¢$FF6WE7V6–f–6F–öävVçD÷WF6öÖR"À¢÷WGWE÷66†VÖ÷fW'6–öã¢#ãã"À¢&÷f–FW%÷&WVW7Eö–G3¢²'&W÷6fUó#2%ÒÀ¢&÷f–FW%÷&W7öç6Uö–G3¢²'&W7÷6fUóCSb%ÒÀ¢W6vS¢²W6vU÷7FGW3¢'W6vU÷Væf–Æ&ÆR"ÒÀ¢W'&÷%ö†æFÆW#¢&–çfÆ–Eöf–æÅö÷WGWB"À¢ÒÀ¢ÒÀ¢ÕÒÀ¢ÒÀ¢Ó°¢–ç7FÆÄfWF6„Öö6²‡°¢ââæFWF–Å&÷WFW2‚‚’Óâ'V–ÆB‚$t•D”äuôDD4UEõ5T4”d”4D”ôåõ$Ud•4”ôâ"Â"Â°¢v÷&¶fÆ÷uö¶–æC¢'G&–æ–æuöFF6WEöF—66÷fW'’"À¢VæF–æuö&÷fÅö–C¢çVÆÂÀ¢Ò’’À¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒö'F–f7G6Ó¢²&öG“¢µÒÒÀ¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒö&÷fÇ6Ó¢²&öG“¢µÒÒÀ¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒövVçB×'Vç6Ó¢²&öG“¢·&Wf—6–öå'VåÒÒÀ¢¶tUBö’öFÖ–âövVçB×'Vç2òG·'Vä–GÖÓ¢²&öG“¢&Wf—6–öå'VâÒÀ¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒ÷G&–æ–ærÖFF6WB×v÷&¶fÆ÷vÓ¢°¢&öG“¢°¢66†VÖ÷fW'6–öã¢#ãã"À¢v÷&¶fÆ÷uö–C¢'V–ÆD–BÀ¢v÷&¶fÆ÷uö¶–æC¢'G&–æ–æuöFF6WEöF—66÷fW'’"À¢&Væ6†Ö&µöÖöFS¢&&Æ–æE÷G&–æ–æuöFF6WEöF—66÷fW'’"À¢ÆVv7“¢fÇ6RÀ¢7V6–f–6F–öåöG&gC¢çVÆÂÀ¢F&vWE÷7V6–f–6F–öã¢çVÆÂÀ¢fW&–f–VE÷6÷W&6Uö–çfVçF÷'“¢çVÆÂÀ¢76VÖ&Ç•÷7G&FVv–W3¢çVÆÂÀ¢7V6–f–6F–öåövVçEö÷WF6öÖS¢°¢7FGW3¢&–çfÆ–EöÖöFVÅö÷WGWB"À¢f–ÇW&Uö6FVv÷'“¢'66†VÖ÷fÆ–FF–öåöf–ÆVB"À¢ÒÀ¢Ò6F—6f–W2FÖ–åG&–æ–ætFF6WEv÷&¶fÆ÷rÀ¢ÒÀ¢Ò“°¢&VæFW$†öFÖ–âöVæGö–çG2òG¶'V–ÆD–GÖ“°¢W‡V7B†v—B67&VVâæf–æD'•&öÆR‚&†VF–ær"Â²æÖS¢$FF6WB7V6–f–6F–öâæVVG2&Wf—6–öâ"Ò’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWD'•FW‡B‚%F†RvVçB&W7öç6RF–Bæ÷BÖF6‚F†R&WV—&VB7G'V7GW&VB6öçG&7Bâæò6÷W&6RF—66÷fW'’v27F'FVBâ"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWDÆÄ'•&öÆR‚&'WGFöâ"Â²æÖS¢%&Wf—6RVæGö–çB&WVW7B"Ò’æÆVæwF‚’çFô&Tw&VFW%F†âƒ“°¢W‡V7B‡67&VVâævWDÆÄ'•&öÆR‚&'WGFöâ"Â²æÖS¢%&WG'’7V6–f–6F–öâ"Ò’æÆVæwF‚’çFô&Tw&VFW%F†âƒ“°¢W‡V7B‡67&VVâævWDÆÄ'•&öÆR‚&'WGFöâ"Â²æÖS¢%W6Ræ÷F†W"ÆææW"ÖöFVÂ"Ò’æÆVæwF‚’çFô&Tw&VFW%F†âƒ“°¢W‡V7B‡67&VVâævWD'•FW‡B‚%W6vRVæf–Æ&ÆRgFW"7G'V7GW&VBÖ÷WGWBf–ÇW&R"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWD'•FW‡B‚'&W÷6fUó#2"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWD'•FW‡B‚'&W7÷6fUóCSb"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâçVW'”'•&öÆR‚&†VF–ær"Â²æÖS¢%fW&–f–VB6÷W&6R–çfVçF÷'’"Ò’’ææ÷BçFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâçVW'”'•&öÆR‚&'WGFöâ"Â²æÖS¢$&÷fRFF6WB"Ò’’ææ÷BçFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâçVW'”'•FW‡B‚$&÷VæFVBtTò6V&6†W2f÷VæBæò7V—F&ÆR6æF–FFR"’’ææ÷BçFô&T–åF†TFö7VÖVçB‚“°¢Ò“° ¢—B‚'&VæFW'2fÆ–B†—7F÷&–6Â–ç7Vff–6–VçB÷WF6öÖRv—F†÷WB66†VÖÖf–ÇW&R6Æ–Ò"Â7–æ2‚’Óâ°¢6öç7B†—7F÷&–6Å'Vã¢FÖ–ävVçE'VâÒ°¢ââç'VâÀ¢vVçEöæÖS¢$FF6WB7V6–f–6F–öâvVçB"À¢&÷f–FW#¢&÷Væ’"À¢ÖöFVÅö–FVçF–f–W#¢&wBÓRãBÖÖ–æ’"À¢'VåöÖöFS¢&Æ—fR"À¢7FGW3¢&6ö×ÆWFVB"À¢GW&ç3¢À¢GW&F–öåö×3¢#3BÀ¢FööÇ3¢µÒÀ¢W6vS¢°¢W6vU÷7FGW3¢'W6vU÷&V6÷&FVB"À¢–çWE÷Fö¶Vç3¢3#À¢÷WGWE÷Fö¶Vç3¢#2À¢66†VE÷Fö¶Vç3¢CRÀ¢6÷7Eö6VçG3¢ãC"À¢&÷f–FW%ö–çfö6F–öç3¢À¢&÷f–FW%÷&WVW7Eö–G3¢²'&Wö†—7F÷&–6Å÷6fR%ÒÀ¢&÷f–FW%÷&W7öç6Uö–G3¢²'&W7ö†—7F÷&–6Å÷6fR%ÒÀ¢ÒÀ¢G&6S¢²WfVçG3¢µÒÒÀ¢Ó°¢–ç7FÆÄfWF6„Öö6²‡°¢ââæFWF–Å&÷WFW2‚‚’Óâ'V–ÆB‚$t•D”äuôDD4UEõ5T4”d”4D”ôåõ$Ud•4”ôâ"Â"Â°¢v÷&¶fÆ÷uö¶–æC¢'G&–æ–æuöFF6WEöF—66÷fW'’"À¢VæF–æuö&÷fÅö–C¢çVÆÂÀ¢Ò’’À¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒö'F–f7G6Ó¢²&öG“¢µÒÒÀ¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒö&÷fÇ6Ó¢²&öG“¢µÒÒÀ¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒövVçB×'Vç6Ó¢²&öG“¢¶†—7F÷&–6Å'VåÒÒÀ¢¶tUBö’öFÖ–âövVçB×'Vç2òG·'Vä–GÖÓ¢²&öG“¢†—7F÷&–6Å'VâÒÀ¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒ÷G&–æ–ærÖFF6WB×v÷&¶fÆ÷vÓ¢°¢&öG“¢°¢66†VÖ÷fW'6–öã¢#ãã"À¢v÷&¶fÆ÷uö–C¢'V–ÆD–BÀ¢v÷&¶fÆ÷uö¶–æC¢'G&–æ–æuöFF6WEöF—66÷fW'’"À¢&Væ6†Ö&µöÖöFS¢&&Æ–æE÷G&–æ–æuöFF6WEöF—66÷fW'’"À¢ÆVv7“¢fÇ6RÀ¢7V6–f–6F–öåöG&gC¢çVÆÂÀ¢7V6–f–6F–öåövVçEö÷WF6öÖS¢°¢7FGW3¢&–ç7Vff–6–VçEöVæGö–çEöFVf–æ—F–öâ"À¢7V6–f–6F–öã¢çVÆÂÀ¢FV6—6–öå÷7VÖÖ'“¢$6öç7G'V7F–öâ×öÆ–7’6†ö–6Rv2G&VFVB2&Æö6¶–ærâ"À¢Vç&W6öÇfVE÷VW7F–öç3¢²%6†÷VÆB7F—f—G’&R&–æ'’÷"6öçF–çV÷W3ò%ÒÀ¢Æ–Ö—FF–öç3¢²$æò6÷W&6RF—66÷fW'’v27F'FVBâ%ÒÀ¢f–ÇW&Uö6FVv÷'“¢çVÆÂÀ¢ÒÀ¢Ò6F—6f–W2FÖ–åG&–æ–ætFF6WEv÷&¶fÆ÷rÀ¢ÒÀ¢Ò“°¢&VæFW$†öFÖ–âöVæGö–çG2òG¶'V–ÆD–GÖ“°¢W‡V7B†v—B67&VVâæf–æD'•&öÆR‚&†VF–ær"Â²æÖS¢$VæGö–çBFVf–æ—F–öâæVVG26Æ&–f–6F–öâ"Ò’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWD'•FW‡B‚%F†RvVçB&öGV6VBfÆ–B7G'V7GW&VB&W7öç6R'WB6÷VÆBæ÷B7&VFRFF6WB×7V6–f–6F–öâG&gBg&öÒF†RVæGö–çBFVf–æ—F–öââ"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWD'•FW‡B‚$6öç7G'V7F–öâ×öÆ–7’6†ö–6Rv2G&VFVB2&Æö6¶–ærâ"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWD'•FW‡B‚%6†÷VÆB7F—f—G’&R&–æ'’÷"6öçF–çV÷W3ò"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâçVW'”'•FW‡B‚÷66†VÖÖ—6ÖF6‚ö’’’ææ÷BçFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâçVW'”'•FW‡B‚õVæ¶æ÷vâÖöFVÂ&V†f–÷"ö’’’ææ÷BçFô&T–åF†TFö7VÖVçB‚“°¢v—BW6W$WfVçBæ6Æ–6²‡67&VVâævWD'•FW‡B‚%FV6†æ–6ÂVF—B"’“°¢W‡V7B‡67&VVâævWD'•FW‡B‚'&Wö†—7F÷&–6Å÷6fR"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWD'•FW‡B‚'&W7ö†—7F÷&–6Å÷6fR"’’çFô&T–åF†TFö7VÖVçB‚“°¢Ò“° ¢—B‚'&W6VçG26VÖçF–2×öÆ–7’Ö—6ÖF6‚6W&FVÇ’g&öÒÖÆf÷&ÖVB÷WGWB"Â7–æ2‚’Óâ°¢6öç7B6VÖçF–5'Vã¢FÖ–ävVçE'VâÒ°¢ââç'VâÀ¢vVçEöæÖS¢$FF6WB7V6–f–6F–öâvVçB"À¢7FGW3¢&6ö×ÆWFVB"À¢FööÇ3¢µÒÀ¢G&6S¢²WfVçG3¢µÒÒÀ¢Ó°¢–ç7FÆÄfWF6„Öö6²‡°¢ââæFWF–Å&÷WFW2‚‚’Óâ'V–ÆB‚$t•D”äuôDD4UEõ5T4”d”4D”ôåõ$Ud•4”ôâ"Â"Â°¢v÷&¶fÆ÷uö¶–æC¢'G&–æ–æuöFF6WEöF—66÷fW'’"À¢VæF–æuö&÷fÅö–C¢çVÆÂÀ¢Ò’’À¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒö'F–f7G6Ó¢²&öG“¢µÒÒÀ¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒö&÷fÇ6Ó¢²&öG“¢µÒÒÀ¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒövVçB×'Vç6Ó¢²&öG“¢·6VÖçF–5'VåÒÒÀ¢¶tUBö’öFÖ–âövVçB×'Vç2òG·'Vä–GÖÓ¢²&öG“¢6VÖçF–5'VâÒÀ¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒ÷G&–æ–ærÖFF6WB×v÷&¶fÆ÷vÓ¢°¢&öG“¢°¢66†VÖ÷fW'6–öã¢#ãã"À¢v÷&¶fÆ÷uö–C¢'V–ÆD–BÀ¢v÷&¶fÆ÷uö¶–æC¢'G&–æ–æuöFF6WEöF—66÷fW'’"À¢ÆVv7“¢fÇ6RÀ¢7V6–f–6F–öåövVçEö÷WF6öÖS¢°¢7FGW3¢&–ç7Vff–6–VçEöVæGö–çEöFVf–æ—F–öâ"À¢FV6—6–öå÷7VÖÖ'“¢%öÆ–7’6†ö–6W2&WfVçFVBG&gBâ"À¢ÒÀ¢7V6–f–6F–öå÷6VÖçF–5÷fÆ–FF–öã¢°¢7FGW3¢'6VÖçF–5ö6öçG&7E÷f–öÆF–öâ"À¢f–öÆF–öåö6öFS¢&æöåö&Æö6¶–æu÷öÆ–7•÷G&VFVEö5ö6÷&UöÖ—76–ær"À¢ÒÀ¢Ò6F—6f–W2FÖ–åG&–æ–ætFF6WEv÷&¶fÆ÷rÀ¢ÒÀ¢Ò“°¢&VæFW$†öFÖ–âöVæGö–çG2òG¶'V–ÆD–GÖ“°¢W‡V7B†v—B67&VVâæf–æD'•&öÆR‚&†VF–ær"Â²æÖS¢$FF6WB7V6–f–6F–öâöÆ–7’æVVG2&Wf—6–öâ"Ò’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWD'•FW‡B‚%F†RVæGö–çBÇ&VG’6öçF–ç2âW‡Æ–6—BF&vWBæBÖöFÆ—G’Â'WBF†RvVçBG&VFVBæöâÖ&Æö6¶–ærFF6WB×öÆ–7’6†ö–6W22&Æö6¶–ærâ"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâçVW'”'•FW‡B‚÷66†VÖÖ—6ÖF6‚ö’’’ææ÷BçFô&T–åF†TFö7VÖVçB‚“°¢Ò“° ¢—B‚'&VæFW'2öæÇ’ÆÆ÷vÆ—7FVB66–VçF–f–2×6÷W&6RF–væ÷7F–72"Â7–æ2‚’Óâ°¢6öç7B6÷W&6TF–væ÷7F–2Ò°¢FööÅöæÖS¢'fÆ–FFUövVõö66W76–öç2"À¢6÷W&6Uö†÷7C¢'wwrææ6&’ææÆÒææ–‚æv÷b"À¢6fU÷W&Å÷Fƒ¢"övVò÷VW'’ö62æ6v’"À¢‡GGöÖWF†öC¢$tUB"26öç7BÀ¢‡GG÷7FGW3¢#À¢f–æÅö&÷fVEö†÷7C¢'wwrææ6&’ææÆÒææ–‚æv÷b"À¢6öçFVçE÷G—S¢&vVò÷FW‡B"À¢'F–f7Eö6öçFVçE÷G—S¢'FW‡B÷Æ–â"À¢&W7öç6Uö'—FUö6÷VçC¢S"À¢'6W%ö÷WF6öÖS¢'V&Æ–5÷fÆ–B"À¢6÷W&6Uö'F–f7Eö–C¢&'BÖvVò×6÷W&6R"À¢66†U÷7FGW3¢&Æ—fR"26öç7BÀ¢W†6WF–öåö6Æ73¢%6÷W&6Tf÷&ÖDW'&÷""À¢6÷W&6UöW'&÷%ö6FVv÷'“¢'VæW‡V7FVEö6öçFVçE÷G—R"À¢&WG'–&ÆS¢fÇ6RÀ¢GFV×EöçVÖ&W#¢À¢&WVW7EöGW&F–öåö×3¢Sc"À¢FWfVÆ÷W%öÖW76vS¢$W‡V7FVBÖ6†–æR×&VF&ÆRtTò&W7öç6Râ"À¢Ó°¢6öç7Bf–ÆVE'Vã¢FÖ–ävVçE'VâÒ°¢ââç'VâÀ¢&÷f–FW#¢&÷Væ’"À¢ÖöFVÅö–FVçF–f–W#¢&wBÓRãBÖÖ–æ’"À¢'VåöÖöFS¢&Æ—fR"À¢7FGW3¢&f–ÆVB"À¢GW&ç3¢À¢FööÇ3¢·°¢–C¢'FööÂ×6÷W&6RÖf–ÇW&R"À¢FööÅöæÖS¢'fÆ–FFUövVõö66W76–öç2"À¢7FGW3¢&f–ÆVB"À¢GW&F–öåö×3¢Sc"À¢ÕÒÀ¢FööÅö6ÆÇ3¢·°¢FööÅöæÖS¢'fÆ–FFUövVõö66W76–öç2"À¢&W7VÇC¢²6÷W&6UöF–væ÷7F–3¢6÷W&6TF–væ÷7F–2ÒÀ¢ÕÒÀ¢Ó°¢6öç7B6÷W&6TW'&÷#¢FÖ–åv÷&¶fÆ÷tW'&÷"Ò°¢ââçv÷&¶fÆ÷tW'&÷'5³ÒÀ¢6öFS¢'6÷W&6U÷VæW‡V7FVEö6öçFVçE÷G—R"À¢6FVv÷'“¢'6÷W&6U÷FööÂ"À¢&WG'–&ÆS¢fÇ6RÀ¢6fUöÖW76vS¢%66–VçF–f–26÷W&6R&WGW&æVBâVæW‡V7FVB6öçFVçBG—Râ"À¢FWF–Ã¢²6÷W&6UöF–væ÷7F–3¢6÷W&6TF–væ÷7F–2ÒÀ¢Ó°¢–ç7FÆÄfWF6„Öö6²‡°¢ââæFWF–Å&÷WFW2‚‚’Óâ'V–ÆB‚$d”ÄTB"ÂB’Â·6÷W&6TW'&÷%Ò’À¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒö'F–f7G6Ó¢²&öG“¢µÒÒÀ¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒö&÷fÇ6Ó¢²&öG“¢µÒÒÀ¢¶tUBö’öFÖ–âöVæGö–çBÖ'V–ÆG2òG¶'V–ÆD–GÒövVçB×'Vç6Ó¢²&öG“¢¶f–ÆVE'VåÒÒÀ¢¶tUBö’öFÖ–âövVçB×'Vç2òG·'Vä–GÖÓ¢²&öG“¢f–ÆVE'VâÒÀ¢Ò“°¢&VæFW$†öFÖ–âöVæGö–çG2òG¶'V–ÆD–GÖ“°¢W‡V7B†v—B67&VVâæf–æD'•FW‡B‚%66–VçF–f–2×6÷W&6RF–væ÷7F–72"’’çFô&T–åF†TFö7VÖVçB‚“°¢W‡V7B‡67&VVâævWDÆÄ'•FW‡B‚'wwrææ6&’ææÆÒææ–‚æv÷bövVò÷VW'’ö62æ6v’"’’çFô†fTÆVæwF‚ƒ"“°¢W‡V7B‡67&VVâævWDÆÄ'•FW‡B‚ôtUBâ£#ò’’çFô†fTÆVæwF‚ƒ"“°¢W‡V7B‡67&VVâævWDÆÄ'•FW‡B‚övVõÂ÷FW‡Bâ£S"'—FW2ò’’çFô†fTÆVæwF‚ƒ"“°¢W‡V7B‡67&VVâævWDÆÄ'•FW‡B‚'FW‡B÷Æ–â"’’çFô†fTÆVæwF‚ƒ"“°¢W‡V7B‡67&VVâævWDÆÄ'•FW‡B‚%V&Æ–2fÆ–B"’’çFô†fTÆVæwF‚ƒ"“°¢W‡V7B‡67&VVâævWDÆÄ'•FW‡B‚&'BÖvVò×6÷W&6R"’’çFô†fTÆVæwF‚ƒ"“°¢W‡V7B‡67&VVâævWDÆÄ'•FW‡B‚%6÷W&6Tf÷&ÖDW'&÷""’’çFô†fTÆVæwF‚ƒ"“°¢W‡V7B‡67&VVâævWDÆÄ'•FW‡B‚$W‡V7FVBÖ6†–æR×&VF&ÆRtTò&W7öç6Râ"’’çFô†fTÆVæwF‚ƒ"“°¢W‡V7B†Fö7VÖVçBæ&öG’’ææ÷BçFô†fUFW‡D6öçFVçB‚&FòÖæ÷B×7F÷&R"“°¢W‡V7B†Fö7VÖVçBæ&öG’’ææ÷BçFô†fUFW‡D6öçFVçB‚$WF†÷&—¦F–öâ"“°¢Ò“°§Ò“°