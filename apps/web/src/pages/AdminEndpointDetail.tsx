import { KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, EndoscanApiError } from "../api/client";
import type {
  AdminAgentRun,
  AdminApproval,
  AdminArtifact,
  AdminBuild,
  AdminTimelineEvent,
  AdminTrainingDatasetWorkflow,
  AdminWorkflowError,
  AdminSourceToolDiagnostic,
} from "../api/types";
import {
  WORKFLOW_STAGES,
  activityLabel,
  actorLabel,
  buildStatusLabel,
  humanizeMachineValue,
  relativeTime,
  shortBuildId,
  shortRunId,
  stageLabel,
  stageProgress,
  toolLabel,
} from "../adminPresentation";
import { AdminModal } from "../components/AdminModal";

type Candidate = {
  candidate_id: string;
  accession?: string;
  title: string;
  source: string;
  recommendation?: string;
  recommendation_status?: string;
  limitations: string[];
  strengths?: string[];
  organism?: string[];
  sample_count?: number;
  controls_available?: boolean;
  data_type?: string;
  context?: string;
  biological_context?: string;
  treatment_control_evidence?: string;
  dose_time_evidence?: string;
  accession_verified?: boolean;
  geo_validation_status?: string | null;
  description?: string;
};

type CandidateArtifact = {
  candidates?: Candidate[];
  recommended_candidate_id?: string | null;
  run_mode?: "live" | "cached" | "replay";
  simulation_label?: string | null;
  live_discovery?: boolean;
  decision_summary?: string;
  unresolved_questions?: string[];
};

type Decision = "approve" | "reject" | "request_revision" | "choose_alternative";
type Confirmation = { kind: Decision | "cancel"; approval?: AdminApproval };

function records(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item))
    : [];
}

function textList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function CompiledSpecificationWorkspace({ data }: { data: AdminTrainingDatasetWorkflow }) {
  const draft = record(data.specification_draft);
  if (!Object.keys(draft).length) return null;
  const discoveryScope = record(draft.endpoint_discovery_scope ?? data.endpoint_discovery_scope);
  const isBroadScope = discoveryScope.mode === "broad_modality_exploration";
  const compilation = record(data.specification_compilation_outcome);
  const review = record(data.specification_review);
  const provenance = records(draft.field_provenance);
  const provenanceFor = (field: string) => provenance
    .filter((item) => item.field_name === field)
    .flatMap((item) => textList(item.origins))
    .map(humanizeMachineValue)
    .join(", ") || "Compiler contract";
  return <div className="admin-training-workspace" data-testid="compiled-specification-workspace">
    <section className="admin-panel">
      <div className="admin-panel-heading"><h2>{isBroadScope ? "Review endpoint discovery scope" : "Review target training dataset"}</h2><span>Compiler {String(compilation.compiler_version ?? "1.0.0")}</span></div>
      <p><strong>Draft produced deterministically from the request and approved platform contract.</strong></p>
      {isBroadScope && <p><strong>The system is not choosing a final endpoint yet. It will first compare the public evidence available for each candidate modality.</strong></p>}
      <p>Deterministic hash: <code>{String(compilation.deterministic_hash ?? "not available")}</code></p>
    </section>
    <section className="admin-panel">
      <div className="admin-panel-heading"><h2>Request interpretation</h2><span>Source-neutral</span></div>
      <dl className="admin-candidate-facts">
        <div><dt>Target</dt><dd>{String(draft.biological_target ?? "Unresolved")} <small>{provenanceFor("biological_target")}</small></dd></div>
        <div><dt>Discovery mode</dt><dd>{humanizeMachineValue(String(discoveryScope.mode ?? "fixed_modality"))}</dd></div>
        <div><dt>Fixed modality</dt><dd>{humanizeMachineValue(String(discoveryScope.fixed_modality ?? draft.endpoint_modality ?? "none"))} <small>{provenanceFor("endpoint_modality")}</small></dd></div>
        <div><dt>Candidate modalities</dt><dd>{textList(discoveryScope.candidate_modalities ?? draft.candidate_modalities).map(humanizeMachineValue).join(", ") || "Unresolved"}</dd></div>
        <div><dt>Preserve modalities separately</dt><dd>{discoveryScope.preserve_modalities_separately === true ? "Yes" : "No"}</dd></div>
        <div><dt>Later aggregation allowed</dt><dd>{discoveryScope.aggregation_allowed_later === true ? "Yes" : "No"}</dd></div>
        <div><dt>Human approval for aggregation</dt><dd>{discoveryScope.aggregation_requires_human_approval === true ? "Required" : "Not required"}</dd></div>
        <div><dt>Selection deferred until</dt><dd>{humanizeMachineValue(String(discoveryScope.selection_deferred_until ?? "not deferred"))}</dd></div>
        <div><dt>Prediction goal</dt><dd>{String(draft.intended_prediction_task ?? "Unresolved")}</dd></div>
      </dl>
    </section>
    <section className="admin-panel">
      <div className="admin-panel-heading"><h2>Target table</h2><span>{textList(draft.mandatory_target_table_fields).length} mandatory fields</span></div>
      <h3>Observation grain</h3><p>{String(draft.explicit_prediction_grain ?? draft.candidate_prediction_grain ?? "Unresolved")}</p>
      <h3>Mandatory fields</h3><ul>{textList(draft.mandatory_target_table_fields).map((item) => <li key={item}>{humanizeMachineValue(item)}</li>)}</ul>
      <h3>Identity and structures</h3><ul>{[...textList(draft.compound_identity_requirements), ...textList(draft.chemical_structure_requirements)].map((item) => <li key={item}>{item}</li>)}</ul>
      <h3>Transcriptomic response</h3><ul>{textList(draft.acceptable_transcriptomic_evidence_types).map((item) => <li key={item}>{item}</li>)}</ul>
      <h3>Activity evidence</h3><ul>{textList(draft.acceptable_activity_evidence_types).map((item) => <li key={item}>{humanizeMachineValue(item)}</li>)}</ul>
      <h3>Experimental contexts</h3><ul>{textList(draft.experimental_context_requirements).map((item) => <li key={item}>{item}</li>)}</ul>
    </section>
    <section className="admin-panel">
      <div className="admin-panel-heading"><h2>Scientific scope</h2><span>{humanizeMachineValue(String(discoveryScope.mode ?? draft.endpoint_modality ?? "unresolved"))}</span></div>
      <p>{String(draft.intended_scope_of_claim ?? draft.endpoint_definition ?? "Unresolved")}</p>
      <h3>Explicit exclusions</h3><ul>{textList(draft.explicit_exclusions).map((item) => <li key={item}>{item}</li>)}</ul>
      <h3>Limitations</h3><ul>{textList(compilation.limitations).map((item) => <li key={item}>{item}</li>)}</ul>
    </section>
    <section className="admin-panel">
      <div className="admin-panel-heading"><h2>Decisions required</h2><span>Human approval</span></div>
      <h3>Blocking questions</h3>{textList(draft.blocking_questions).length ? <ul>{textList(draft.blocking_questions).map((item) => <li key={item}>{item}</li>)}</ul> : <p>None.</p>}
      <h3>Approval questions</h3><ul>{textList(draft.approval_questions ?? draft.human_decisions_required).map((item) => <li key={item}>{item}</li>)}</ul>
      <h3>Assumptions</h3><ul>{textList(draft.assumptions).map((item) => <li key={item}>{item}</li>)}</ul>
    </section>
    <section className="admin-panel">
      <div className="admin-panel-heading"><h2>Optional AI review</h2><span>{humanizeMachineValue(String(review.status ?? "not_run"))}</span></div>
      <p>{String(review.safe_summary ?? "Optional AI review has not been requested.")}</p>
      <p>The deterministic draft remains authoritative if review is refused, invalid, or unavailable.</p>
    </section>
  </div>;
}

