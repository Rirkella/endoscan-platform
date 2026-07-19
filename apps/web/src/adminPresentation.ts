import type { AdminBuild, AdminTimelineEvent } from "./api/types";

export const WORKFLOW_STAGES = [
  { label: "Definition", states: ["DRAFT", "COMPILING_TARGET_DATASET_SPECIFICATION", "AWAITING_DATASET_SPECIFICATION_REVIEW", "REVIEWING_DATASET_SPECIFICATION", "SPECIFYING_TARGET_DATASET", "AWAITING_DATASET_SPECIFICATION_APPROVAL", "AWAITING_DATASET_SPECIFICATION_REVISION"] },
  { label: "Discovery", states: ["DISCOVERING_DATA"] },
  { label: "Dataset review", states: ["AWAITING_DATASET_APPROVAL", "AWAITING_SEARCH_REVIEW"] },
  { label: "Curation", states: ["CURATING_DATA", "AWAITING_LABEL_APPROVAL"] },
  { label: "Identity", states: ["RESOLVING_IDENTITIES"] },
  { label: "Quality audit", states: ["AUDITING_DATASET", "AUDITING_LEAKAGE"] },
  { label: "Training", states: ["AWAITING_TRAINING_APPROVAL", "TRAINING"] },
  { label: "Evaluation", states: ["EVALUATING"] },
  { label: "Scientific review", states: ["AWAITING_SCIENTIFIC_APPROVAL"] },
  { label: "Registration", states: ["REGISTERING", "COMPLETED"] },
] as const;

const STATE_LABELS: Record<string, string> = {
  DRAFT: "Draft ready to start",
  COMPILING_TARGET_DATASET_SPECIFICATION: "Compiling target dataset specification",
  AWAITING_DATASET_SPECIFICATION_REVIEW: "Review target training dataset",
  REVIEWING_DATASET_SPECIFICATION: "Running optional specification review",
  SPECIFYING_TARGET_DATASET: "Preparing dataset specification",
  AWAITING_DATASET_SPECIFICATION_APPROVAL: "Waiting for specification review",
  AWAITING_DATASET_SPECIFICATION_REVISION: "Dataset specification needs revision",
  DISCOVERING_DATA: "Searching for candidate datasets",
  AWAITING_DATASET_APPROVAL: "Waiting for dataset review",
  AWAITING_SEARCH_REVIEW: "Waiting for revised-search review",
  CURATING_DATA: "Preparing the selected dataset",
  AWAITING_LABEL_APPROVAL: "Waiting for label review",
  RESOLVING_IDENTITIES: "Resolving compound identities",
  AUDITING_DATASET: "Checking dataset quality",
  AUDITING_LEAKAGE: "Checking train/test leakage",
  AWAITING_TRAINING_APPROVAL: "Waiting for training approval",
  TRAINING: "Training candidate models",
  EVALUATING: "Evaluating candidate models",
  AWAITING_SCIENTIFIC_APPROVAL: "Waiting for scientific review",
  REGISTERING: "Registering approved endpoint",
  COMPLETED: "Completed",
  PAUSED: "Paused",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
};

export function humanizeMachineValue(value: string): string {
  return value
    .toLowerCase()
    .split("_")
    .filter(Boolean)
    .map((part, index) => (index === 0 ? part.charAt(0).toUpperCase() + part.slice(1) : part))
    .join(" ");
}

export function stageLabel(state: string): string {
  return STATE_LABELS[state] ?? humanizeMachineValue(state);
}

export function effectiveStage(build: AdminBuild): string {
  if (build.current_stage === "PAUSED" && build.paused_from_state) return build.paused_from_state;
  if (build.current_stage === "FAILED" && build.failed_from_state) return build.failed_from_state;
  return build.current_stage;
}

export function buildStatusLabel(build: AdminBuild): string {
  if (["PAUSED", "FAILED", "CANCELLED", "COMPLETED"].includes(build.current_stage)) {
    return stageLabel(build.current_stage);
  }
  return stageLabel(build.current_stage);
}

