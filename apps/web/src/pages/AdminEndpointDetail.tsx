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

function TrainingDatasetWorkspace({ data }: { data: AdminTrainingDatasetWorkflow }) {
  if (data.legacy) {
    return <section className="admin-panel"><h2>Legacy single-source discovery</h2><p>Historical records remain unchanged and use the original dataset-selection workflow.</p></section>;
  }
  const specification = record(data.target_specification);
  const requirements = records(record(data.component_requirements).requirements);
  const sources = records(record(data.verified_source_inventory).sources);
  const cells = records(record(data.capability_matrix).cells);
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
      <section className="admin-panel"><div className="admin-panel-heading"><h2>Target training dataset</h2><span>{data.benchmark_mode ?? "standard"}</span></div>
        {Object.keys(specification).length ? <dl className="admin-candidate-facts"><div><dt>Prediction task</dt><dd>{String(specification.intended_prediction_task ?? "Unresolved")}</dd></div><div><dt>Prediction unit</dt><dd>{String(specification.prediction_unit ?? "Unresolved")}</dd></div><div><dt>Activity representation</dt><dd>{textList(specification.acceptable_activity_representations).join(", ") || "Unresolved"}</dd></div><div><dt>Required fields</dt><dd>{textList(specification.mandatory_output_fields).join(", ") || "Awaiting specification"}</dd></div></dl> : <p>Awaiting a strict, human-reviewed target specification.</p>}
      </section>
      <section className="admin-panel"><div className="admin-panel-heading"><h2>Component requirements</h2><span>{requirements.length}</span></div>{requirements.length ? <ul>{requirements.map((item) => <li key={String(item.requirement_id)}><strong>{humanizeMachineValue(String(item.role))}</strong> â€” {item.mandatory ? "required" : "optional"}</li>)}</ul> : <p>No requirements derived yet.</p>}</section>
      <section className="admin-panel"><div className="admin-panel-heading"><h2>Verified source inventory</h2><span>{sources.length}</span></div>{sources.length ? sources.map((source) => <article key={String(source.source_id)}><h3>{String(source.source_system)} Â· {String(source.stable_accession)}</h3><p>{textList(source.source_roles).map(humanizeMachineValue).join(", ")}</p><p>{textList(source.limitations).join(" ") || "No recorded limitation."}</p></article>) : <p>No discovered or verified sources. Concrete strategies remain locked.</p>}</section>
      <section className="admin-panel"><div className="admin-panel-heading"><h2>Capability matrix</h2><span>{cells.length} cells</span></div>{cells.length ? <div className="admin-artifact-list">{cells.map((cell, index) => <article key={`${String(cell.source_id)}-${String(cell.component)}-${index}`}><strong>{String(cell.source_id)}</strong><span>{humanizeMachineValue(String(cell.component))}: {humanizeMachineValue(String(cell.status))}</span></article>)}</div> : <p>Awaiting deterministic inventory analysis.</p>}</section>
      <section className="admin-panel"><div className="admin-panel-heading"><h2>Strategies considered</h2><span>{strategies.length}</span></div>{strategies.length ? strategies.map((strategy) => <article key={String(strategy.strategy_id)}><h3>{strategy.strategy_id === recommendedId ? "Recommended: " : ""}{String(strategy.strategy_id)}</h3><p>{textList(strategy.scientific_risks).join(" ") || "No scientific risks recorded."}</p></article>) : <p>No assembly strategy exists before verified discovery.</p>}</section>
      <section className="admin-panel"><div className="admin-panel-heading"><h2>Recommended assembly graph</h2><span>{nodes.length} nodes</span></div>{nodes.length ? <><ul>{nodes.map((node) => <li key={String(node.node_id)}><strong>{String(node.label)}</strong> ({humanizeMachineValue(String(node.node_type))})</li>)}</ul><p>{edges.map((edge) => `${String(edge.from_node)} â†’ ${String(edge.to_node)}`).join(" Â· ")}</p></> : <p>No source graph selected.</p>}</section>
      <section className="admin-panel"><div className="admin-panel-heading"><h2>Preparation plan</h2><span>{records(plan.steps).length} steps</span></div>{records(plan.steps).length ? <ol>{records(plan.steps).map((step) => <li key={String(step.step_id)}><strong>{String(step.action)}</strong> â€” {humanizeMachineValue(String(step.status))}</li>)}</ol> : <p>Preparation remains deferred until a verified strategy exists.</p>}</section>
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
      const nested = (detail as Record<string, unknown>).source_diagnostic;
      const message = typeof direct === "string"
        ? direct
        : nested && typeof nested === "object" && !Array.isArray(nested)
          ? (nested as Record<string, unknown>).developer_message
          : null;
      if (typeof message === "string" && message.length > 0) return message;
    }
  }
  return null;
}