function TrainingDatasetWorkspace({ data, artifacts }: { data: AdminTrainingDatasetWorkflow; artifacts: AdminArtifact[] }) {
  if (data.legacy) {
    return <section className="admin-panel"><h2>Legacy single-source discovery</h2><p>Historical records remain unchanged and use the original dataset-selection workflow.</p></section>;
  }
  const specification = record(data.target_specification);
  const requirements = records(record(data.component_requirements).requirements);
  const plannedAgents = records(data.planned_discovery_agents);
  const specificationArtifact = [...artifacts].reverse().find((item) => item.artifact_type === "training_dataset_specification");
  const humanPolicyArtifact = [...artifacts].reverse().find((item) => item.artifact_type === "dataset_specification_human_policy");
  const sources = records(record(data.verified_source_inventory).sources);
  const discoveryReadiness = record(data.source_discovery_readiness);
  const adapterReadiness = record(discoveryReadiness.reviewed_adapters);
  const adapterInventory = records(discoveryReadiness.reviewed_adapter_inventory);
  const discoveryBudget = record(data.source_discovery_budget ?? discoveryReadiness.budget);
  const sourceObservations = records(data.source_observations);
  const sourceFragments = records(data.source_fragments);
  const cells = records(record(data.capability_matrix).cells);
  const fieldCells = records(record(data.capability_matrix).field_cells);
  const review = record(data.assembly_review);
  const strategies = records(review.strategies ?? record(data.assembly_strategies).strategies);
  const recommendedId = typeof review.recommended_strategy_id === "string" ? review.recommended_strategy_id : null;
  const recommended = strategies.find((item) => item.strategy_id === recommendedId) ?? strategies[0];
  const graph = record(recommended?.source_graph);
  const nodes = records(graph.nodes);
  const edges = records(graph.edges);
  const plan = record(recommended?.preparation_plan ?? data.preparation_plan);
  const gaps = records(record(data.gap_report).gaps);
  return (
    <div className="admin-training-workspace" data-testid="training-dataset-workspace">
      <CompiledSpecificationWorkspace data={data} />
      <section className="admin-panel"><div className="admin-panel-heading"><h2>Target training dataset</h2><span>{data.benchmark_mode ?? "standard"}</span></div>
        {Object.keys(specification).length ? <><dl className="admin-candidate-facts"><div><dt>Specification</dt><dd>{String(specification.specification_id)} · version {String(specification.contract_version ?? "1.0.0")}</dd></div><div><dt>Immutable hash</dt><dd><code>{specificationArtifact?.sha256 ?? "not available"}</code></dd></div><div><dt>Human policy</dt><dd>version {String(specification.approved_policy_version ?? "not applied")} · <code>{humanPolicyArtifact?.sha256 ?? "not available"}</code></dd></div><div><dt>Prediction task</dt><dd>{String(specification.intended_prediction_task ?? "Unresolved")}</dd></div><div><dt>Prediction unit</dt><dd>{String(specification.prediction_unit ?? "Unresolved")}</dd></div><div><dt>Activity representation</dt><dd>{textList(specification.acceptable_activity_representations).join(", ") || "Unresolved"}</dd></div><div><dt>Required fields</dt><dd>{textList(specification.mandatory_output_fields).join(", ") || "Awaiting specification"}</dd></div><div><dt>Nullable fields</dt><dd>{textList(specification.nullable_output_fields).join(", ") || "None"}</dd></div><div><dt>Optional fields</dt><dd>{textList(specification.optional_output_fields).join(", ") || "None"}</dd></div></dl><h3>Approved human policy decisions</h3><ul>{textList(specification.approved_policy_decisions).map((item) => <li key={item}>{item}</li>)}</ul></> : <p>Awaiting a strict, human-reviewed target specification.</p>}
      </section>
      <section className="admin-panel"><div className="admin-panel-heading"><h2>Planned source-discovery agents</h2><span>{plannedAgents.length} not started</span></div>{plannedAgents.map((item) => <article key={String(item.agent_name)}><h3>{String(item.agent_name)}</h3><p>{String(item.provider)} / {String(item.model)} · {String(item.maximum_turns)} turns · {String(item.maximum_tool_calls)} tools · {String(item.maximum_input_tokens)} input tokens · {String(item.maximum_output_tokens)} output tokens · ${String(item.maximum_cost_usd)} · {String(item.timeout_seconds)} s · {String(item.provider_retries)} retries</p><p>Tools: {textList(item.allowed_tools).join(", ") || "none"}</p><p>Official adapters: {textList(item.allowed_official_source_adapters).join(", ")}</p><p>Output: {String(item.output_schema_name)}</p></article>)}</section>
      <section className="admin-panel" data-testid="source-discovery-readiness"><div className="admin-panel-heading"><h2>Reviewed source adapters</h2><span>{discoveryReadiness.ready === true ? "Ready" : "Blocked"}</span></div>
        <p>{String(adapterReadiness.approved_adapter_count ?? 0)} approved adapters cover {textList(adapterReadiness.covered_roles).length} component roles. Source retries: {String(adapterReadiness.source_retries ?? 0)}.</p>
        {adapterInventory.map((adapter) => <article key={String(adapter.adapter_id)}><h3>{String(adapter.official_source_system)} · {String(adapter.adapter_id)}@{String(adapter.adapter_version)}</h3><p>Domains: {textList(adapter.allowlisted_domains).join(", ")}</p><p>Operations: {textList(adapter.approved_operations).join(", ")}</p><p>{String(adapter.request_timeout_seconds)} s · {String(adapter.maximum_response_bytes)} bytes · {String(adapter.requests_per_second)} req/s · {String(adapter.source_retry_count)} retries · cache {String(adapter.cache_ttl_seconds)} s</p></article>)}
        <h3>Controlled workflow budget</h3><p>{String(discoveryBudget.maximum_agent_runs ?? 4)} agent runs · {String(discoveryBudget.maximum_turns_per_agent ?? 6)} turns/agent · {String(discoveryBudget.maximum_total_tool_calls ?? 24)} total tools · ${String(discoveryBudget.maximum_total_cost_usd ?? 0.8)} total · {String(discoveryBudget.provider_retries ?? 0)} provider retries</p>
        {discoveryReadiness.ready !== true && <p>Execution remains fail-closed until provider, specification, requirements, stage, and reviewed adapters are ready.</p>}
      </section>
      <section className="admin-panel"><div className="admin-panel-heading"><h2>Component requirements</h2><span>{requirements.length}</span></div>{requirements.length ? <ul>{requirements.map((item) => <li key={String(item.requirement_id)}><strong>{humanizeMachineValue(String(item.role))}</strong> — {item.mandatory ? "required" : "optional"}</li>)}</ul> : <p>No requirements derived yet.</p>}</section>
      <section className="admin-panel"><div className="admin-panel-heading"><h2>Source observations</h2><span>{sourceObservations.length}</span></div>{sourceObservations.length ? sourceObservations.map((item, index) => { const observation = record(item.observation); return <article key={String(observation.observation_id ?? index)}><h3>{String(observation.source_system)} · {String(observation.stable_source_identifier)}</h3><p>{String(observation.adapter_id)}@{String(observation.adapter_version)} · {humanizeMachineValue(String(observation.public_validation_status))} · {humanizeMachineValue(String(observation.data_access_status))}</p><p>Artifact <code>{String(item.observation_artifact_hash ?? observation.response_artifact_hash)}</code></p><p>{textList(observation.limitations).join(" ") || "No recorded limitation."}</p></article>; }) : <p>No reviewed-adapter observations have been persisted.</p>}</section>
      <section className="admin-panel"><div className="admin-panel-heading"><h2>Agent source reviews</h2><span>{sourceFragments.length}</span></div>{sourceFragments.length ? sourceFragments.map((item, index) => { const fragment = record(item.fragment); return <article key={String(fragment.fragment_id ?? index)}><h3>{String(item.agent_name)}</h3><p>{humanizeMachineValue(String(fragment.agent_review_status ?? "unavailable"))} ({humanizeMachineValue(String(fragment.agent_terminal_outcome ?? "unavailable"))}) · {textList(fragment.observation_ids).length} authoritative observations</p><p>{textList(fragment.limitations).join(" ") || "No recorded limitation."}</p></article>; }) : <p>No bounded source-review agent has completed.</p>}</section>
      <section className="admin-panel" id="verified-source-inventory"><div className="admin-panel-heading"><h2>Verified source inventory</h2><span>{sources.length}</span></div>{sources.length ? sources.map((source) => <article key={String(source.source_id)}><h3>{String(source.source_system)} · {String(source.stable_accession)}</h3><p>Roles: {textList(source.source_roles).map(humanizeMachineValue).join(", ")}</p><p>Verified fields: {[...textList(source.measurement_fields), ...textList(source.identifier_fields), ...textList(source.structure_fields), ...textList(source.experimental_context_fields)].join(", ") || "None verified"}</p><p>Counts: {humanizeMachineValue(String(source.count_status ?? "not_computed"))} · Access: {humanizeMachineValue(String(source.access_status ?? "unresolved"))}</p><p>{textList(source.strengths).join(" ") || "No recorded strength."}</p><p>{textList(source.limitations).join(" ") || "No recorded limitation."}</p><p>Next: {textList(source.next_required_ingestion_actions).join(" ") || "Human source-inventory review."}</p></article>) : <p>No discovered or verified sources. Concrete strategies remain locked.</p>}</section>
      <section className="admin-panel"><div className="admin-panel-heading"><h2>Capability matrix</h2><span>{cells.length + fieldCells.length} cells</span></div>{cells.length || fieldCells.length ? <div className="admin-artifact-list">{[...cells, ...fieldCells].map((cell, index) => { const capability = String(cell.component ?? cell.field); return <article key={`${String(cell.source_id)}-${capability}-${index}`}><strong>{String(cell.source_id)}</strong><span>{humanizeMachineValue(capability)}: {humanizeMachineValue(String(cell.status))}</span></article>; })}</div> : <p>Awaiting deterministic inventory analysis.</p>}</section>
      <section className="admin-panel"><div className="admin-panel-heading"><h2>Strategies considered</h2><span>{strategies.length}</span></div>{strategies.length ? strategies.map((strategy) => <article key={String(strategy.strategy_id)}><h3>{strategy.strategy_id === recommendedId ? "Recommended: " : ""}{String(strategy.strategy_id)}</h3><p>{textList(strategy.scientific_risks).join(" ") || "No scientific risks recorded."}</p></article>) : <p>No assembly strategy exists before verified discovery.</p>}</section>
      <section className="admin-panel"><div className="admin-panel-heading"><h2>Recommended assembly graph</h2><span>{nodes.length} nodes</span></div>{nodes.length ? <><ul>{nodes.map((node) => <li key={String(node.node_id)}><strong>{String(node.label)}</strong> ({humanizeMachineValue(String(node.node_type))})</li>)}</ul><p>{edges.map((edge) => `${String(edge.from_node)} → ${String(edge.to_node)}`).join(" · ")}</p></> : <p>No source graph selected.</p>}</section>
      <section className="admin-panel"><div className="admin-panel-heading"><h2>Preparation plan</h2><span>{records(plan.steps).length} steps</span></div>{records(plan.steps).length ? <ol>{records(plan.steps).map((step) => <li key={String(step.step_id)}><strong>{String(step.action)}</strong> — {humanizeMachineValue(String(step.status))}</li>)}</ol> : <p>Preparation remains deferred until a verified strategy exists.</p>}</section>
      <section className="admin-panel"><div className="admin-panel-heading"><h2>Assembly gaps</h2><span>{gaps.length}</span></div>{gaps.length ? <ul>{gaps.map((gap) => <li key={String(gap.gap_id)}>{String(gap.description)}</li>)}</ul> : <p>No persisted gap report yet.</p>}</section>
    </div>
  );
}