export type BuildFilter = "all" | "review" | "running" | "paused" | "failed" | "completed";

export function buildFilter(build: AdminBuild): BuildFilter {
  if (build.pending_approval_id || build.status === "waiting") return "review";
  if (build.status === "paused") return "paused";
  if (build.status === "failed") return "failed";
  if (build.status === "completed") return "completed";
  return "running";
}

export function shortBuildId(id: string): string {
  const compact = id.replace(/^build-/, "").replace(/-/g, "");
  return compact.slice(0, 8).toUpperCase();
}

export function shortRunId(id: string): string {
  const compact = id.replace(/^run-/, "").replace(/-/g, "");
  return compact.slice(0, 8).toUpperCase();
}

export function relativeTime(value: string, now = Date.now()): string {
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "Unknown";
  const seconds = Math.max(0, Math.round((now - timestamp) / 1000));
  if (seconds < 45) return "Just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hr ago`;
  const days = Math.round(hours / 24);
  if (days < 14) return `${days} day${days === 1 ? "" : "s"} ago`;
  return new Date(value).toLocaleDateString();
}

export function stageProgress(build: AdminBuild) {
  const state = effectiveStage(build);
  let index = WORKFLOW_STAGES.findIndex((stage) => stage.states.some((item) => item === state));
  const unknown = index === -1;
  if (unknown) index = Math.min(WORKFLOW_STAGES.length - 1, Math.floor(build.progress / 10));
  const completed = build.current_stage === "COMPLETED" ? WORKFLOW_STAGES.length : index;
  return { index, completed, total: WORKFLOW_STAGES.length, unknown };
}

const EVENT_LABELS: Record<string, string> = {
  "workflow.created": "Endpoint draft created",
  "workflow.step.started": "Workflow stage started",
  "workflow.step.completed": "Workflow stage completed",
  "workflow.transitioned": "Workflow advanced",
  "workflow.resumed": "Workflow resumed",
  "workflow.retried": "Failed stage retried",
  "workflow.recovered_interruption": "Interrupted work recovered safely",
  "approval.created": "Dataset review requested",
  "approval.decided": "Reviewer decision recorded",
};

export function activityLabel(event: AdminTimelineEvent): string {
  if (event.event_type === "workflow.transitioned" && event.to_state) return stageLabel(event.to_state);
  if (event.event_type === "workflow.step.started" && event.to_state === "DISCOVERING_DATA") {
    return "Dataset discovery started";
  }
  if (event.event_type === "workflow.step.completed" && event.from_state === "DISCOVERING_DATA") {
    return "Candidate comparison prepared";
  }
  if (event.event_type === "approval.decided") {
    const decision = typeof event.payload.decision === "string" ? event.payload.decision : "reviewed";
    return `Reviewer ${humanizeMachineValue(decision).toLowerCase()} candidate`;
  }
  return EVENT_LABELS[event.event_type] ?? humanizeMachineValue(event.event_type.replace(/\./g, "_"));
}

export function actorLabel(actorType: string): string {
  if (actorType === "human") return "Local administrator";
  if (actorType === "agent") return "Agent";
  if (actorType === "tool") return "Workflow tool";
  return "Workflow service";
}

export function toolLabel(name: string): string {
  const labels: Record<string, string> = {
    inspect_endpoint_registry: "Inspected endpoint registry",
    list_known_source_adapters: "Listed available source adapters",
    summarize_existing_endpoint_pipeline: "Reviewed the existing endpoint pipeline",
    create_dataset_candidate_artifact: "Prepared candidate comparison artifact",
    search_geo_series: "Searched GEO Series",
    validate_geo_accessions: "Validated GEO accessions",
    inspect_geo_candidates: "Inspected candidate metadata and sample design",
    compare_dataset_candidates: "Compared candidate facts",
  };
  return labels[name] ?? humanizeMachineValue(name);
}