function sourceDiagnostics(run: AdminAgentRun): AdminSourceToolDiagnostic[] {
  return (run.tool_calls ?? []).flatMap((call) => {
    const result = call.result;
    if (!result || typeof result !== "object" || Array.isArray(result)) return [];
    const diagnostic = (result as Record<string, unknown>óŸ:¶‰ËkºwµçQÑ½¸ùô(€€€€€€€€€€€€€í…¹I•ÑÉä€˜˜€ñ‰ÕÑÑ½¸‘¥Í…‰±•õí‰ÕÍåô½¹±¥¬õì ¤€ôøÙ½¥½µµ…¹ ‰É•ÑÉäˆ¥ôùI•ÑÉä™…¥±•ÍÑ•Àğ½‰ÕÑÑ½¸ùô(€€€€€€€€€€€€€í…¹…¹•°€˜˜€ñ‰ÕÑÑ½¸±…ÍÍ9…µ”ô‰…‘µ¥¸µ‘…¹•Èµ½ÕÑ±¥¹”ˆ‘¥Í…‰±•õí‰ÕÍåô½¹±¥¬õì¡•Ù•¹Ğ¤€ôøÉ•ÅÕ•ÍÑ½¹™¥Éµ…Ñ¥½¸ ‰…¹•°ˆ°Õ¹‘•™¥¹•°•Ù•¹Ğ¹ÕÉÉ•¹ÑQ…É•Ğ¥ôù…¹•°İ½É­™±½Üğ½‰ÕÑÑ½¸ùô(€€€€€€€€€€€€€€ñ‰ÕÑÑ½¸±…ÍÍ9…µ”ô‰…‘µ¥¸µÍ•½¹‘…Éäˆ‘¥Í…‰±•õí‰ÕÍåô½¹±¥¬õì ¤€ôøÙ½¥±½… ¥ôùI•™É•Í ğ½‰ÕÑÑ½¸ø(€€€€€€€€€€€€€í¥µÁ½ÉĞ¹µ•Ñ„¹•¹Ø¹X€˜˜ÉÕ¹5½‘”€„ôô€‰É•Á±…äˆ€˜˜€ñ‰ÕÑÑ½¸±…ÍÍ9…µ”ô‰…‘µ¥¸µÍ•½¹‘…Éäˆ‘¥Í…‰±•õí‰ÕÍåô½¹±¥¬õì ¤€ôøÙ½¥É•™É•Í¡M½ÕÉ•5•Ñ…‘…Ñ„ ¥ôùI•™É•Í Í½ÕÉ”µ•Ñ…‘…Ñ„ğ½‰ÕÑÑ½¸ùô(€€€€€€€€€€€€ğ½‘¥Øø(€€€€€€€€€€ğ½Í•Ñ¥½¸ø((€€€€€€€€€ì¡‘•¥Í¥½¹MÕµµ…ÉäñğÕ¹É•Í½±Ù•‘EÕ•ÍÑ¥½¹Ì¹±•¹Ñ €ø€À¤€˜˜€ (€€€€€€€€€€€€ñÍ•Ñ¥½¸±…ÍÍ9…µ”ô‰…‘µ¥¸µÁ…¹•°ˆ…É¥„µ±…‰•±±•‘‰äô‰‘•¥Í¥½¸µÍÕµµ…ÉäµÑ¥Ñ±”ˆø(€€€€€€€€€€€€€€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µÁ…¹•°µ¡•…‘¥¹œˆøñ È¥ô‰‘•¥Í¥½¸µÍÕµµ…ÉäµÑ¥Ñ±”ˆù•¥Í¥½¸ÍÕµµ…Éäğ½ ÈøñÍÁ…¸ù!Õµ…¸É•Ù¥•ÜÉ•ÅÕ¥É•ğ½ÍÁ…¸øğ½‘¥Øø(€€€€€€€€€€€€€í‘•¥Í¥½¹MÕµµ…Éä€˜˜€ñÀùí‘•¥Í¥½¹MÕµµ…Éåôğ½Àùô(€€€€€€€€€€€€€íÕ¹É•Í½±Ù•‘EÕ•ÍÑ¥½¹Ì¹±•¹Ñ €ø€À€˜˜€ğøñ ÌùU¹É•Í½±Ù•ÅÕ•ÍÑ¥½¹Ìğ½ ÌøñÕ°ùíÕ¹É•Í½±Ù•‘EÕ•ÍÑ¥½¹Ì¹µ…À ¡¥Ñ•´¤€ôø€ñ±¤­•äõí¥Ñ•µôùí¥Ñ•µôğ½±¤ø¥ôğ½Õ°øğ¼ùô(€€€€€€€€€€€€ğ½Í•Ñ¥½¸ø(€€€€€€€€€€¥ô(€€€€€€€€ğ½µ…¥¸ø((€€€€€€€€ñ…Í¥‘”±…ÍÍ9…µ”ô‰…‘µ¥¸µÍ•½¹‘…Éäµ½±Õµ¸ˆø(€€€€€€€€€€ñÍ•Ñ¥½¸±…ÍÍ9…µ”ô‰…‘µ¥¸µÁ…¹•°ˆ…É¥„µ±…‰•±±•‘‰äô‰…•¹ĞµÍÕµµ…ÉäµÑ¥Ñ±”ˆø(€€€€€€€€€€€€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µÁ…¹•°µ¡•…‘¥¹œˆøñ È¥ô‰…•¹ĞµÍÕµµ…ÉäµÑ¥Ñ±”ˆù•¹Ğ…Ñ¥Ù¥Ñäğ½ ÈøñÍÁ…¸ùíÉÕ¹Ì¹±•¹Ñ¡ôÉÕ¹íÉÕ¹Ì¹±•¹Ñ €ôôô€Ä€ü€ˆˆ€è€‰Ì‰ôğ½ÍÁ…¸øğ½‘¥Øø(€€€€€€€€€€€íÑÉ…”€ü€ (€€€€€€€€€€€€€€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µ…•¹ĞµÍÕµµ…Éäˆø(€€€€€€€€€€€€€€€€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µÉÕ¸µ¥‘•¹Ñ¥™¥•Èˆø(€€€€€€€€€€€€€€€€€€ñÍÁ…¸ùIÕ¸íÍ¡½ÉÑIÕ¹%¡ÑÉ…”¹¥¥ôğ½ÍÁ…¸ø(€€€€€€€€€€€€€€€€€€ñ‰ÕÑÑ½¸(€€€€€€€€€€€€€€€€€€€ÑåÁ”ô‰‰ÕÑÑ½¸ˆ(€€€€€€€€€€€€€€€€€€€±…ÍÍ9…µ”ô‰…‘µ¥¸µ½Áäµ‰ÕÑÑ½¸ˆ(€€€€€€€€€€€€€€€€€€€…É¥„µ±…‰•°õí½Áä™Õ±°ÉÕ¸%€‘íÑÉ…”¹¥‘õô(€€€€€€€€€€€€€€€€€€€½¹±¥¬õì ¤€ôøÙ½¥½Áå%‘•¹Ñ¥™¥•È ‰ÉÕ¸ˆ°ÑÉ…”¹¥¥ô(€€€€€€€€€€€€€€€€€€ùí½Á¥•‘%‘•¹Ñ¥™¥•È€ôôô€‰ÉÕ¸ˆ€ü€‰½Á¥•ˆ€è€‰½Áä‰ôğ½‰ÕÑÑ½¸ø(€€€€€€€€€€€€€€€€ğ½‘¥Øø(€€€€€€€€€€€€€€€€ñ ÌùíÑÉ…”¹…•¹Ñ}¹…µ•ôğ½ Ìø(€€€€€€€€€€€€€€€€ñÍÁ…¸±…ÍÍ9…µ”ô‰…‘µ¥¸µÍÑ…ÑÕÌˆùíÉÕ¹MÑ…ÑÕÍ1…‰•°¡ÑÉ…”°ÉÕ¹5½‘”°¡…Í…¹‘¥‘…Ñ•I•½µµ•¹‘…Ñ¥½¸¥ôğ½ÍÁ…¸ø(€€€€€€€€€€€€€€€€ñ‘°ø(€€€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘Ğù5½‘”ğ½‘Ğøñ‘ùí½¹™¥ÕÉ•‘5½‘•1…‰•°¡ÉÕ¹5½‘”¥ôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘Ğù¥¹…°ÉÕ¸ÍÑ…ÑÕÌğ½‘Ğøñ‘ùí¡Õµ…¹¥é•5…¡¥¹•Y…±Õ”¡ÑÉ…”¹ÍÑ…ÑÕÌ¥ôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘Ğù=ÕÑÁÕĞğ½‘Ğøñ‘ùí¡…Í½µÁ±•Ñ•‘=ÕÑÁÕĞ€ü€‘í…¹‘¥‘…Ñ•Ì¹±•¹Ñ¡ô…¹‘¥‘…Ñ•ÌÁÉ•Á…É•‘€€è€‰9¼É•½µµ•¹‘…Ñ¥½¸…Ù…¥±…‰±”‰ôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘Ğù•¹ĞÉÕ¹Ìğ½‘Ğøñ‘ùíÉÕ¹Ì¹±•¹Ñ¡ôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘Ğù5½‘•°ÑÕÉ¹Ìğ½‘Ğøñ‘ùíÑÉ…”¹ÑÕÉ¹Íôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘ĞùAÉ½Ù¥‘•ÈÉ•ÑÉ¥•Ìğ½‘Ğøñ‘ùíÁÉ½Ù¥‘•ÉI•ÑÉ¥•Íôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘ĞùQ½½°…±±Ìğ½‘Ğøñ‘ùí…•¹ÑQ½½±Ì¹±•¹Ñ¡ôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘Ğù…¹‘¥‘…Ñ”¥¹ÍÁ•Ñ¥½¸ğ½‘Ğøñ‘ùí¥¹ÍÁ•Ñ¥½¹½Õ¹ÑÌ€ü€‘í¥¹ÍÁ•Ñ¥½¹½Õ¹ÑÌ¹¥¹ÍÁ•Ñ•‘ô¥¹ÍÁ•Ñ•€¼€‘í¥¹ÍÁ•Ñ¥½¹½Õ¹ÑÌ¹™…¥±•‘ôÕ¹É•Í½±Ù•‘€€è€‰9½ĞÉ•…¡•‰ôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘ĞùÕÉ…Ñ¥½¸ğ½‘Ğøñ‘ùì¡ÑÉ…”¹‘ÕÉ…Ñ¥½¹}µÌ€¼€ÄÀÀÀ¤¹Ñ½¥á• È¥ôÌğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘ĞùAÉ½Ù¥‘•Èğ½‘Ğøñ‘ùíÑÉ…”¹ÁÉ½Ù¥‘•Éôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘Ğù5½‘•°ğ½‘Ğøñ‘ùíÑÉ…”¹µ½‘•±}¥‘•¹Ñ¥™¥•Éôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘Ğù%¹ÁÕĞÑ½­•¹Ìğ½‘Ğøñ‘ùíÕÍ…•9Õµ‰•È¡ÑÉ…”°€‰¥¹ÁÕÑ}Ñ½­•¹Ìˆ¥ôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘Ğù=ÕÑÁÕĞÑ½­•¹Ìğ½‘Ğøñ‘ùíÕÍ…•9Õµ‰•È¡ÑÉ…”°€‰½ÕÑÁÕÑ}Ñ½­•¹Ìˆ¥ôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘Ğù…¡•¥¹ÁÕĞÑ½­•¹Ìğ½‘Ğøñ‘ùíÕÍ…•9Õµ‰•È¡ÑÉ…”°€‰…¡•‘}Ñ½­•¹Ìˆ¥ôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘ĞùÍÑ¥µ…Ñ•½ÍĞğ½‘Ğøñ‘ø‘ì¡ÕÍ…•9Õµ‰•È¡ÑÉ…”°€‰½ÍÑ}•¹ÑÌˆ¤€¼€ÄÀÀ¤¹Ñ½¥á• Ğ¥ôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€ğ½‘°ø(€€€€€€€€€€€€€€€í•½M•…É¡•Ì¹±•¹Ñ €ø€À€˜˜€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µÑÉ…”µÍÕµµ…Éäˆøñ ĞùI•¹‘•É•<ÅÕ•É¥•Ìğ½ Ğøñ½°ùí•½M•…É¡•Ì¹µ…À ¡Í•…É °¥¹‘•à¤€ôø€ñ±¤­•äõí€‘íÍ•…É ¹É•¹‘•É•‘EÕ•Éåô´‘í¥¹‘•áõôøñÍÑÉ½¹œùíÍ•…É ¹ÍÑÉ…Ñ•åI•…Í½¹ôğ½ÍÑÉ½¹œøñ½‘”ùíÍ•…É ¹É•¹‘•É•‘EÕ•Éåôğ½½‘”øñÍÁ…¸ùíÍ•…É ¹É•ÍÕ±Ñ½Õ¹ÑôÉ•ÍÕ±ÑÌ€¼íÍ•…É ¹…¡•MÑ…ÑÕÍôğ½ÍÁ…¸øğ½±¤ø¥ôğ½½°øğ½‘¥Øùô(€€€€€€€€€€€€€€€íÑÕÉ¹áÁ½ÍÕÉ•Ì¹±•¹Ñ €ø€À€˜˜€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µÑÉ…”µÍÕµµ…Éäˆøñ ĞùQ½½±Ì•áÁ½Í•Á•ÈÑÕÉ¸ğ½ Ğøñ½°ùíÑÕÉ¹áÁ½ÍÕÉ•Ì¹µ…À ¡ÑÕÉ¸¤€ôø€ñ±¤­•äõí€‘íÑÕÉ¸¹ÑÕÉ¹ô´‘íÑÕÉ¸¹ÍÕ‰ÍÑ…•õôøñÍÑÉ½¹œùQÕÉ¸íÑÕÉ¸¹ÑÕÉ¹ôèí¡Õµ…¹¥é•5…¡¥¹•Y…±Õ”¡ÑÕÉ¸¹ÍÕ‰ÍÑ…”¥ôğ½ÍÑÉ½¹œøñÍÁ…¸ùíÑÕÉ¸¹Ñ½½±Ì¹±•¹Ñ €ø€À€üÑÕÉ¸¹Ñ½½±Ì¹µ…À¡Ñ½½±1…‰•°¤¹©½¥¸ ˆ°€ˆ¤€è€‰¥¹…°ÍÑÉÕÑÕÉ•½ÕÑÁÕĞ½¹±ä‰ôğ½ÍÁ…¸øğ½±¤ø¥ôğ½½°øğ½‘¥Øùô(€€€€€€€€€€€€€€€í¹½Éµ…±¥é…Ñ¥½¹]…É¹¥¹½Õ¹Ğ€ø€À€˜˜€ñÀ±…ÍÍ9…µ”ô‰…‘µ¥¸µÍ•½¹‘…Éäµ¹½Ñ”ˆùí½¹ÑÉ½±±•‘Y½…‰Õ±…Éå]…É¹¥¹½Õ¹Ğ€ø€À€ü€‘í½¹ÑÉ½±±•‘Y½…‰Õ±…Éå]…É¹¥¹½Õ¹Ñô½¹ÑÉ½±±•µÙ½…‰Õ±…Éä€‘í½¹ÑÉ½±±•‘Y½…‰Õ±…Éå]…É¹¥¹½Õ¹Ğ€ôôô€Ä€ü€‰Ù…±Õ”İ…Ìˆ€è€‰Ù…±Õ•Ìİ•É”‰ô¹½Éµ…±¥é•‰•™½É”•á•ÕÑ¥½¸¹€€è•µÁÑå=ÁÑ¥½¹…±¥±Ñ•É]…É¹¥¹½Õ¹Ğ€ôôô¹½Éµ…±¥é…Ñ¥½¹]…É¹¥¹½Õ¹Ğ€ü€‘í•µÁÑå=ÁÑ¥½¹…±¥±Ñ•É]…É¹¥¹½Õ¹Ñô•µÁÑä½ÁÑ¥½¹…°€‘í•µÁÑå=ÁÑ¥½¹…±¥±Ñ•É]…É¹¥¹½Õ¹Ğ€ôôô€Ä€ü€‰™¥±Ñ•Èİ…Ìˆ€è€‰™¥±Ñ•ÉÌİ•É”‰ôÉ•µ½Ù•‰•™½É”•á•ÕÑ¥½¸¹€€è€‘í¹½Éµ…±¥é…Ñ¥½¹]…É¹¥¹½Õ¹Ñô½ÁÑ¥½¹…°™¥±Ñ•È€‘í¹½Éµ…±¥é…Ñ¥½¹]…É¹¥¹½Õ¹Ğ€ôôô€Ä€ü€‰Ù…±Õ”İ…Ìˆ€è€‰Ù…±Õ•Ìİ•É”‰ôÍ…™•±ä¹½Éµ…±¥é•‰•™½É”•á•ÕÑ¥½¸¹ôğ½Àùô(€€€€€€€€€€€€€€€í‘•Ù•±½Á•É¥…¹½ÍÑ¥Œ€˜˜€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µÑÉ…”µÍÕµµ…Éäˆøñ ĞùM…™”‘¥…¹½ÍÑ¥Œğ½ ĞøñÀùí‘•Ù•±½Á•É¥…¹½ÍÑ¥ôğ½Àøğ½‘¥Øùô(€€€€€€€€€€€€€€€íÍ¥•¹Ñ¥™¥M½ÕÉ•¥…¹½ÍÑ¥Ì¹±•¹Ñ €ø€À€˜˜€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µÑÉ…”µÍÕµµ…Éäˆøñ ĞùM¥•¹Ñ¥™¥ŒµÍ½ÕÉ”‘¥…¹½ÍÑ¥Ìğ½ ĞùíÍ¥•¹Ñ¥™¥M½ÕÉ•¥…¹½ÍÑ¥Ì¹µ…À ¡‘¥…¹½ÍÑ¥Œ°¥¹‘•à¤€ôø€ñM½ÕÉ•¥…¹½ÍÑ¥•Ñ…¥±Ì‘¥…¹½ÍÑ¥Œõí‘¥…¹½ÍÑ¥ô­•äõí€‘í‘¥…¹½ÍÑ¥Œ¹Ñ½½±}¹…µ•ô´‘í¥¹‘•áõô€¼ø¥ôğ½‘¥Øùô(€€€€€€€€€€€€€€€€ñ½°¥ô‰…•¹ĞµÑ½½±Ìˆ±…ÍÍ9…µ”ô‰…‘µ¥¸µÑ½½°µ±¥ÍĞˆùí…•¹ÑQ½½±Ì¹µ…À ¡Ñ½½°¤€ôø€ñ±¤­•äõíÑ½½°¹¥‘ôøñÍÁ…¸ùíÑ½½±1…‰•°¡Ñ½½°¹Ñ½½±}¹…µ”¥ôğ½ÍÁ…¸øñÍµ…±°ùíÑ½½°¹ÍÑ…ÑÕÍôğ½Íµ…±°øğ½±¤ø¥ôğ½½°ø(€€€€€€€€€€€€€€€€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µ¥¹±¥¹”µ…Ñ¥½¹Ìˆø(€€€€€€€€€€€€€€€€€€ñ‰ÕÑÑ½¸±…ÍÍ9…µ”ô‰…‘µ¥¸µ±¥¹¬µ‰ÕÑÑ½¸ˆ½¹±¥¬õì ¤€ôøÍ•ÑM¡½İQÉ…” ¡Ù…±Õ”¤€ôø€…Ù…±Õ”¥ôùíÍ¡½İQÉ…”€ü€‰!¥‘”ÑÉ…”ˆ€è€‰Y¥•ÜÑÉ…”‰ôğ½‰ÕÑÑ½¸ø(€€€€€€€€€€€€€€€€€€ñ„¡É•˜ôˆ…•¹ĞµÑ½½±ÌˆùY¥•ÜÑ½½±ÌÕÍ•ğ½„ø(€€€€€€€€€€€€€€€€€€ñ„¡É•˜ôˆ…ÉÑ¥™…ÑÌˆùY¥•Ü•¹•É…Ñ•…ÉÑ¥™…Ğğ½„ø(€€€€€€€€€€€€€€€€ğ½‘¥Øø(€€€€€€€€€€€€€€€íÍ¡½İQÉ…”€˜˜€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µÑÉ…”µÍÕµµ…Éäˆ¥ô‰…•¹ĞµÑÉ…”ˆøñ ĞùQÉ…”•Ù•¹ÑÌğ½ Ğøñ½°ùì¡ÑÉ…”¹ÑÉ…”ü¹•Ù•¹ÑÌ€üümt¤¹µ…À ¡•Ù•¹Ğ°¥¹‘•à¤€ôø€ñ±¤­•äõí¥¹‘•áôùíÑåÁ•½˜•Ù•¹Ğ¹•Ù•¹Ñ}ÑåÁ”€ôôô€‰ÍÑÉ¥¹œˆ€ü¡Õµ…¹¥é•5…¡¥¹•Y…±Õ”¡•Ù•¹Ğ¹•Ù•¹Ñ}ÑåÁ”¹É•Á±…” ½p¸½œ°€‰|ˆ¤¤€èQÉ…”•Ù•¹Ğ€‘í¥¹‘•à€¬€Åõôğ½±¤ø¥ôğ½½°ùí¹½Éµ…±¥é…Ñ¥½¹MÕµµ…É¥•Ì¹µ…À ¡ÍÕµµ…Éä°¥¹‘•à¤€ôø€ñÍ•Ñ¥½¸­•äõí€‘íÍÕµµ…Éä¹Ñ½½±9…µ•ô´‘í¥¹‘•áõôøñ ÔùíÑ½½±1…‰•°¡ÍÕµµ…Éä¹Ñ½½±9…µ”¥ô¥¹ÁÕĞ¹½Éµ…±¥é…Ñ¥½¸ğ½ ÔøñÀùíÍÕµµ…Éä¹İ…É¹¥¹½‘•Ì¹±•¹Ñ¡ôİ…É¹¥¹íÍÕµµ…Éä¹İ…É¹¥¹½‘•Ì¹±•¹Ñ €ôôô€Ä€ü€ˆˆ€è€‰Ì‰ôèíÍÕµµ…Éä¹İ…É¹¥¹½‘•Ì¹©½¥¸ ˆ°€ˆ¥ôğ½Àøñ‘°øñ‘¥Øøñ‘Ğù5½‘•°µÍÕÁÁ±¥•…ÉÕµ•¹ÑÌğ½‘Ğøñ‘øñ½‘”ùí)M=8¹ÍÑÉ¥¹¥™ä¡ÍÕµµ…Éä¹½É¥¥¹…±ÉÕµ•¹ÑÌ¥ôğ½½‘”øğ½‘øğ½‘¥Øøñ‘¥Øøñ‘Ğùá•ÕÑ•…ÉÕµ•¹ÑÌğ½‘Ğøñ‘øñ½‘”ùí)M=8¹ÍÑÉ¥¹¥™ä¡ÍÕµµ…Éä¹¹½Éµ…±¥é•‘ÉÕµ•¹ÑÌ¥ôğ½½‘”øğ½‘øğ½‘¥Øøğ½‘°øñÕ°±…ÍÍ9…µ”ô‰…‘µ¥¸µ¹½Éµ…±¥é…Ñ¥½¸µ±¥ÍĞˆùíÍÕµµ…Éä¹İ…É¹¥¹Ì¹µ…À ¡İ…É¹¥¹œ°İ…É¹¥¹%¹‘•à¤€ôø€ñ±¤­•äõí€‘íİ…É¹¥¹œ¹½‘•ô´‘íİ…É¹¥¹œ¹™¥•±‘ô´‘íİ…É¹¥¹%¹‘•áõôøñÍÑÉ½¹œùíİ…É¹¥¹œ¹™¥•±‘ôğ½ÍÑÉ½¹œøñÍÁ…¸ùíİ…É¹¥¹œ¹½É¥¥¹…°€üü€‰¹½ĞÉ•½É‘•‰ôƒŠHíİ…É¹¥¹œ¹¹½Éµ…±¥é•€üü€‰¹½Ğ•á•ÕÑ•‰ôğ½ÍÁ…¸øñÍµ…±°ùíİ…É¹¥¹œ¹½‘•õíİ…É¹¥¹œ¹Á½±¥åY•ÉÍ¥½¸€ü€ƒ
Ü€‘íİ…É¹¥¹œ¹Á½±¥åY•ÉÍ¥½¹õ€€è€ˆ‰ôğ½Íµ…±°øğ½±¤ø¥ôğ½Õ°øğ½Í•Ñ¥½¸ø¥ôğ½‘¥Øùô(€€€€€€€€€€€€€€ğ½‘¥Øø(€€€€€€€€€€€€¤€è€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µ•µÁÑäˆù9¼…•¹ĞÉÕ¸å•Ğ¸ğ½‘¥Øùô(€€€€€€€€€€ğ½Í•Ñ¥½¸ø((€€€€€€€€€€ñÍ•Ñ¥½¸±…ÍÍ9…µ”ô‰…‘µ¥¸µÁ…¹•°ˆ¥ô‰…ÉÑ¥™…ÑÌˆ…É¥„µ±…‰•±±•‘‰äô‰…ÉÑ¥™…ÑÌµÑ¥Ñ±”ˆø(€€€€€€€€€€€€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µÁ…¹•°µ¡•…‘¥¹œˆøñ È¥ô‰…ÉÑ¥™…ÑÌµÑ¥Ñ±”ˆù•¹•É…Ñ•…ÉÑ¥™…ÑÌğ½ ÈøñÍÁ…¸ùí…ÉÑ¥™…ÑÌ¹±•¹Ñ¡ôğ½ÍÁ…¸øğ½‘¥Øø(€€€€€€€€€€€€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µ…ÉÑ¥™…Ğµ±¥ÍĞˆùí…ÉÑ¥™…ÑÌ¹µ…À ¡…ÉÑ¥™…Ğ¤€ôø€ñ…ÉÑ¥±”­•äõí…ÉÑ¥™…Ğ¹¥‘ôøñ‘¥ØøñÍÑÉ½¹œùí…ÉÑ¥™…Ğ¹±½¥…±}¹…µ•ôğ½ÍÑÉ½¹œøñÍÁ…¸ùí¡Õµ…¹¥é•5…¡¥¹•Y…±Õ”¡…ÉÑ¥™…Ğ¹…ÉÑ¥™…Ñ}ÑåÁ”¥ôƒ
Üí…ÉÑ¥™…Ğ¹ÁÉ½‘Õ•Éôğ½ÍÁ…¸øğ½‘¥Øøğ½…ÉÑ¥±”ø¥ôğ½‘¥Øø(€€€€€€€€€€ğ½Í•Ñ¥½¸ø((€€€€€€€€€€ñÍ•Ñ¥½¸±…ÍÍ9…µ”ô‰…‘µ¥¸µÁ…¹•°ˆ¥ô‰Ñ•¡¹¥…°µ…Õ‘¥Ğˆ…É¥„µ±…‰•±±•‘‰äô‰…Ñ¥Ù¥ÑäµÑ¥Ñ±”ˆø(€€€€€€€€€€€€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µÑ…‰ÌˆÉ½±”ô‰Ñ…‰±¥ÍĞˆ…É¥„µ±…‰•°ô‰	Õ¥±¡¥ÍÑ½Éäˆø(€€€€€€€€€€€€€€ñ‰ÕÑÑ½¸¥ô‰…Ñ¥Ù¥ÑäµÑ…ˆˆÉ½±”ô‰Ñ…ˆˆ…É¥„µÍ•±•Ñ•õí…Ñ¥Ù¥ÑåQ…ˆ€ôôô€‰…Ñ¥Ù¥Ñä‰ô…É¥„µ½¹ÑÉ½±Ìô‰…Ñ¥Ù¥ÑäµÁ…¹•°ˆÑ…‰%¹‘•àõí…Ñ¥Ù¥ÑåQ…ˆ€ôôô€‰…Ñ¥Ù¥Ñäˆ€ü€À€è€´Åô½¹-•å½İ¸õí¡…¹‘±•Q…‰-•åô½¹±¥¬õì ¤€ôøÍ•ÑÑ¥Ù¥ÑåQ…ˆ ‰…Ñ¥Ù¥Ñäˆ¥ôùÑ¥Ù¥Ñäğ½‰ÕÑÑ½¸ø(€€€€€€€€€€€€€€ñ‰ÕÑÑ½¸¥ô‰…Õ‘¥ĞµÑ…ˆˆÉ½±”ô‰Ñ…ˆˆ…É¥„µÍ•±•Ñ•õí…Ñ¥Ù¥ÑåQ…ˆ€ôôô€‰…Õ‘¥Ğ‰ô…É¥„µ½¹ÑÉ½±Ìô‰…Õ‘¥ĞµÁ…¹•°ˆÑ…‰%¹‘•àõí…Ñ¥Ù¥ÑåQ…ˆ€ôôô€‰…Õ‘¥Ğˆ€ü€À€è€´Åô½¹-•å½İ¸õí¡…¹‘±•Q…‰-•åô½¹±¥¬õì ¤€ôøÍ•ÑÑ¥Ù¥ÑåQ…ˆ ‰…Õ‘¥Ğˆ¥ôùQ•¡¹¥…°…Õ‘¥Ğ±½œğ½‰ÕÑÑ½¸ø(€€€€€€€€€€€€ğ½‘¥Øø(€€€€€€€€€€€í…Ñ¥Ù¥ÑåQ…ˆ€ôôô€‰…Ñ¥Ù¥Ñäˆ€ü€ (€€€€€€€€€€€€€€ñ‘¥Ø¥ô‰…Ñ¥Ù¥ÑäµÁ…¹•°ˆÉ½±”ô‰Ñ…‰Á…¹•°ˆ…É¥„µ±…‰•±±•‘‰äô‰…Ñ¥Ù¥ÑäµÑ…ˆˆø(€€€€€€€€€€€€€€€€ñ È¥ô‰…Ñ¥Ù¥ÑäµÑ¥Ñ±”ˆ±…ÍÍ9…µ”ô‰ÍÈµ½¹±äˆùÑ¥Ù¥Ñäğ½ Èø(€€€€€€€€€€€€€€€€ñ½°±…ÍÍ9…µ”ô‰…‘µ¥¸µ…Ñ¥Ù¥Ñäµ±¥ÍĞˆø(€€€€€€€€€€€€€€€€€í‘¥ÍÁ±…åÙ•¹ÑÌ¹µ…À ¡•Ù•¹Ğ¤€ôø€ñ±¤­•äõí•Ù•¹Ğ¹¥‘ôøñÍÁ…¸±…ÍÍ9…µ”ô‰…‘µ¥¸µ…Ñ¥Ù¥Ñäµµ…É­•Èˆ…É¥„µ¡¥‘‘•¸€¼øñ‘¥ØøñÍÑÉ½¹œùí…Ñ¥Ù¥Ñå1…‰•°¡•Ù•¹Ğ¥ôğ½ÍÑÉ½¹œøñÀùí…Ñ½É1…‰•°¡•Ù•¹Ğ¹…Ñ½É}ÑåÁ”¥ôƒ
Üí¹•Ü…Ñ”¡•Ù•¹Ğ¹É•…Ñ•‘}…Ğ¤¹Ñ½1½…±•MÑÉ¥¹œ ¥ôğ½Àøğ½‘¥Øøğ½±¤ø¥ô(€€€€€€€€€€€€€€€€ğ½½°ø(€€€€€€€€€€€€€€€í•Ù•¹ÑÌ¹±•¹Ñ €ø€Ø€˜˜€ñ‰ÕÑÑ½¸±…ÍÍ9…µ”ô‰…‘µ¥¸µ±¥¹¬µ‰ÕÑÑ½¸ˆ½¹±¥¬õì ¤€ôøÍ•ÑÕ±±Ñ¥Ù¥Ñä ¡Ù…±Õ”¤€ôø€…Ù…±Õ”¥ôùí™Õ±±Ñ¥Ù¥Ñä€ü€‰M¡½ÜÉ••¹Ğ…Ñ¥Ù¥Ñäˆ€è€‰M¡½Ü™Õ±°…Ñ¥Ù¥Ñä‰ôğ½‰ÕÑÑ½¸ùô(€€€€€€€€€€€€€€ğ½‘¥Øø(€€€€€€€€€€€€¤€è€ (€€€€€€€€€€€€€€ñ‘¥Ø¥ô‰…Õ‘¥ĞµÁ…¹•°ˆÉ½±”ô‰Ñ…‰Á…¹•°ˆ…É¥„µ±…‰•±±•‘‰äô‰…Õ‘¥ĞµÑ…ˆˆø(€€€€€€€€€€€€€€€€ñ‘°±…ÍÍ9…µ”ô‰…‘µ¥¸µ…Õ‘¥Ğµ¥‘•¹Ñ¥™¥•ÉÌˆø(€€€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘Ğù	Õ¥±%ğ½‘Ğøñ‘ùí‰Õ¥±¹¥‘ôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€€íÑÉ…”€˜˜€ñ‘¥Øøñ‘ĞùÕÉÉ•¹ĞÉÕ¸%ğ½‘Ğøñ‘ùíÑÉ…”¹¥‘ôğ½‘øğ½‘¥Øùô(€€€€€€€€€€€€€€€€ğ½‘°ø(€€€€€€€€€€€€€€€€ñ½°±…ÍÍ9…µ”ô‰…‘µ¥¸µ…Õ‘¥Ğµ±¥ÍĞˆø(€€€€€€€€€€€€€€€€€íl¸¸¹•Ù•¹ÑÍt¹É•Ù•ÉÍ” ¤¹µ…À ¡•Ù•¹Ğ¤€ôø€ñ±¤­•äõí•Ù•¹Ğ¹¥‘ôøñ‘¥ØøñÍÑÉ½¹œùí•Ù•¹Ğ¹•Ù•¹Ñ}ÑåÁ•ôğ½ÍÑÉ½¹œøñÑ¥µ”ùí¹•Ü…Ñ”¡•Ù•¹Ğ¹É•…Ñ•‘}…Ğ¤¹Ñ½1½…±•MÑÉ¥¹œ ¥ôğ½Ñ¥µ”øğ½‘¥Øøñ‘°øñ‘¥Øøñ‘ĞùQÉ…¹Í¥Ñ¥½¸ğ½‘Ğøñ‘ùí•Ù•¹Ğ¹™É½µ}ÍÑ…Ñ”€üü€‰¹½¹”‰ôÑ¼í•Ù•¹Ğ¹Ñ½}ÍÑ…Ñ”€üü€‰¹½¹”‰ôğ½‘øğ½‘¥Øøñ‘¥Øøñ‘ĞùÑ½Èğ½‘Ğøñ‘ùí•Ù•¹Ğ¹…Ñ½É}ÑåÁ•ô€¼í•Ù•¹Ğ¹…Ñ½É}¥‘ôğ½‘øğ½‘¥Øøñ‘¥Øøñ‘ĞùÙ•¹Ğ%ğ½‘Ğøñ‘ùí•Ù•¹Ğ¹¥‘ôğ½‘øğ½‘¥Øøñ‘¥Øøñ‘ĞùÙ•¹Ğ¡…Í ğ½‘Ğøñ‘ùí•Ù•¹Ğ¹•Ù•¹Ñ}¡…Í¡ôğ½‘øğ½‘¥Øøğ½‘°øğ½±¤ø¥ô(€€€€€€€€€€€€€€€€ğ½½°ø(€€€€€€€€€€€€€€ğ½‘¥Øø(€€€€€€€€€€€€¥ô(€€€€€€€€€€ğ½Í•Ñ¥½¸ø((€€€€€€€€€€ñ‘•Ñ…¥±Ì±…ÍÍ9…µ”ô‰…‘µ¥¸µÁ…¹•°…‘µ¥¸µÑ•¡¹¥…°µ‘•Ñ…¥±Ìˆø(€€€€€€€€€€€€ñÍÕµµ…ÉäùQ•¡¹¥…°‘•Ñ…¥±Ìğ½ÍÕµµ…Éäø(€€€€€€€€€€€€ñ‘°ø(€€€€€€€€€€€€€€ñ‘¥Øøñ‘Ğù5…¡¥¹”ÍÑ…Ñ”ğ½‘Ğøñ‘ùí‰Õ¥±¹ÕÉÉ•¹Ñ}ÍÑ…•ôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€ñ‘¥Øøñ‘Ğù]½É­™±½ÜÙ•ÉÍ¥½¸ğ½‘Ğøñ‘ùí‰Õ¥±¹Ù•ÉÍ¥½¹ôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€ñ‘¥Øøñ‘Ğù	Õ¥±%ğ½‘Ğøñ‘ùí‰Õ¥±¹¥‘ôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€íÑÉ…”€˜˜€ñ‘¥Øøñ‘ĞùÕÉÉ•¹ĞÉÕ¸%ğ½‘Ğøñ‘ùíÑÉ…”¹¥‘ôğ½‘øğ½‘¥Øùô(€€€€€€€€€€€€€íÁ•¹‘¥¹œ€˜˜€ñ‘¥Øøñ‘ĞùAÉ½Á½Í…°‰¥¹‘¥¹œğ½‘Ğøñ‘ùíÁ•¹‘¥¹œ¹ÁÉ½Á½Í…±}¡…Í¡ôğ½‘øğ½‘¥Øùô(€€€€€€€€€€€€ğ½‘°ø(€€€€€€€€€€ğ½‘•Ñ…¥±Ìø((€€€€€€€€€í•ÉÉ½ÉÌ¹±•¹Ñ €ø€À€˜˜€ñÍ•Ñ¥½¸±…ÍÍ9…µ”ô‰…‘µ¥¸µÁ…¹•°…‘µ¥¸µ•ÉÉ½ÉÌµÁ…¹•°ˆøñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µÁ…¹•°µ¡•…‘¥¹œˆøñ Èù]½É­™±½Ü•ÉÉ½ÉÌğ½ ÈøñÍÁ…¸ùí•ÉÉ½ÉÌ¹±•¹Ñ¡ôğ½ÍÁ…¸øğ½‘¥Øùí•ÉÉ½ÉÌ¹µ…À ¡¥Ñ•´¤€ôø€ñ…ÉÑ¥±”±…ÍÍ9…µ”ô‰…‘µ¥¸µ•ÉÉ½ÈµÉ½Üˆ­•äõí¥Ñ•´¹¥‘ôøñÍÑÉ½¹œùí¡Õµ…¹¥é•5…¡¥¹•Y…±Õ”¡¥Ñ•´¹½‘”¥ôğ½ÍÑÉ½¹œøñÀùí¥Ñ•´¹Í…™•}µ•ÍÍ…•ôğ½ÀøñÍÁ…¸ùí¥Ñ•´¹É•ÑÉå…‰±”€ü€‰I•ÑÉå…‰±”ˆ€è€‰Q•Éµ¥¹…°‰ôğ½ÍÁ…¸ùí¥Ñ•´¹‘•Ñ…¥°ü¹Í½ÕÉ•}‘¥…¹½ÍÑ¥Œ€˜˜€ñM½ÕÉ•¥…¹½ÍÑ¥•Ñ…¥±Ì‘¥…¹½ÍÑ¥Œõí¥Ñ•´¹‘•Ñ…¥°¹Í½ÕÉ•}‘¥…¹½ÍÑ¥ô€¼ùôğ½…ÉÑ¥±”ø¥ôğ½Í•Ñ¥½¸ùô((€€€€€€€€€í¥µÁ½ÉĞ¹µ•Ñ„¹•¹Ø¹X€˜˜€ (€€€€€€€€€€€€ñ‘•Ñ…¥±Ì±…ÍÍ9…µ”ô‰…‘µ¥¸µÁ…¹•°…‘µ¥¸µ‘•Ù•±½Á•ÈµÑ½½±Ìˆø(€€€€€€€€€€€€€€ñÍÕµµ…Éäù•Ù•±½Á•ÈÑ½½±Ì€ñÍÁ…¸ùQ•ÍĞ½¹±äğ½ÍÁ…¸øğ½ÍÕµµ…Éäø(€€€€€€€€€€€€€€ñÀùM¥µÕ±…Ñ¥½¸µ½¹±äÉ•½Ù•Éä½¹ÑÉ½±Ì¸Q¡•Í”…Ñ¥½¹Ì…É”¹½ĞÁ…ÉĞ½˜Ñ¡”ÁÉ½‘ÕÑ¥½¸İ½É­™±½Ü¸ğ½Àø(€€€€€€€€€€€€€í…¹…¥°€ü€ñ‰ÕÑÑ½¸±…ÍÍ9…µ”ô‰…‘µ¥¸µ‘…¹•Èµ½ÕÑ±¥¹”ˆ‘¥Í…‰±•õí‰ÕÍåô½¹±¥¬õì ¤€ôøÙ½¥½µµ…¹ ‰Í¥µÕ±…Ñ”µ™…¥±ÕÉ”ˆ¥ôùQÉ¥•È½¹ÑÉ½±±•™…¥±ÕÉ”ğ½‰ÕÑÑ½¸ø€è€ñÍÁ…¸ù9¼Ñ•ÍĞ…Ñ¥½¸¥ÌÙ…±¥¥¸Ñ¡¥ÌÍÑ…Ñ”¸ğ½ÍÁ…¸ùô(€€€€€€€€€€€€ğ½‘•Ñ…¥±Ìø(€€€€€€€€€€¥ô(€€€€€€€€ğ½…Í¥‘”ø(€€€€€€ğ½‘¥Øø((€€€€€í½¹™¥Éµ…Ñ¥½¸€˜˜€ (€€€€€€€€ñ‘µ¥¹5½‘…°(€€€€€€€€€Ñ¥Ñ±”õí½¹™¥Éµ…Ñ¥½¸¹­¥¹€ôôô€‰…ÁÁÉ½Ù”ˆ€ü€¡¥ÍÍÍ•µ‰±åÁÁÉ½Ù…°€ü€‰½¹™¥É´…ÍÍ•µ‰±äÍÑÉ…Ñ•ä…ÁÁÉ½Ù…°ˆ€è¥ÍMÁ•¥™¥…Ñ¥½¹ÁÁÉ½Ù…°€ü€‰½¹™¥É´Ñ…É•ĞÍÁ•¥™¥…Ñ¥½¸…ÁÁÉ½Ù…°ˆ€è€‰½¹™¥É´‘…Ñ…Í•Ğ…ÁÁÉ½Ù…°ˆ¤€è½¹™¥Éµ…Ñ¥½¸¹­¥¹€ôôô€‰…¹•°ˆ€ü€‰…¹•°Ñ¡¥Ìİ½É­™±½Üüˆ€è½¹™¥É´€‘í¡Õµ…¹¥é•5…¡¥¹•Y…±Õ”¡½¹™¥Éµ…Ñ¥½¸¹­¥¹¤¹Ñ½1½İ•É…Í” ¥õô(€€€€€€€€€‘•ÍÉ¥ÁÑ¥½¸õí½¹™¥Éµ…Ñ¥½¸¹­¥¹€ôôô€‰…ÁÁÉ½Ù”ˆ€ü€¡¥ÍÍÍ•µ‰±åÁÁÉ½Ù…°€ü€‰Q¡¥Ì…ÁÁÉ½Ù•Ì½¹±äÑ¡”Ù•É¥™¥•Í½ÕÉ”É…Á …¹ÁÉ•Á…É…Ñ¥½¸ÍÑÉ…Ñ•äì¥Ğ‘½•Ì¹½Ğ…ÕÑ¡½É¥é”ÑÉ…¥¹¥¹œ¸ˆ€è€‰I•Ù¥•ÜÑ¡”Í•±•Ñ•ÁÉ½Á½Í…°…¹…ÁÁÉ½Ù…°Í½Á”‰•™½É”½¹Ñ¥¹Õ¥¹œ¸ˆ¤€è€‰Q¡¥Ì‘•¥Í¥½¸İ¥±°‰”É•½É‘•¥¸Ñ¡”¥µµÕÑ…‰±”…Õ‘¥Ğ¡¥ÍÑ½Éä¸‰ô(€€€€€€€€€½¹±½Í”õì ¤€ôøÍ•Ñ½¹™¥Éµ…Ñ¥½¸¡¹Õ±°¥ô(€€€€€€€€€É•ÑÕÉ¹½ÕÌõí½¹™¥Éµ…Ñ¥½¹QÉ¥•È¹ÕÉÉ•¹Ñô(€€€€€€€€ø(€€€€€€€€€í½¹™¥Éµ…Ñ¥½¸¹­¥¹€ôôô€‰…¹•°ˆ€ü€ (€€€€€€€€€€€€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µ½¹™¥Éµ…Ñ¥½¸ˆø(€€€€€€€€€€€€€€ñÀù	Õ¥±íÍ¡½ÉÑ	Õ¥±‘%¡‰Õ¥±¹¥¥ôİ¥±°‰”…¹•±±•¸½µÁ±•Ñ•…ÉÑ¥™…ÑÌ…¹…Õ‘¥Ğ¡¥ÍÑ½Éäİ¥±°É•µ…¥¸…Ù…¥±…‰±”¸ğ½Àø(€€€€€€€€€€€€€€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µµ½‘…°µ…Ñ¥½¹Ìˆøñ‰ÕÑÑ½¸±…ÍÍ9…µ”ô‰…‘µ¥¸µÍ•½¹‘…Éäˆ½¹±¥¬õì ¤€ôøÍ•Ñ½¹™¥Éµ…Ñ¥½¸¡¹Õ±°¥ôù-••Àİ½É­™±½Üğ½‰ÕÑÑ½¸øñ‰ÕÑÑ½¸±…ÍÍ9…µ”ô‰…‘µ¥¸µ‘…¹•Èˆ‘¥Í…‰±•õí‰ÕÍåô½¹±¥¬õì ¤€ôøÙ½¥½µµ…¹ ‰…¹•°ˆ¥ôù…¹•°İ½É­™±½Üğ½‰ÕÑÑ½¸øğ½‘¥Øø(€€€€€€€€€€€€ğ½‘¥Øø(€€€€€€€€€€¤€è€ (€€€€€€€€€€€€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µ½¹™¥Éµ…Ñ¥½¸ˆø(€€€€€€€€€€€€€€ñ‘°ø(€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘ĞùM•±•Ñ•…¹‘¥‘…Ñ”ğ½‘Ğøñ‘ùíÍ•±•Ñ•‘…¹‘¥‘…Ñ”€ü€‘í…¹‘¥‘…Ñ•1…‰•°¡Í•±•Ñ•‘…¹‘¥‘…Ñ”°Í•±•Ñ•‘%¹‘•à¥ôƒ
Ü€‘íÍ•±•Ñ•‘…¹‘¥‘…Ñ”¹Ñ¥Ñ±•õ€€è€‰9¼…¹‘¥‘…Ñ”‰ôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘ĞùÙ¥‘•¹”‰¥¹‘¥¹œğ½‘Ğøñ‘ùÕÉÉ•¹Ğ¥µµÕÑ…‰±”…¹‘¥‘…Ñ”…ÉÑ¥™…Ğğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘ĞùÁÁÉ½Ù…°Í½Á”ğ½‘Ğøñ‘ùí¥ÍÍÍ•µ‰±åÁÁÉ½Ù…°€ü€‰Y•É¥™¥•Í½ÕÉ”É…Á …¹‘•Ñ•Éµ¥¹¥ÍÑ¥ŒÁÉ•Á…É…Ñ¥½¸ÍÑÉ…Ñ•ä½¹±äˆ€è¥ÍMÁ•¥™¥…Ñ¥½¹ÁÁÉ½Ù…°€ü€‰Q…É•ĞÑÉ…¥¹¥¹œµ‘…Ñ„½¹ÑÉ…Ğ½¹±äˆ€è€‰…Ñ…Í•ĞÍ•±•Ñ¥½¸™½ÈÑ¡¥Ì‰Õ¥±½¹±ä‰ôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€€€ñ‘¥Øøñ‘Ğù9•áĞÍÑ…”ğ½‘Ğøñ‘ùí½¹™¥Éµ…Ñ¥½¸¹­¥¹€ôôô€‰…ÁÁÉ½Ù”ˆ€ü€¡¥ÍÍÍ•µ‰±åÁÁÉ½Ù…°€ü€‰½µÁ±•Ñ”…ÍÍ•µ‰±äÉ•Ù¥•Üì½¹ÍÑÉÕÑ¥½¸…¹ÑÉ…¥¹¥¹œÉ•µ…¥¸‘•™•ÉÉ•ˆ€è¥ÍMÁ•¥™¥…Ñ¥½¹ÁÁÉ½Ù…°€ü€‰•É¥Ù”½µÁ½¹•¹ĞÉ•ÅÕ¥É•µ•¹ÑÌˆ€è€‰AÉ•Á…É”Ñ¡”Í•±•Ñ•‘…Ñ…Í•Ğˆ¤€è½¹™¥Éµ…Ñ¥½¸¹­¥¹€ôôô€‰É•ÅÕ•ÍÑ}É•Ù¥Í¥½¸ˆ€ü€‰I•Ù¥Í”Ñ¡”ÁÉ•Á…É•½µÁ…É¥Í½¸ˆ€è½¹™¥Éµ…Ñ¥½¸¹­¥¹€ôôô€‰¡½½Í•}…±Ñ•É¹…Ñ¥Ù”ˆ€ü€‰I•½ÉÑ¡”…±Ñ•É¹…Ñ¥Ù”Í•±•Ñ¥½¸ˆ€è€‰MÑ½ÀÑ¡¥ÌÁÉ½Á½Í…°‰ôğ½‘øğ½‘¥Øø(€€€€€€€€€€€€€€ğ½‘°ø(€€€€€€€€€€€€€í½µµ•¹Ğ€˜˜€ñÀøñÍÑÉ½¹œùI•Ù¥•İ•È½µµ•¹Ğèğ½ÍÑÉ½¹œøí½µµ•¹Ñôğ½Àùô(€€€€€€€€€€€€€€ñ‘¥Ø±…ÍÍ9…µ”ô‰…‘µ¥¸µµ½‘…°µ…Ñ¥½¹Ìˆøñ‰ÕÑÑ½¸±…ÍÍ9…µ”ô‰…‘µ¥¸µÍ•½¹‘…Éäˆ½¹±¥¬õì ¤€ôøÍ•Ñ½¹™¥Éµ…Ñ¥½¸¡¹Õ±°¥ôù¼‰…¬ğ½‰ÕÑÑ½¸øñ‰ÕÑÑ½¸±…ÍÍ9…µ”õí½¹™¥Éµ…Ñ¥½¸¹­¥¹€ôôô€‰É•©•Ğˆ€ü€‰…‘µ¥¸µ‘…¹•Èˆ€è€‰…‘µ¥¸µÁÉ¥µ…Éä‰ô‘¥Í…‰±•õí‰ÕÍåô½¹±¥¬õì ¤€ôø½¹™¥Éµ…Ñ¥½¸¹…ÁÁÉ½Ù…°€˜˜Ù½¥‘•¥‘”¡½¹™¥Éµ…Ñ¥½¸¹…ÁÁÉ½Ù…°°½¹™¥Éµ…Ñ¥½¸¹­¥¹…Ì•¥Í¥½¸¥ôù½¹™¥É´‘•¥Í¥½¸ğ½‰ÕÑÑ½¸øğ½‘¥Øø(€€€€€€€€€€€€ğ½‘¥Øø(€€€€€€€€€€¥ô(€€€€€€€€ğ½‘µ¥¹5½‘…°ø(€€€€€€¥ô(€€€€ğ½Í•Ñ¥½¸ø(€€¤ì)ô