function candidateStatus(candidate: Candidate, index: number): string {
  const value = (candidate.recommendation_status ?? candidate.recommendation ?? "").toLowerCase();
  if (value.includes("reject")) return "Rejected";
  if (value.includes("review")) return index === 0 ? "Recommended" : "Needs review";
  if (value.includes("prefer") || value.includes("recommend")) return "Recommended";
  if (value.includes("alternative")) return "Alternative";
  return "Needs review";
}

function candidateLabel(candidate: Candidate | undefined, index = 0): string {
  return candidate?.accession ?? (candidate?.candidate_id.startsWith("SIM-") ? candidate.candidate_id : `SIM-OS-${String(index + 1).padStart(3, "0")}`);
}

function modeLabel(mode: "live" | "cached" | "replay"): string {
  return mode === "live" ? "Live agent mode" : mode === "cached" ? "Cached mode" : "Replay mode";
}

function usageNumber(run: AdminAgentRun, name: string): number {
  const value = run.usage[name];
  return typeof value === "number" ? value : 0;
}

function configuredModeLabel(mode: "live" | "cached" | "replay"): string {
  return mode === "live" ? "Live" : mode === "cached" ? "Cached" : "Replay";
}

function runStatusLabel(
  run: AdminAgentRun,
  mode: "live" | "cached" | "replay",
  hasRecommendation: boolean,
): string {
  if (run.status === "failed") return mode === "live" ? "Live agent run failed" : "Agent run failed";
  if (run.status === "budget_exceeded" && mode === "live") return "Live agent run stopped at the configured cumulative token budget.";
  if (run.status === "completed") return "Completed";
  if (run.status === "approval_required") return hasRecommendation ? "Completed for review" : "No dataset recommendation";
  return humanizeMachineValue(run.status);
}

function traceEventCount(run: AdminAgentRun, eventType: string): number {
  return (run.trace?.events ?? []).filter((event) => event.event_type === eventType).length;
}

type GeoSearchSummary = {
  renderedQuery: string;
  resultCount: number;
  cacheStatus: string;
  strategyReason: string;
};

type TurnExposureSummary = {
  turn: number;
  substage: string;
  tools: string[];
};

function turnExposureSummaries(run: AdminAgentRun): TurnExposureSummary[] {
  return (run.trace?.events ?? []).flatMap((event) => {
    if (event.event_type !== "provider.turn.started") return [];
    const detail = event.detail;
    if (!detail || typeof detail !== "object" || Array.isArray(detail)) return [];
    const record = detail as Record<string, unknown>;
    const tools = Array.isArray(record.tools_exposed)
      ? record.tools_exposed.filter((item): item is string => typeof item === "string")
      : [];
    return [{
      turn: typeof record.turn === "number" ? record.turn : 0,
      substage: typeof record.discovery_substage === "string" ? record.discovery_substage : "unknown",
      tools,
    }];
  });
}

function candidateInspectionCounts(run: AdminAgentRun): { inspected: number; failed: number } | null {
  for (const call of run.tool_calls ?? []) {
    if (call.tool_name !== "inspect_geo_candidates") continue;
    const result = call.result;
    if (!result || typeof result !== "object" || Array.isArray(result)) continue;
    const output = (result as Record<string, unknown>).output;
    if (!output || typeof output !== "object" || Array.isArray(output)) continue;
    const record = output as Record<string, unknown>;
    return {
      inspected: typeof record.inspected_count === "number" ? record.inspected_count : 0,
      failed: typeof record.failed_count === "number" ? record.failed_count : 0,
    };
  }
  return null;
}

function geoSearchSummaries(run: AdminAgentRun): GeoSearchSummary[] {
  return (run.tool_calls ?? []).flatMap((call) => {
    if (call.tool_name !== "search_geo_series") return [];
    const result = call.result as Record<string, unknown> | undefined;
    const output = result?.output as Record<string, unknown> | undefined;
    if (!output || typeof output.rendered_query !== "string") return [];
    return [{
      renderedQuery: output.rendered_query,
      resultCount: typeof output.result_count === "number" ? output.result_count : 0,
      cacheStatus: typeof output.cache_status === "string" ? output.cache_status : "unknown",
      strategyReason: typeof output.strategy_reason === "string" ? output.strategy_reason : "Bounded GEO search",
    }];
  });
}

type ToolNormalizationSummary = {
  toolName: string;
  originalArguments: Record<string, unknown>;
  normalizedArguments: Record<string, unknown>;
  warningCodes: string[];
  warnings: Array<{
    code: string;
    field: string;
    original?: string | null;
    normalized?: string | null;
    policyVersion?: string | null;
  }>;
};

function toolNormalizationSummaries(run: AdminAgentRun): ToolNormalizationSummary[] {
  return (run.tool_calls ?? []).flatMap((call) => {
    const result = call.result as Record<string, unknown> | undefined;
    const originalArguments = result?.original_arguments;
    const normalizedArguments = result?.normalized_arguments;
    const warnings = result?.normalization_warnings;
    if (
      !originalArguments || typeof originalArguments !== "object" || Array.isArray(originalArguments)
      || !normalizedArguments || typeof normalizedArguments !== "object" || Array.isArray(normalizedArguments)
      || !Array.isArray(warnings) || warnings.length === 0
    ) return [];
    const parsedWarnings = warnings.flatMap((warning) => {
      if (!warning || typeof warning !== "object" || Array.isArray(warning)) return [];
      const record = warning as Record<string, unknown>;
      if (typeof record.code !== "string" || typeof record.field !== "string") return [];
      return [{
        code: record.code,
        field: record.field,
        original: typeof record.original === "string" ? record.original : null,
        normalized: typeof record.normalized === "string" ? record.normalized : null,
        policyVersion: typeof record.policy_version === "string" ? record.policy_version : null,
      }];
    });
    return [{
      toolName: typeof call.tool_name === "string" ? call.tool_name : "tool",
      originalArguments: originalArguments as Record<string, unknown>,
      normalizedArguments: normalizedArguments as Record<string, unknown>,
      warningCodes: parsedWarnings.map((warning) => warning.code),
      warnings: parsedWarnings,
    }];
  });
}

function safeDeveloperDiagnostic(run: AdminAgentRun): string | null {
  const events = run.trace?.events ?? [];
  for (const event of [...events].reverse()) {
    const detail = event.detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      const direct = (detail as Record<string, unknown>).developer_message;
      const structured = (detail as Record<string, unknown>).structured_output_diagnostic;
      const nested = (detail as Record<string, unknown>).source_diagnostic;
      const message = typeof direct === "string"
        ? direct
        : structured && typeof structured === "object" && !Array.isArray(structured)
          ? (structured as Record<string, unknown>).developer_message
        : nested && typeof nested === "object" && !Array.isArray(nested)
          ? (nested as Record<string, unknown>).developer_message
          : null;
      if (typeof message === "string" && message.length > 0) return message;
    }
  }
  return null;
}

function structuredOutputDiagnostic(run: AdminAgentRun): Record<string, unknown> | null {
  for (const event of [...(run.trace?.events ?? [])].reverse()) {
    const detail = event.detail;
    if (!detail || typeof detail !== "object" || Array.isArray(detail)) continue;
    const diagnostic = (detail as Record<string, unknown>).structured_output_diagnostic;
    if (diagnostic && typeof diagnostic === "object" && !Array.isArray(diagnostic)) {
      return diagnostic as Record<string, unknown>;
    }
  }
  return null;
}

function structuredOutputFingerprint(run: AdminAgentRun): Record<string, unknown> | null {
  for (const event of [...(run.trace?.events ?? [])].reverse()) {
    const detail = event.detail;
    if (!detail || typeof detail !== "object" || Array.isArray(detail)) continue;
    const direct = (detail as Record<string, unknown>).structured_output_request_fingerprint;
    if (direct && typeof direct === "object" && !Array.isArray(direct)) {
      return direct as Record<string, unknown>;
    }
    const diagnostic = (detail as Record<string, unknown>).structured_output_diagnostic;
    if (diagnostic && typeof diagnostic === "object" && !Array.isArray(diagnostic)) {
      const nested = (diagnostic as Record<string, unknown>).request_fingerprint;
      if (nested && typeof nested === "object" && !Array.isArray(nested)) {
        return nested as Record<string, unknown>;
      }
    }
  }
  return null;
}

function sourceDiagnostics(run: AdminAgentRun): AdminSourceToolDiagnostic[] {
  return (run.tool_calls ?? []).flatMap((call) => {
    const result = call.result;
    if (!result || typeof result !== "object" || Array.isArray(result)) return [];
    const diagnostic = (result as Record<string, unknown>).source_diagnostic;
    const output = (result as Record<string, unknown>).output;
    const candidateDiagnostics = output && typeof output === "object" && !Array.isArray(output)
      && Array.isArray((output as Record<string, unknown>).results)
      ? ((output as Record<string, unknown>).results as Array<Record<string, unknown>>).flatMap((item) => {
        const itemDiagnostic = item?.source_diagnostic;
        return itemDiagnostic && typeof itemDiagnostic === "object" && !Array.isArray(itemDiagnostic)
          ? [itemDiagnostic as AdminSourceToolDiagnostic]
          : [];
      })
      : [];
    return [
      ...(diagnostic && typeof diagnostic === "object" && !Array.isArray(diagnostic)
        ? [diagnostic as AdminSourceToolDiagnostic]
        : []),
      ...candidateDiagnostics,
    ];
  });
}

function SourceDiagnosticDetails({ diagnostic }: { diagnostic: AdminSourceToolDiagnostic }) {
  return <dl className="admin-source-diagnostic">
    <div><dt>Source</dt><dd>{diagnostic.source_host}{diagnostic.safe_url_path}</dd></div>
    <div><dt>HTTP</dt><dd>{diagnostic.http_method} · {diagnostic.http_status ?? "not received"}</dd></div>
    <div><dt>Final host</dt><dd>{diagnostic.final_approved_host ?? "not reached"}</dd></div>
    <div><dt>Source MIME</dt><dd>{diagnostic.content_type ?? "unknown"} · {diagnostic.response_byte_count ?? 0} bytes</dd></div>
    <div><dt>Artifact MIME</dt><dd>{diagnostic.artifact_content_type ?? "not stored"}</dd></div>
    <div><dt>Parser</dt><dd>{diagnostic.parser_outcome ? humanizeMachineValue(diagnostic.parser_outcome) : "not reached"}</dd></div>
    <div><dt>Artifact</dt><dd>{diagnostic.source_artifact_id ?? "not stored"}</dd></div>
    <div><dt>Cache</dt><dd>{diagnostic.cache_status ? humanizeMachineValue(diagnostic.cache_status) : "not available"}</dd></div>
    <div><dt>Category</dt><dd>{humanizeMachineValue(diagnostic.source_error_category)}</dd></div>
    <div><dt>Exception</dt><dd>{diagnostic.exception_class ?? "none"}</dd></div>
    <div><dt>Attempt</dt><dd>{diagnostic.attempt_number} · {diagnostic.request_duration_ms} ms</dd></div>
    <div><dt>Retry policy</dt><dd>{diagnostic.retryable ? "Retryable" : "Terminal"}</dd></div>
    {diagnostic.developer_message && <div><dt>Developer note</dt><dd>{diagnostic.developer_message}</dd></div>}
  </dl>;
}

export function AdminEndpointDetail() {
  const { buildId = "" } = useParams();
  const confirmationTrigger = useRef<HTMLElement | null>(null);
  const [build, setBuild] = useState<AdminBuild | null>(null);
  const [events, setEvents] = useState<AdminTimelineEvent[]>([]);
  const [artifacts, setArtifacts] = useState<AdminArtifact[]>([]);
  const [approvals, setApprovals] = useState<AdminApproval[]>([]);
  const [runs, setRuns] = useState<AdminAgentRun[]>([]);
  const [trace, setTrace] = useState<AdminAgentRun | null>(null);
  const [errors, setErrors] = useState<AdminWorkflowError[]>([]);
  const [trainingWorkflow, setTrainingWorkflow] = useState<AdminTrainingDatasetWorkflow | null>(null);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [recommendedCandidateId, setRecommendedCandidateId] = useState<string | null | undefined>(undefined);
  const [runMode, setRunMode] = useState<"live" | "cached" | "replay">("replay");
  const [simulationLabel, setSimulationLabel] = useState<string | null>(null);
  const [decisionSummary, setDecisionSummary] = useState("");
  const [unresolvedQuestions, setUnresolvedQuestions] = useState<string[]>([]);
  const [comment, setComment] = useState("");
  const [selected, setSelected] = useState("");
  const [activityTab, setActivityTab] = useState<"activity" | "audit">("activity");
  const [fullActivity, setFullActivity] = useState(false);
  const [showTrace, setShowTrace] = useState(false);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copiedIdentifier, setCopiedIdentifier] = useState<"build" | "run" | null>(null);

  const load = useCallback(async () => {
    try {
      const [
        nextBuild,
        nextEvents,
        nextArtifacts,
        nextApprovals,
        nextRuns,
        nextErrors,
        nextTrainingWorkflow,
        nextCapabilities,
      ] =
        await Promise.all([
          api.adminGetBuild(buildId),
          api.adminTimeline(buildId),
          api.adminArtifacts(buildId),
          api.adminApprovals(buildId),
          api.adminAgentRuns(buildId),
          api.adminErrors(buildId),
          api.adminTrainingDatasetWorkflow(buildId).catch(() => ({
            schema_version: "1.0.0" as const,
            workflow_id: buildId,
            workflow_kind: "legacy_single_source_discovery",
            legacy: true,
            label: "Legacy single-source discovery",
          })),
          api.adminCapabilities().catch(() => null),
        ]);
      setBuild(nextBuild);
      setEvents(nextEvents);
      setArtifacts(nextArtifacts);
      setApprovals(nextApprovals);
      setRuns(nextRuns);
      setErrors(nextErrors);
      setTrainingWorkflow(nextTrainingWorkflow);
      const persistedRunMode = nextRuns.at(-1)?.run_mode ?? undefined;
      const candidateArtifact = [...nextArtifacts]
        .reverse()
        .find((item) => item.artifact_type === "dataset_candidates");
      if (candidateArtifact) {
        const preview = await api.adminArtifactPreview(candidateArtifact.id);
        const content = preview.content as CandidateArtifact;
        setCandidates(content.candidates ?? []);
        setRecommendedCandidateId(content.recommended_candidate_id);
        setSelected((current) => current || content.candidates?.[0]?.candidate_id || "");
        setRunMode(persistedRunMode ?? content.run_mode ?? nextCapabilities?.run_mode ?? "replay");
        setSimulationLabel(content.simulation_label ?? null);
        setDecisionSummary(content.decision_summary ?? "");
        setUnresolvedQuestions(content.unresolved_questions ?? []);
      } else {
        setCandidates([]);
        setRecommendedCandidateId(undefined);
        setRunMode(persistedRunMode ?? nextCapabilities?.run_mode ?? "replay");
        setSimulationLabel(null);
        setDecisionSummary("");
        setUnresolvedQuestions([]);
      }
      if (nextRuns.length > 0) setTrace(await api.adminAgentRun(nextRuns.at(-1)!.id));
      else setTrace(null);
      setError(null);
    } catch {
      setError("Build details could not be loaded. Check the local service and try again.");
    }
  }, [buildId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function command(action: "start" | "pause" | "resume" | "cancel" | "retry" | "simulate-failure" | "retry-dataset-specification" | "run-dataset-specification-review" | "revise-endpoint-request") {
    if (!build) return;
    setBusy(true);
    try {
      await api.adminCommand(build.id, action, build.version);
      await load();
      setConfirmation(null);
    } catch (reason) {
      setError(reason instanceof EndoscanApiError ? reason.detail : `Unable to ${action}.`);
    } finally {
      setBusy(false);
    }
  }

  async function decide(approval: AdminApproval, decision: Decision) {
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
      setConfirmation(null);
      await load();
    } catch (reason) {
      setError(reason instanceof EndoscanApiError ? reason.detail : "The review decision could not be recorded.");
    } finally {
      setBusy(false);
    }
  }

  async function refreshSourceMetadata() {
    if (!build) return;
    setBusy(true);
    try {
      await api.adminRefreshSourceMetadata(build.id, build.version);
      await load();
    } catch (reason) {
      setError(reason instanceof EndoscanApiError ? reason.detail : "Source metadata could not be refreshed.");
    } finally {
      setBusy(false);
    }
  }

  async function continueTrainingDataset() {
    if (!build) return;
    setBusy(true);
    try {
      await api.adminContinueTrainingDataset(build.id, build.version);
      await load();
    } catch (reason) {
      setError(
        reason instanceof EndoscanApiError
          ? reason.detail
          : "The active training-dataset stage could not be continued.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function authorizeSourceDiscovery() {
    if (!build) return;
    setBusy(true);
    try {
      const authorized = await api.adminAuthorizeSourceDiscovery(build.id, build.version);
      await api.adminContinueTrainingDataset(build.id, authorized.version);
      await load();
    } catch (reason) {
      setError(
        reason instanceof EndoscanApiError
          ? reason.detail
          : "Reviewed source discovery could not be authorized.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function copyIdentifier(kind: "build" | "run", value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopiedIdentifier(kind);
    } catch {
      setError(`Unable to copy the full ${kind} ID.`);
    }
  }

  const pending = approvals.find((item) => item.status === "pending") ?? null;
  const recommended = useMemo(
    () => recommendedCandidateId === null
      ? undefined
      : recommendedCandidateId
        ? candidates.find((item) => item.candidate_id === recommendedCandidateId)
        : candidates.find((item, index) => candidateStatus(item, index) === "Recommended") ?? candidates[0],
    [candidates, recommendedCandidateId],
  );
  const agentTools = useMemo(
    () => trace?.tools ?? [],
    [trace],
  );

  if (!build) {
    return <section className="admin-page"><div className="admin-empty" role="status">{error ?? "Loading build..."}</div></section>;
  }

  const progress = stageProgress(build);
  const displayEvents = fullActivity ? [...events].reverse() : [...events].reverse().slice(0, 6);
  const canFail = !["DRAFT", "PAUSED", "FAILED", "CANCELLED", "COMPLETED", "REGISTERING", "AWAITING_DATASET_SPECIFICATION_REVISION"].includes(build.current_stage);
  const canPause = !["DRAFT", "PAUSED", "FAILED", "CANCELLED", "COMPLETED", "REGISTERING"].includes(build.current_stage);
  const canCancel = !["CANCELLED", "COMPLETED"].includes(build.current_stage);
  const canRetry = build.current_stage === "FAILED" && errors.at(-1)?.retryable === true;
  const sourceDiscoveryReady = record(trainingWorkflow?.source_discovery_readiness).ready === true;
  const requirementsReady = artifacts.some((item) => item.artifact_type === "component_requirements");
  const selectedIndex = Math.max(0, candidates.findIndex((candidate) => candidate.candidate_id === selected));
  const selectedCandidate = candidates[selectedIndex];
  const developerDiagnostic = trace ? safeDeveloperDiagnostic(trace) : null;
  const outputDiagnostic = trace ? structuredOutputDiagnostic(trace) : null;
  const outputFingerprint = trace ? structuredOutputFingerprint(trace) : null;
  const scientificSourceDiagnostics = trace ? sourceDiagnostics(trace) : [];
  const providerRetries = trace ? traceEventCount(trace, "provider.retry") : 0;
  const geoSearches = trace ? geoSearchSummaries(trace) : [];
  const turnExposures = trace ? turnExposureSummaries(trace) : [];
  const inspectionCounts = trace ? candidateInspectionCounts(trace) : null;
  const normalizationSummaries = trace ? toolNormalizationSummaries(trace) : [];
  const normalizationWarningCount = normalizationSummaries.reduce(
    (count, summary) => count + summary.warningCodes.length,
    0,
  );
  const emptyOptionalFilterWarningCount = normalizationSummaries.reduce(
    (count, summary) => count + summary.warningCodes.filter(
      (code) => code === "empty_optional_search_term_removed",
    ).length,
    0,
  );
  const controlledVocabularyWarningCount = normalizationSummaries.reduce(
    (count, summary) => count + summary.warningCodes.filter(
      (code) => code === "controlled_vocabulary_alias_canonicalized",
    ).length,
    0,
  );
  const hasCompletedOutput = trace?.status === "completed" || trace?.status === "approval_required";
  const hasCandidateRecommendation = Boolean(pending && recommended && candidates.length > 0);
  const isAssemblyApproval = pending?.approval_type === "training_dataset_assembly_strategy";
  const isSpecificationApproval = pending?.approval_type === "dataset_specification";
  const activeDiscoveryScope = record(
    trainingWorkflow?.endpoint_discovery_scope
      ?? record(trainingWorkflow?.specification_draft).endpoint_discovery_scope,
  );
  const isBroadDiscoveryScope = activeDiscoveryScope.mode === "broad_modality_exploration";
  const isSpecificationRevision = build.current_stage === "AWAITING_DATASET_SPECIFICATION_REVISION";
  const specificationOutcome = record(trainingWorkflow?.specification_agent_outcome);
  const specificationSemanticValidation = record(
    trainingWorkflow?.specification_semantic_validation,
  );
  const specificationOutcomeStatus = typeof specificationOutcome.status === "string"
    ? specificationOutcome.status
    : "unknown_model_behavior";
  const specificationFailureCategory = typeof specificationOutcome?.failure_category === "string"
    ? specificationOutcome.failure_category
    : "unknown_model_behavior";
  const hasSpecificationSemanticViolation =
    specificationSemanticValidation.status === "semantic_contract_violation";
  const specificationRevisionTitle = hasSpecificationSemanticViolation
    ? "Dataset specification policy needs revision"
    : specificationOutcomeStatus === "insufficient_endpoint_definition"
      ? "Endpoint definition needs clarification"
      : specificationOutcomeStatus === "model_refused"
        ? "Dataset specification request was refused"
        : "Dataset specification needs revision";
  const specificationRevisionExplanation = hasSpecificationSemanticViolation
    ? "The endpoint already contains an explicit target and modality, but the agent treated non-blocking dataset-policy choices as blocking."
    : specificationOutcomeStatus === "insufficient_endpoint_definition"
      ? "The agent produced a valid structured response but could not create a dataset-specification draft from the endpoint definition."
      : specificationOutcomeStatus === "model_refused"
        ? "The provider returned a valid refusal outcome. No source discovery was started."
        : "The agent response did not match the required structured contract. No source discovery was started.";
  const specificationQuestions = [
    ...textList(specificationOutcome.blocking_questions),
    ...textList(specificationOutcome.approval_questions),
    ...textList(specificationOutcome.unresolved_questions),
  ].filter((item, index, all) => all.indexOf(item) === index);
  const specificationLimitations = textList(specificationOutcome.limitations);

  function requestConfirmation(kind: Confirmation["kind"], approval?: AdminApproval, trigger?: HTMLElement) {
    confirmationTrigger.current = trigger ?? null;
    setConfirmation({ kind, approval });
  }

  function handleTabKey(event: KeyboardEvent<HTMLButtonElement>) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const order: Array<"activity" | "audit"> = ["activity", "audit"];
    let index = order.indexOf(activityTab);
    if (event.key === "Home") index = 0;
    else if (event.key === "End") index = order.length - 1;
    else index = (index + (event.key === "ArrowRight" ? 1 : -1) + order.length) % order.length;
    setActivityTab(order[index]);
    requestAnimationFrame(() => document.getElementById(`${order[index]}-tab`)?.focus());
  }

  return (
    <section className="admin-page admin-detail-page" aria-labelledby="admin-build-title">
      <Link className="admin-back" to="/admin/endpoints">Back to endpoint builds</Link>
      <header className="admin-heading admin-detail-heading">
        <div>
          <div className="admin-title-meta">
            <span className="admin-identifier">
              <span>Build {shortBuildId(build.id)}</span>
              <button
                type="button"
                className="admin-copy-button"
                aria-label={`Copy full build ID ${build.id}`}
                onClick={() => void copyIdentifier("build", build.id)}
              >{copiedIdentifier === "build" ? "Copied" : "Copy"}</button>
            </span>
            <span>Updated {relativeTime(build.updated_at)}</span>
          </div>
          <h1 id="admin-build-title">{build.endpoint_name}</h1>
          <p>{build.biological_goal}</p>
        </div>
        <div className="admin-build-status-summary">
          <span className="admin-status admin-status-detail">{buildStatusLabel(build)}</span>
          <strong>{stageLabel(build.current_stage)}</strong>
          <span>Local administrator</span>
          <span className="admin-mode-badge">{modeLabel(runMode)}</span>
        </div>
      </header>

      {error && <div className="admin-error" role="alert">{error}</div>}

      <section className="admin-stepper-panel" aria-labelledby="workflow-progress-title">
        <div className="admin-panel-heading">
          <div><h2 id="workflow-progress-title">Workflow progress</h2><p>{progress.completed} of {progress.total} stages completed</p></div>
          <span aria-live="polite">Current: {stageLabel(build.current_stage)}</span>
        </div>
        <ol className="admin-stepper" aria-label="Endpoint build stages">
          {WORKFLOW_STAGES.map((stage, index) => {
            const state = index < progress.index ? "completed" : index === progress.index ? (build.current_stage === "FAILED" ? "failed" : "current") : "waiting";
            return (
              <li key={stage.label} className={`admin-step admin-step-${state}`} aria-current={state === "current" ? "step" : undefined}>
                <span className="admin-step-marker" aria-hidden>{state === "completed" ? "Done" : index + 1}</span>
                <span><strong>{stage.label}</strong><small>{state}</small></span>
              </li>
            );
          })}
          {progress.unknown && (
            <li className="admin-step admin-step-current" aria-current="step">
              <span className="admin-step-marker" aria-hidden>+</span>
              <span><strong>{stageLabel(build.current_stage)}</strong><small>future stage</small></span>
            </li>
          )}
        </ol>
      </section>

      <div className="admin-detail-grid">
        <main className="admin-primary-column">
          {trainingWorkflow && !isSpecificationRevision && <TrainingDatasetWorkspace data={trainingWorkflow} artifacts={artifacts} />}
          <section className="admin-panel admin-decision-panel" aria-labelledby="decision-title">
            <div className="admin-decision-heading">
              <div>
                <span className="admin-section-kicker">Human decision</span>
                <h2 id="decision-title">{isSpecificationRevision ? specificationRevisionTitle : isAssemblyApproval ? "Assembly strategy review required" : isSpecificationApproval ? (isBroadDiscoveryScope ? "Review endpoint discovery scope" : "Review target training dataset") : pending ? (hasCandidateRecommendation ? "Dataset review required" : "Search review required") : "No review required"}</h2>
                <p>{isSpecificationRevision ? specificationRevisionExplanation : isAssemblyApproval ? "Approve only the immutable verified source graph and deterministic preparation plan; training remains deferred." : isSpecificationApproval ? (isBroadDiscoveryScope ? "The system is not choosing a final endpoint yet. It will first compare the public evidence available for each candidate modality." : "The draft was produced deterministically from the request and approved platform contract; no source discovery has started.") : pending ? (hasCandidateRecommendation ? "Review the bounded recommendation before any data curation can begin." : "No dataset was recommended; review the bounded search limitations before requesting a revision.") : "This workflow is not currently waiting for a reviewer."}</p>
              </div>
              {pending && <span className="admin-review-flag">Action required</span>}
            </div>
            {isSpecificationRevision ? (
              <div className="admin-approval-card admin-no-candidate-review">
                <div className="admin-recommendation">
                  <span>Dataset specification review gate</span>
                  <h3>{String(specificationOutcome.decision_summary ?? "No valid dataset specification was produced")}</h3>
                  <p>Outcome status: {humanizeMachineValue(specificationOutcomeStatus)}</p>
                  {specificationOutcomeStatus === "invalid_model_output" && (
                    <p>Failure category: {humanizeMachineValue(specificationFailureCategory)}</p>
                  )}
                  {specificationQuestions.length > 0 && <><h4>Questions</h4><ul>{specificationQuestions.map((item) => <li key={item}>{item}</li>)}</ul></>}
                  {specificationLimitations.length > 0 && <><h4>Limitations</h4><ul>{specificationLimitations.map((item) => <li key={item}>{item}</li>)}</ul></>}
                </div>
                <div className="admin-approval-actions">
                  <button className="admin-secondary" disabled={busy} onClick={() => void command("revise-endpoint-request")}>Revise endpoint request</button>
                  <button className="admin-primary" disabled={busy} onClick={() => void command("retry-dataset-specification")}>Retry specification</button>
                  <button className="admin-secondary" disabled title="Configure an alternate planner model before retrying">Use another planner model</button>
                  <button className="admin-danger-outline" disabled={busy} onClick={(event) => requestConfirmation("cancel", undefined, event.currentTarget)}>Cancel build</button>
                </div>
              </div>
            ) : pending && (isAssemblyApproval || isSpecificationApproval) ? (
              <div className="admin-approval-card">
                <div className="admin-recommendation"><span>{isAssemblyApproval ? "Verified assembly proposal" : "Target contract"}</span><h3>{pending.request.proposed_decision}</h3><p>{pending.request.evidence_summary}</p></div>
                <div className="admin-decision-evidence"><div><h3>Approval scope</h3><p>{pending.request.requested_action}</p></div><div><h3>Limitations</h3><ul>{pending.request.limitations.map((item) => <li key={item}>{item}</li>)}</ul></div></div>
                <label className="admin-comment-field">Reviewer comment<textarea value={comment} onChange={(event) => setComment(event.target.value)} placeholder="Required for rejection or revision" /></label>
                <div className="admin-approval-actions">
                  <button className="admin-primary" disabled={busy} onClick={(event) => requestConfirmation("approve", pending, event.currentTarget)}>{isAssemblyApproval ? "Approve assembly strategy" : "Approve target specification"}</button>
                  {isSpecificationApproval && <button className="admin-secondary" disabled={busy} onClick={() => void command("run-dataset-specification-review")}>Run optional AI review</button>}
                  {isSpecificationApproval && <button className="admin-secondary" disabled={busy} title="Record the requested policy-choice edits in the revision comment" onClick={(event) => requestConfirmation("request_revision", pending, event.currentTarget)}>Edit approval choices</button>}
                  <button className="admin-secondary" disabled={busy || !comment} onClick={(event) => requestConfirmation("request_revision", pending, event.currentTarget)}>{isSpecificationApproval ? "Request revision" : "Request revised strategy"}</button>
                  <button className="admin-danger-outline" disabled={busy || !comment} onClick={(event) => requestConfirmation("reject", pending, event.currentTarget)}>Reject</button>
                </div>
              </div>
            ) : pending && hasCandidateRecommendation ? (
              <div className="admin-approval-card">
                <div className="admin-recommendation">
                  <span>Recommended candidate</span>
                  <strong>{recommended ? candidateLabel(recommended, Math.max(0, candidates.indexOf(recommended))) : "No candidate"}</strong>
                  <h3>{recommended?.title ?? pending.request.proposed_decision}</h3>
                  <p>{recommended?.description ?? recommended?.biological_context ?? pending.request.evidence_summary}</p>
                </div>
                <div className="admin-decision-evidence">
                  <div>
                    <h3>Why it is recommended</h3>
                    <ul>{[...(recommended?.strengths ?? []), pending.request.agent_recommendation].map((item) => <li key={item}>{item}</li>)}</ul>
                  </div>
                  <div>
                    <h3>Limitations</h3>
                    <ul>{[...(recommended?.limitations ?? []), ...pending.request.limitations].filter((item, index, all) => all.indexOf(item) === index).map((item) => <li key={item}>{item}</li>)}</ul>
                  </div>
                </div>
                <label className="admin-comment-field">
                  Reviewer comment
                  <textarea value={comment} onChange={(event) => setComment(event.target.value)} placeholder="Required for rejection, revision, or selecting another candidate" />
                </label>
                <div className="admin-approval-actions">
                  <button className="admin-primary" disabled={busy} onClick={(event) => requestConfirmation("approve", pending, event.currentTarget)}>Approve dataset</button>
                  <button className="admin-secondary" disabled={busy || !comment} onClick={(event) => requestConfirmation("request_revision", pending, event.currentTarget)}>Request revision</button>
                  <button className="admin-danger-outline" disabled={busy || !comment} onClick={(event) => requestConfirmation("reject", pending, event.currentTarget)}>Reject</button>
                  {candidates.length > 1 && <button className="admin-link-button" disabled={busy || !selected || !comment} onClick={(event) => requestConfirmation("choose_alternative", pending, event.currentTarget)}>Select another candidate</button>}
                </div>
              </div>
            ) : pending ? (
              <div className="admin-approval-card admin-no-candidate-review">
                <div className="admin-recommendation">
                  <span>No dataset recommendation</span>
                  <h3>Bounded GEO searches found no suitable candidate</h3>
                  <p>{pending.request.evidence_summary}</p>
                </div>
                <div className="admin-decision-evidence">
                  <div><h3>Limitations</h3><ul>{pending.request.limitations.map((item) => <li key={item}>{item}</li>)}</ul></div>
                  <div><h3>Proposed next search</h3><p>{pending.request.agent_recommendation}</p></div>
                </div>
                <label className="admin-comment-field">Reviewer comment<textarea value={comment} onChange={(event) => setComment(event.target.value)} placeholder="Required to request a revised search" /></label>
                <div className="admin-approval-actions">
                  <button className="admin-secondary" disabled={busy || !comment} onClick={(event) => requestConfirmation("request_revision", pending, event.currentTarget)}>Request revised search</button>
                  <button className="admin-danger-outline" disabled={busy} onClick={(event) => requestConfirmation("cancel", undefined, event.currentTarget)}>Cancel workflow</button>
                </div>
              </div>
            ) : !isSpecificationRevision && hasCompletedOutput && candidates.length === 0 ? (
              <div className="admin-approval-card admin-no-candidate-review">
                <div className="admin-recommendation">
                  <span>No dataset recommendation</span>
                  <h3>Bounded GEO searches found no suitable candidate</h3>
                  <p>{decisionSummary || "The bounded discovery completed without a public_valid recommendation."}</p>
                </div>
              </div>
            ) : <div className="admin-empty">The next workflow action is shown below.</div>}
          </section>

          {candidates.length > 0 && (
            <section className="admin-panel" aria-labelledby="candidate-comparison-title">
              <div className="admin-panel-heading">
                <div><h2 id="candidate-comparison-title">Candidate comparison</h2><p>{runMode === "replay" ? simulationLabel ?? "Prepared replay fixture; not live scientific discovery." : "Official-source metadata prepared for human scientific review."}</p></div>
                <span>{candidates.length} candidates</span>
              </div>
              <div className="admin-candidate-grid">
                {candidates.map((candidate, index) => {
                  const status = candidateStatus(candidate, index);
                  return (
                    <article className={`admin-candidate-card ${selected === candidate.candidate_id ? "candidate-selected" : ""}`} key={candidate.candidate_id}>
                      <div className="admin-card-title-row">
                        <span className={`admin-candidate-status admin-candidate-${status.toLowerCase().replace(/ /g, "-")}`}>{status}</span>
                        <span className="admin-fixture-label">{runMode === "replay" ? "Prepared replay fixture" : candidate.accession_verified ? "Official accession verified" : "Verification required"}</span>
                      </div>
                      <h3>{candidateLabel(candidate, index)}</h3>
                      <p>{candidate.title}</p>
                      <dl className="admin-candidate-facts">
                        <div><dt>Source</dt><dd>{candidate.source.startsWith("https://") ? <a href={candidate.source} target="_blank" rel="noreferrer">NCBI GEO</a> : candidate.source}</dd></div>
                        <div><dt>Organism</dt><dd>{candidate.organism?.join(", ") || "Not specified"}</dd></div>
                        <div><dt>Samples</dt><dd>{candidate.sample_count ?? "Not supplied"}</dd></div>
                        <div><dt>Controls</dt><dd>{candidate.treatment_control_evidence ?? (candidate.controls_available == null ? "Not verified" : candidate.controls_available ? "Available" : "Unavailable")}</dd></div>
                        <div><dt>Data type</dt><dd>{candidate.data_type ?? "Not specified"}</dd></div>
                        <div><dt>Context</dt><dd>{candidate.biological_context ?? candidate.context ?? "Not specified"}</dd></div>
                        <div><dt>Dose / time</dt><dd>{candidate.dose_time_evidence ?? "Not verified"}</dd></div>
                      </dl>
                      <h4>Cautions</h4>
                      <ul>{candidate.limitations.map((item) => <li key={item}>{item}</li>)}</ul>
                      <label className="admin-candidate-choice">
                        <input type="radio" name="candidate" checked={selected === candidate.candidate_id} onChange={() => setSelected(candidate.candidate_id)} />
                        Select {candidateLabel(candidate, index)}
                      </label>
                    </article>
                  );
                })}
              </div>
            </section>
          )}

          <section className="admin-panel" aria-labelledby="workflow-actions-title">
            <div className="admin-panel-heading"><h2 id="workflow-actions-title">Workflow actions</h2><span>Only valid actions are shown</span></div>
            <div className="admin-actions" aria-label="Workflow controls">
              {build.current_stage === "DRAFT" && <button disabled={busy} onClick={() => void command("start")}>Start workflow</button>}
              {build.workflow_kind === "training_dataset_discovery" &&
                ["COMPILING_TARGET_DATASET_SPECIFICATION", "SPECIFYING_TARGET_DATASET", "DERIVING_COMPONENT_REQUIREMENTS"].includes(
                  build.current_stage,
                ) && (
                  <button
                    disabled={busy || (build.current_stage === "DERIVING_COMPONENT_REQUIREMENTS" && requirementsReady && !sourceDiscoveryReady)}
                    title={build.current_stage === "DERIVING_COMPONENT_REQUIREMENTS" && requirementsReady && !sourceDiscoveryReady ? "Provider, approved adapters, specification, requirements, and stage must all be ready." : undefined}
                    onClick={() => void (build.current_stage === "DERIVING_COMPONENT_REQUIREMENTS" && requirementsReady ? authorizeSourceDiscovery() : continueTrainingDataset())}
                  >
                    {build.current_stage === "DERIVING_COMPONENT_REQUIREMENTS" && requirementsReady ? "Authorize reviewed source discovery" : build.current_stage === "DERIVING_COMPONENT_REQUIREMENTS" ? "Derive component requirements" : "Continue active stage"}
                  </button>
                )}
              {build.workflow_kind === "training_dataset_discovery" && ["DISCOVERING_ACTIVITY_EVIDENCE", "DISCOVERING_TRANSCRIPTOMIC_EVIDENCE", "DISCOVERING_IDENTITY_AND_STRUCTURE_SOURCES", "DISCOVERING_SUPPORTING_METADATA", "VALIDATING_DISCOVERED_SOURCES"].includes(build.current_stage) && <button disabled={busy} onClick={() => void continueTrainingDataset()}>Resume authorized source discovery</button>}
              {build.current_stage === "AWAITING_SOURCE_INVENTORY_REVIEW" && <button disabled={busy} onClick={() => document.getElementById("verified-source-inventory")?.scrollIntoView({ behavior: "smooth" })}>Review source inventory</button>}
              {build.current_stage === "AWAITING_SOURCE_INVENTORY_REVIEW" && <button className="admin-secondary" disabled title="Gap-directed discovery is intentionally deferred in this phase">Request later targeted discovery</button>}
              {canPause && <button disabled={busy} onClick={() => void command("pause")}>Pause</button>}
              {build.current_stage === "PAUSED" && <button disabled={busy} onClick={() => void command("resume")}>Resume</button>}
              {isSpecificationRevision && <>
                <button disabled={busy} onClick={() => void command("revise-endpoint-request")}>Revise endpoint request</button>
                <button disabled={busy} onClick={() => void command("retry-dataset-specification")}>Compile revised specification</button>
              </>}
              {canRetry && <button disabled={busy} onClick={() => void command("retry")}>Retry failed step</button>}
              {canCancel && <button className="admin-danger-outline" disabled={busy} onClick={(event) => requestConfirmation("cancel", undefined, event.currentTarget)}>Cancel workflow</button>}
              <button className="admin-secondary" disabled={busy} onClick={() => void load()}>Refresh</button>
              {import.meta.env.DEV && runMode !== "replay" && !isSpecificationRevision && <button className="admin-secondary" disabled={busy} onClick={() => void refreshSourceMetadata()}>Refresh source metadata</button>}
            </div>
          </section>

          {(decisionSummary || unresolvedQuestions.length > 0) && (
            <section className="admin-panel" aria-labelledby="decision-summary-title">
              <div className="admin-panel-heading"><h2 id="decision-summary-title">Decision summary</h2><span>Human review required</span></div>
              {decisionSummary && <p>{decisionSummary}</p>}
              {unresolvedQuestions.length > 0 && <><h3>Unresolved questions</h3><ul>{unresolvedQuestions.map((item) => <li key={item}>{item}</li>)}</ul></>}
            </section>
          )}
        </main>

        <aside className="admin-secondary-column">
          <section className="admin-panel" aria-labelledby="agent-summary-title">
            <div className="admin-panel-heading"><h2 id="agent-summary-title">Agent activity</h2><span>{runs.length} run{runs.length === 1 ? "" : "s"}</span></div>
            {trace ? (
              <div className="admin-agent-summary">
                <div className="admin-run-identifier">
                  <span>Run {shortRunId(trace.id)}</span>
                  <button
                    type="button"
                    className="admin-copy-button"
                    aria-label={`Copy full run ID ${trace.id}`}
                    onClick={() => void copyIdentifier("run", trace.id)}
                  >{copiedIdentifier === "run" ? "Copied" : "Copy"}</button>
                </div>
                <h3>{trace.agent_name}</h3>
                <span className="admin-status">{runStatusLabel(trace, runMode, hasCandidateRecommendation)}</span>
                <dl>
                  <div><dt>Mode</dt><dd>{configuredModeLabel(runMode)}</dd></div>
                  <div><dt>Final run status</dt><dd>{humanizeMachineValue(trace.status)}</dd></div>
                  <div><dt>Output</dt><dd>{hasCompletedOutput ? `${candidates.length} candidates prepared` : "No recommendation available"}</dd></div>
                  <div><dt>Agent runs</dt><dd>{runs.length}</dd></div>
                  <div><dt>Model turns</dt><dd>{trace.turns}</dd></div>
                  <div><dt>Provider retries</dt><dd>{providerRetries}</dd></div>
                  <div><dt>Tool calls</dt><dd>{agentTools.length}</dd></div>
                  <div><dt>Candidate inspection</dt><dd>{inspectionCounts ? `${inspectionCounts.inspected} inspected / ${inspectionCounts.failed} unresolved` : "Not reached"}</dd></div>
                  <div><dt>Duration</dt><dd>{(trace.duration_ms / 1000).toFixed(2)} s</dd></div>
                  <div><dt>Provider</dt><dd>{trace.provider}</dd></div>
                  <div><dt>Model</dt><dd>{trace.model_identifier}</dd></div>
                  <div><dt>Input tokens</dt><dd>{usageNumber(trace, "input_tokens")}</dd></div>
                  <div><dt>Output tokens</dt><dd>{usageNumber(trace, "output_tokens")}</dd></div>
                  <div><dt>Cached input tokens</dt><dd>{usageNumber(trace, "cached_tokens")}</dd></div>
                  <div><dt>Estimated cost</dt><dd>{trace.usage.usage_status === "usage_unavailable" ? "Usage unavailable after structured-output failure" : `$${(usageNumber(trace, "cost_cents") / 100).toFixed(4)}`}</dd></div>
                </dl>
                {geoSearches.length > 0 && <div className="admin-trace-summary"><h4>Rendered GEO queries</h4><ol>{geoSearches.map((search, index) => <li key={`${search.renderedQuery}-${index}`}><strong>{search.strategyReason}</strong><code>{search.renderedQuery}</code><span>{search.resultCount} results / {search.cacheStatus}</span></li>)}</ol></div>}
                {turnExposures.length > 0 && <div className="admin-trace-summary"><h4>Tools exposed per turn</h4><ol>{turnExposures.map((turn) => <li key={`${turn.turn}-${turn.substage}`}><strong>Turn {turn.turn}: {humanizeMachineValue(turn.substage)}</strong><span>{turn.tools.length > 0 ? turn.tools.map(toolLabel).join(", ") : "Final structured output only"}</span></li>)}</ol></div>}
                {normalizationWarningCount > 0 && <p className="admin-secondary-note">{controlledVocabularyWarningCount > 0 ? `${controlledVocabularyWarningCount} controlled-vocabulary ${controlledVocabularyWarningCount === 1 ? "value was" : "values were"} normalized before execution.` : emptyOptionalFilterWarningCount === normalizationWarningCount ? `${emptyOptionalFilterWarningCount} empty optional ${emptyOptionalFilterWarningCount === 1 ? "filter was" : "filters were"} removed before execution.` : `${normalizationWarningCount} optional filter ${normalizationWarningCount === 1 ? "value was" : "values were"} safely normalized before execution.`}</p>}
                {developerDiagnostic && <div className="admin-trace-summary"><h4>Safe diagnostic</h4><p>{developerDiagnostic}</p></div>}
                {(outputDiagnostic || outputFingerprint || isSpecificationRevision) && (
                  <details className="admin-trace-summary">
                    <summary>Technical audit</summary>
                    <dl>
                      {outputDiagnostic && <>
                        <div><dt>Failure category</dt><dd>{humanizeMachineValue(String(outputDiagnostic.failure_classification ?? "unknown_model_behavior"))}</dd></div>
                        <div><dt>Schema</dt><dd>{String(outputDiagnostic.output_schema_name ?? "unknown")} · {String(outputDiagnostic.output_schema_version ?? "unknown")}</dd></div>
                      </>}
                      {outputFingerprint && <>
                        <div><dt>Output type</dt><dd>{String(outputFingerprint.output_type_name ?? "unknown")} · {outputFingerprint.output_type_present === true ? "present" : "missing"}</dd></div>
                        <div><dt>Structured API</dt><dd>{humanizeMachineValue(String(outputFingerprint.api_surface ?? "unknown"))} · {outputFingerprint.strict_json_schema === true ? "strict schema" : "non-strict schema"}</dd></div>
                        <div><dt>Schema hash</dt><dd><code>{String(outputFingerprint.schema_hash ?? "unknown")}</code></dd></div>
                        <div><dt>Boundary/runtime</dt><dd>{outputFingerprint.boundary_runtime_contracts_match === true ? "Matched" : "Mismatch — execution blocked"}</dd></div>
                      </>}
                      {outputDiagnostic && <>
                        <div><dt>Response shape</dt><dd>{String(outputDiagnostic.output_item_count ?? 0)} items · {textList(outputDiagnostic.output_item_types).join(", ") || "no output items"}</dd></div>
                        <div><dt>Text / JSON</dt><dd>{outputDiagnostic.text_output_present === true ? `${String(outputDiagnostic.bounded_text_length ?? 0)} text characters` : "no text"} · {outputDiagnostic.json_object_present === true ? "JSON object present" : "no JSON object"}</dd></div>
                      </>}
                      <div><dt>Request IDs</dt><dd>{Array.isArray(outputDiagnostic?.provider_request_ids) && outputDiagnostic.provider_request_ids.length ? outputDiagnostic.provider_request_ids.join(", ") : textList(trace.usage.provider_request_ids).join(", ") || "not available"}</dd></div>
                      <div><dt>Response IDs</dt><dd>{Array.isArray(outputDiagnostic?.provider_response_ids) && outputDiagnostic.provider_response_ids.length ? outputDiagnostic.provider_response_ids.join(", ") : textList(trace.usage.provider_response_ids).join(", ") || "not available"}</dd></div>
                      <div><dt>Usage status</dt><dd>{humanizeMachineValue(String((outputDiagnostic?.usage as Record<string, unknown> | undefined)?.usage_status ?? trace.usage.usage_status ?? "usage_unavailable"))}</dd></div>
                      {outputDiagnostic && <div><dt>Handler</dt><dd>{humanizeMachineValue(String(outputDiagnostic.error_handler ?? "none"))}</dd></div>}
                    </dl>
                  </details>
                )}
                {scientificSourceDiagnostics.length > 0 && <div className="admin-trace-summary"><h4>Scientific-source diagnostics</h4>{scientificSourceDiagnostics.map((diagnostic, index) => <SourceDiagnosticDetails diagnostic={diagnostic} key={`${diagnostic.tool_name}-${index}`} />)}</div>}
                <ol id="agent-tools" className="admin-tool-list">{agentTools.map((tool) => <li key={tool.id}><span>{toolLabel(tool.tool_name)}</span><small>{tool.status}</small></li>)}</ol>
                <div className="admin-inline-actions">
                  <button className="admin-link-button" onClick={() => setShowTrace((value) => !value)}>{showTrace ? "Hide trace" : "View trace"}</button>
                  <a href="#agent-tools">View tools used</a>
                  <a href="#artifacts">View generated artifact</a>
                </div>
                {showTrace && <div className="admin-trace-summary" id="agent-trace"><h4>Trace events</h4><ol>{(trace.trace?.events ?? []).map((event, index) => <li key={index}>{typeof event.event_type === "string" ? humanizeMachineValue(event.event_type.replace(/\./g, "_")) : `Trace event ${index + 1}`}</li>)}</ol>{normalizationSummaries.map((summary, index) => <section key={`${summary.toolName}-${index}`}><h5>{toolLabel(summary.toolName)} input normalization</h5><p>{summary.warningCodes.length} warning{summary.warningCodes.length === 1 ? "" : "s"}: {summary.warningCodes.join(", ")}</p><dl><div><dt>Model-supplied arguments</dt><dd><code>{JSON.stringify(summary.originalArguments)}</code></dd></div><div><dt>Executed arguments</dt><dd><code>{JSON.stringify(summary.normalizedArguments)}</code></dd></div></dl><ul className="admin-normalization-list">{summary.warnings.map((warning, warningIndex) => <li key={`${warning.code}-${warning.field}-${warningIndex}`}><strong>{warning.field}</strong><span>{warning.original ?? "not recorded"} → {warning.normalized ?? "not executed"}</span><small>{warning.code}{warning.policyVersion ? ` · ${warning.policyVersion}` : ""}</small></li>)}</ul></section>)}</div>}
              </div>
            ) : <div className="admin-empty">No agent run yet.</div>}
          </section>

          <section className="admin-panel" id="artifacts" aria-labelledby="artifacts-title">
            <div className="admin-panel-heading"><h2 id="artifacts-title">Generated artifacts</h2><span>{artifacts.length}</span></div>
            <div className="admin-artifact-list">{artifacts.map((artifact) => <article key={artifact.id}><div><strong>{artifact.logical_name}</strong><span>{humanizeMachineValue(artifact.artifact_type)} · {artifact.producer}</span></div></article>)}</div>
          </section>

          <section className="admin-panel" id="technical-audit" aria-labelledby="activity-title">
            <div className="admin-tabs" role="tablist" aria-label="Build history">
              <button id="activity-tab" role="tab" aria-selected={activityTab === "activity"} aria-controls="activity-panel" tabIndex={activityTab === "activity" ? 0 : -1} onKeyDown={handleTabKey} onClick={() => setActivityTab("activity")}>Activity</button>
              <button id="audit-tab" role="tab" aria-selected={activityTab === "audit"} aria-controls="audit-panel" tabIndex={activityTab === "audit" ? 0 : -1} onKeyDown={handleTabKey} onClick={() => setActivityTab("audit")}>Technical audit log</button>
            </div>
            {activityTab === "activity" ? (
              <div id="activity-panel" role="tabpanel" aria-labelledby="activity-tab">
                <h2 id="activity-title" className="sr-only">Activity</h2>
                <ol className="admin-activity-list">
                  {displayEvents.map((event) => <li key={event.id}><span className="admin-activity-marker" aria-hidden /><div><strong>{activityLabel(event)}</strong><p>{actorLabel(event.actor_type)} · {new Date(event.created_at).toLocaleString()}</p></div></li>)}
                </ol>
                {events.length > 6 && <button className="admin-link-button" onClick={() => setFullActivity((value) => !value)}>{fullActivity ? "Show recent activity" : "Show full activity"}</button>}
              </div>
            ) : (
              <div id="audit-panel" role="tabpanel" aria-labelledby="audit-tab">
                <dl className="admin-audit-identifiers">
                  <div><dt>Build ID</dt><dd>{build.id}</dd></div>
                  {trace && <div><dt>Current run ID</dt><dd>{trace.id}</dd></div>}
                </dl>
                <ol className="admin-audit-list">
                  {[...events].reverse().map((event) => <li key={event.id}><div><strong>{event.event_type}</strong><time>{new Date(event.created_at).toLocaleString()}</time></div><dl><div><dt>Transition</dt><dd>{event.from_state ?? "none"} to {event.to_state ?? "none"}</dd></div><div><dt>Actor</dt><dd>{event.actor_type} / {event.actor_id}</dd></div><div><dt>Event ID</dt><dd>{event.id}</dd></div><div><dt>Event hash</dt><dd>{event.event_hash}</dd></div></dl></li>)}
                </ol>
              </div>
            )}
          </section>

          <details className="admin-panel admin-technical-details">
            <summary>Technical details</summary>
            <dl>
              <div><dt>Machine state</dt><dd>{build.current_stage}</dd></div>
              <div><dt>Workflow version</dt><dd>{build.version}</dd></div>
              <div><dt>Build ID</dt><dd>{build.id}</dd></div>
              {trace && <div><dt>Current run ID</dt><dd>{trace.id}</dd></div>}
              {pending && <div><dt>Proposal binding</dt><dd>{pending.proposal_hash}</dd></div>}
            </dl>
          </details>

          {errors.length > 0 && <section className="admin-panel admin-errors-panel"><div className="admin-panel-heading"><h2>Workflow errors</h2><span>{errors.length}</span></div>{errors.map((item) => <article className="admin-error-row" key={item.id}><strong>{humanizeMachineValue(item.code)}</strong><p>{item.safe_message}</p><span>{item.retryable ? "Retryable" : "Terminal"}</span>{item.detail?.source_diagnostic && <SourceDiagnosticDetails diagnostic={item.detail.source_diagnostic} />}</article>)}</section>}

          {import.meta.env.DEV && (
            <details className="admin-panel admin-developer-tools">
              <summary>Developer tools <span>Test only</span></summary>
              <p>Simulation-only recovery controls. These actions are not part of the production workflow.</p>
              {canFail ? <button className="admin-danger-outline" disabled={busy} onClick={() => void command("simulate-failure")}>Trigger controlled failure</button> : <span>No test action is valid in this state.</span>}
            </details>
          )}
        </aside>
      </div>

      {confirmation && (
        <AdminModal
          title={confirmation.kind === "approve" ? (isAssemblyApproval ? "Confirm assembly strategy approval" : isSpecificationApproval ? "Confirm target specification approval" : "Confirm dataset approval") : confirmation.kind === "cancel" ? "Cancel this workflow?" : `Confirm ${humanizeMachineValue(confirmation.kind).toLowerCase()}`}
          description={confirmation.kind === "approve" ? (isAssemblyApproval ? "This approves only the verified source graph and preparation strategy; it does not authorize training." : "Review the selected proposal and approval scope before continuing.") : "This decision will be recorded in the immutable audit history."}
          onClose={() => setConfirmation(null)}
          returnFocus={confirmationTrigger.current}
        >
          {confirmation.kind === "cancel" ? (
            <div className="admin-confirmation">
              <p>Build {shortBuildId(build.id)} will be cancelled. Completed artifacts and audit history will remain available.</p>
              <div className="admin-modal-actions"><button className="admin-secondary" onClick={() => setConfirmation(null)}>Keep workflow</button><button className="admin-danger" disabled={busy} onClick={() => void command("cancel")}>Cancel workflow</button></div>
            </div>
          ) : (
            <div className="admin-confirmation">
              <dl>
                <div><dt>Selected candidate</dt><dd>{selectedCandidate ? `${candidateLabel(selectedCandidate, selectedIndex)} · ${selectedCandidate.title}` : "No candidate"}</dd></div>
                <div><dt>Evidence binding</dt><dd>Current immutable candidate artifact</dd></div>
                <div><dt>Approval scope</dt><dd>{isAssemblyApproval ? "Verified source graph and deterministic preparation strategy only" : isSpecificationApproval ? "Target training-data contract only" : "Dataset selection for this build only"}</dd></div>
                <div><dt>Next stage</dt><dd>{confirmation.kind === "approve" ? (isAssemblyApproval ? "Complete assembly review; construction and training remain deferred" : isSpecificationApproval ? "Derive component requirements" : "Prepare the selected dataset") : confirmation.kind === "request_revision" ? "Revise the prepared comparison" : confirmation.kind === "choose_alternative" ? "Record the alternative selection" : "Stop this proposal"}</dd></div>
              </dl>
              {comment && <p><strong>Reviewer comment:</strong> {comment}</p>}
              <div className="admin-modal-actions"><button className="admin-secondary" onClick={() => setConfirmation(null)}>Go back</button><button className={confirmation.kind === "reject" ? "admin-danger" : "admin-primary"} disabled={busy} onClick={() => confirmation.approval && void decide(confirmation.approval, confirmation.kind as Decision)}>Confirm decision</button></div>
            </div>
          )}
        </AdminModal>
      )}
    </section>
  );
}
