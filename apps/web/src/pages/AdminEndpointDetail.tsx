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
      <div className="admin-panel-heading"><h2>Review target training dataset</h2><span>Compiler {String(compilation.compiler_version ?? "1.0.0")}</span></div>
      <p><strong>Draft produced deterministically from the request and approved platform contract.</strong></p>
      <p>Deterministic hash: <code>{String(compilation.deterministic_hash ?? "not available")}</code></p>
    </section>
    <section className="admin-panel">
      <div className="admin-panel-heading"><h2>Request interpretation</h2><span>Source-neutral</span></div>
      <dl className="admin-candidate-facts">
        <div><dt>Target</dt><dd>{String(draft.biological_target ?? "Unresolved")} <small>{provenanceFor("biological_target")}</small></dd></div>
        <div><dt>Modality</dt><dd>{humanizeMachineValue(String(draft.endpoint_modality ?? "unresolved"))} <small>{provenanceFor("endpoint_modality")}</small></dd></div>
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
      <div className="admin-panel-heading"><h2>Scientific scope</h2><span>{humanizeMachineValue(String(draft.endpoint_modality ?? "unresolved"))}</span></div>
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
      <CompiledSpecificationWorkspace data={data} />
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

function candidateInspectionCounts(run: AdminAgentRun): { inspected: nu×7öÚ$z{-®éÜj×Cå&÷f–FW#ÂöGCãÆFCç·G&6Rç&÷f–FW'ÓÂöFCãÂöF—cà¢ÆF—cãÆGCäÖöFVÃÂöGCãÆFCç·G&6RæÖöFVÅö–FVçF–f–W'ÓÂöFCãÂöF—cà¢ÆF—cãÆGCä–çWBFö¶Vç3ÂöGCãÆFCç·W6vTçVÖ&W"‡G&6RÂ&–çWE÷Fö¶Vç2"—ÓÂöFCãÂöF—cà¢ÆF—cãÆGCä÷WGWBFö¶Vç3ÂöGCãÆFCç·W6vTçVÖ&W"‡G&6RÂ&÷WGWE÷Fö¶Vç2"—ÓÂöFCãÂöF—cà¢ÆF—cãÆGCä66†VB–çWBFö¶Vç3ÂöGCãÆFCç·W6vTçVÖ&W"‡G&6RÂ&66†VE÷Fö¶Vç2"—ÓÂöFCãÂöF—cà¢ÆF—cãÆGCäW7F–ÖFVB6÷7CÂöGCãÆFCç·G&6RçW6vRçW6vU÷7FGW2ÓÓÒ'W6vU÷Væf–Æ&ÆR"ò%W6vRVæf–Æ&ÆRgFW"7G'V7GW&VBÖ÷WGWBf–ÇW&R"¢BG²‡W6vTçVÖ&W"‡G&6RÂ&6÷7Eö6VçG2"’ò’çFôf—†VBƒB—ÖÓÂöFCãÂöF—cà¢ÂöFÃà¢¶vVõ6V&6†W2æÆVæwF‚âbbÆF—b6Æ74æÖSÒ&FÖ–â×G&6R×7VÖÖ'’#ãÆƒCå&VæFW&VBtTòVW&–W3ÂöƒCãÆöÃç¶vVõ6V&6†W2æÖ‚‡6V&6‚Â–æFW‚’ÓâÆÆ’¶W“×¶G·6V&6‚ç&VæFW&VEVW'—ÒÒG¶–æFW‡ÖÓãÇ7G&öæsç·6V&6‚ç7G&FVw•&V6öçÓÂ÷7G&öæsãÆ6öFSç·6V&6‚ç&VæFW&VEVW'—ÓÂö6öFSãÇ7ãç·6V&6‚ç&W7VÇD6÷VçGÒ&W7VÇG2ò·6V&6‚æ66†U7FGW7ÓÂ÷7ããÂöÆ“â—ÓÂööÃãÂöF—cçĞ¢·GW&äW‡÷7W&W2æÆVæwF‚âbbÆF—b6Æ74æÖSÒ&FÖ–â×G&6R×7VÖÖ'’#ãÆƒCåFööÇ2W‡÷6VBW"GW&ãÂöƒCãÆöÃç·GW&äW‡÷7W&W2æÖ‚‡GW&â’ÓâÆÆ’¶W“×¶G·GW&âçGW&çÒÒG·GW&âç7V'7FvWÖÓãÇ7G&öæsåGW&â·GW&âçGW&çÓ¢¶‡VÖæ—¦TÖ6†–æUfÇVR‡GW&âç7V'7FvR—ÓÂ÷7G&öæsãÇ7ãç·GW&âçFööÇ2æÆVæwF‚âòGW&âçFööÇ2æÖ‡FööÄÆ&VÂ’æ¦ö–â‚"Â"’¢$f–æÂ7G'V7GW&VB÷WGWBöæÇ’'ÓÂ÷7ããÂöÆ“â—ÓÂööÃãÂöF—cçĞ¢¶æ÷&ÖÆ—¦F–öåv&æ–æt6÷VçBâbbÇ6Æ74æÖSÒ&FÖ–â×6V6öæF'’Öæ÷FR#ç¶6öçG&öÆÆVEfö6'VÆ'•v&æ–æt6÷VçBâòG¶6öçG&öÆÆVEfö6'VÆ'•v&æ–æt6÷VçGÒ6öçG&öÆÆVB×fö6'VÆ'’G¶6öçG&öÆÆVEfö6'VÆ'•v&æ–æt6÷VçBÓÓÒò'fÇVRv2"¢'fÇVW2vW&R'Òæ÷&ÖÆ—¦VB&Vf÷&RW†V7WF–öâæ¢V×G”÷F–öæÄf–ÇFW%v&æ–æt6÷VçBÓÓÒæ÷&ÖÆ—¦F–öåv&æ–æt6÷VçBòG¶V×G”÷F–öæÄf–ÇFW%v&æ–æt6÷VçGÒV×G’÷F–öæÂG¶V×G”÷F–öæÄf–ÇFW%v&æ–æt6÷VçBÓÓÒò&f–ÇFW"v2"¢&f–ÇFW'2vW&R'Ò&VÖ÷fVB&Vf÷&RW†V7WF–öâæ¢G¶æ÷&ÖÆ—¦F–öåv&æ–æt6÷VçGÒ÷F–öæÂf–ÇFW"G¶æ÷&ÖÆ—¦F–öåv&æ–æt6÷VçBÓÓÒò'fÇVRv2"¢'fÇVW2vW&R'Ò6fVÇ’æ÷&ÖÆ—¦VB&Vf÷&RW†V7WF–öâæÓÂ÷çĞ¢¶FWfVÆ÷W$F–væ÷7F–2bbÆF—b6Æ74æÖSÒ&FÖ–â×G&6R×7VÖÖ'’#ãÆƒCå6fRF–væ÷7F–3ÂöƒCãÇç¶FWfVÆ÷W$F–væ÷7F–7ÓÂ÷ãÂöF—cçĞ¢²†÷WGWDF–væ÷7F–2ÇÂ÷WGWDf–ævW'&–çBÇÂ—57V6–f–6F–öå&Wf—6–öâ’bb€¢ÆFWF–Ç26Æ74æÖSÒ&FÖ–â×G&6R×7VÖÖ'’#à¢Ç7VÖÖ'“åFV6†æ–6ÂVF—CÂ÷7VÖÖ'“à¢ÆFÃà¢¶÷WGWDF–væ÷7F–2bbÃà¢ÆF—cãÆGCäf–ÇW&R6FVv÷'“ÂöGCãÆFCç¶‡VÖæ—¦TÖ6†–æUfÇVR…7G&–ær†÷WGWDF–væ÷7F–2æf–ÇW&Uö6Æ76–f–6F–öâóò'Væ¶æ÷våöÖöFVÅö&V†f–÷""’—ÓÂöFCãÂöF—cà¢ÆF—cãÆGCå66†VÖÂöGCãÆFCçµ7G&–ær†÷WGWDF–væ÷7F–2æ÷WGWE÷66†VÖöæÖRóò'Væ¶æ÷vâ"—Ò+rµ7G&–ær†÷WGWDF–væ÷7F–2æ÷WGWE÷66†VÖ÷fW'6–öâóò'Væ¶æ÷vâ"—ÓÂöFCãÂöF—cà¢ÂóçĞ¢¶÷WGWDf–ævW'&–çBbbÃà¢ÆF—cãÆGCä÷WGWBG—SÂöGCãÆFCçµ7G&–ær†÷WGWDf–ævW'&–çBæ÷WGWE÷G—UöæÖRóò'Væ¶æ÷vâ"—Ò+r¶÷WGWDf–ævW'&–çBæ÷WGWE÷G—U÷&W6VçBÓÓÒG'VRò'&W6VçB"¢&Ö—76–ær'ÓÂöFCãÂöF—cà¢ÆF—cãÆGCå7G'V7GW&VB“ÂöGCãÆFCç¶‡VÖæ—¦TÖ6†–æUfÇVR…7G&–ær†÷WGWDf–ævW'&–çBæ•÷7W&f6Róò'Væ¶æ÷vâ"’—Ò+r¶÷WGWDf–ævW'&–çBç7G&–7Eö§6öå÷66†VÖÓÓÒG'VRò'7G&–7B66†VÖ"¢&æöâ×7G&–7B66†VÖ'ÓÂöFCãÂöF—cà¢ÆF—cãÆGCå66†VÖ†6ƒÂöGCãÆFCãÆ6öFSçµ7G&–ær†÷WGWDf–ævW'&–çBç66†VÖö†6‚óò'Væ¶æ÷vâ"—ÓÂö6öFSãÂöFCãÂöF—cà¢ÆF—cãÆGCä&÷VæF'’÷'VçF–ÖSÂöGCãÆFCç¶÷WGWDf–ævW'&–çBæ&÷VæF'•÷'VçF–ÖUö6öçG&7G5öÖF6‚ÓÓÒG'VRò$ÖF6†VB"¢$Ö—6ÖF6‚(	BW†V7WF–öâ&Æö6¶VB'ÓÂöFCãÂöF—cà¢ÂóçĞ¢¶÷WGWDF–væ÷7F–2bbÃà¢ÆF—cãÆGCå&W7öç6R6†SÂöGCãÆFCçµ7G&–ær†÷WGWDF–væ÷7F–2æ÷WGWEö—FVÕö6÷VçBóò—Ò—FV×2+r·FW‡DÆ—7B†÷WGWDF–væ÷7F–2æ÷WGWEö—FVÕ÷G—W2’æ¦ö–â‚"Â"’ÇÂ&æò÷WGWB—FV×2'ÓÂöFCãÂöF—cà¢ÆF—cãÆGCåFW‡Bò¥4ôãÂöGCãÆFCç¶÷WGWDF–væ÷7F–2çFW‡Eö÷WGWE÷&W6VçBÓÓÒG'VRòGµ7G&–ær†÷WGWDF–væ÷7F–2æ&÷VæFVE÷FW‡EöÆVæwF‚óò—ÒFW‡B6†&7FW'6¢&æòFW‡B'Ò+r¶÷WGWDF–væ÷7F–2æ§6öåöö&¦V7E÷&W6VçBÓÓÒG'VRò$¥4ôâö&¦V7B&W6VçB"¢&æò¥4ôâö&¦V7B'ÓÂöFCãÂöF—cà¢ÂóçĞ¢ÆF—cãÆGCå&WVW7B”G3ÂöGCãÆFCç´'&’æ—4'&’†÷WGWDF–væ÷7F–3òç&÷f–FW%÷&WVW7Eö–G2’bb÷WGWDF–væ÷7F–2ç&÷f–FW%÷&WVW7Eö–G2æÆVæwF‚ò÷WGWDF–væ÷7F–2ç&÷f–FW%÷&WVW7Eö–G2æ¦ö–â‚"Â"’¢FW‡DÆ—7B‡G&6RçW6vRç&÷f–FW%÷&WVW7Eö–G2’æ¦ö–â‚"Â"’ÇÂ&æ÷Bf–Æ&ÆR'ÓÂöFCãÂöF—cà¢ÆF—cãÆGCå&W7öç6R”G3ÂöGCãÆFCç´'&’æ—4'&’†÷WGWDF–væ÷7F–3òç&÷f–FW%÷&W7öç6Uö–G2’bb÷WGWDF–væ÷7F–2ç&÷f–FW%÷&W7öç6Uö–G2æÆVæwF‚ò÷WGWDF–væ÷7F–2ç&÷f–FW%÷&W7öç6Uö–G2æ¦ö–â‚"Â"’¢FW‡DÆ—7B‡G&6RçW6vRç&÷f–FW%÷&W7öç6Uö–G2’æ¦ö–â‚"Â"’ÇÂ&æ÷Bf–Æ&ÆR'ÓÂöFCãÂöF—cà¢ÆF—cãÆGCåW6vR7FGW3ÂöGCãÆFCç¶‡VÖæ—¦TÖ6†–æUfÇVR…7G&–ær‚†÷WGWDF–væ÷7F–3òçW6vR2&V6÷&CÇ7G&–ærÂVæ¶æ÷vãâÂVæFVf–æVB“òçW6vU÷7FGW2óòG&6RçW6vRçW6vU÷7FGW2óò'W6vU÷Væf–Æ&ÆR"’—ÓÂöFCãÂöF—cà¢¶÷WGWDF–væ÷7F–2bbÆF—cãÆGCä†æFÆW#ÂöGCãÆFCç¶‡VÖæ—¦TÖ6†–æUfÇVR…7G&–ær†÷WGWDF–væ÷7F–2æW'&÷%ö†æFÆW"óò&æöæR"’—ÓÂöFCãÂöF—cçĞ¢ÂöFÃà¢ÂöFWF–Ç3à¢—Ğ¢·66–VçF–f–56÷W&6TF–væ÷7F–72æÆVæwF‚âbbÆF—b6Æ74æÖSÒ&FÖ–â×G&6R×7VÖÖ'’#ãÆƒCå66–VçF–f–2×6÷W&6RF–væ÷7F–73ÂöƒCç·66–VçF–f–56÷W&6TF–væ÷7F–72æÖ‚†F–væ÷7F–2Â–æFW‚’ÓâÅ6÷W&6TF–væ÷7F–4FWF–Ç2F–væ÷7F–3×¶F–væ÷7F–7Ò¶W“×¶G¶F–væ÷7F–2çFööÅöæÖWÒÒG¶–æFW‡ÖÒóâ—ÓÂöF—cçĞ¢ÆöÂ–CÒ&vVçB×FööÇ2"6Æ74æÖSÒ&FÖ–â×FööÂÖÆ—7B#ç¶vVçEFööÇ2æÖ‚‡FööÂ’ÓâÆÆ’¶W“×·FööÂæ–GÓãÇ7ãç·FööÄÆ&VÂ‡FööÂçFööÅöæÖR—ÓÂ÷7ããÇ6ÖÆÃç·FööÂç7FGW7ÓÂ÷6ÖÆÃãÂöÆ“â—ÓÂööÃà¢ÆF—b6Æ74æÖSÒ&FÖ–âÖ–æÆ–æRÖ7F–öç2#à¢Æ'WGFöâ6Æ74æÖSÒ&FÖ–âÖÆ–æ²Ö'WGFöâ"öä6Æ–6³×²‚’Óâ6WE6†÷uG&6R‚‡fÇVR’ÓâfÇVR—Óç·6†÷uG&6Rò$†–FRG&6R"¢%f–WrG&6R'ÓÂö'WGFöãà¢Æ‡&VcÒ"6vVçB×FööÇ2#åf–WrFööÇ2W6VCÂöà¢Æ‡&VcÒ"6'F–f7G2#åf–WrvVæW&FVB'F–f7CÂöà¢ÂöF—cà¢·6†÷uG&6RbbÆF—b6Æ74æÖSÒ&FÖ–â×G&6R×7VÖÖ'’"–CÒ&vVçB×G&6R#ãÆƒCåG&6RWfVçG3ÂöƒCãÆöÃç²‡G&6RçG&6SòæWfVçG2óòµÒ’æÖ‚†WfVçBÂ–æFW‚’ÓâÆÆ’¶W“×¶–æFW‡Óç·G—VöbWfVçBæWfVçE÷G—RÓÓÒ'7G&–ær"ò‡VÖæ—¦TÖ6†–æUfÇVR†WfVçBæWfVçE÷G—Rç&WÆ6R‚õÂâörÂ%ò"’’¢G&6RWfVçBG¶–æFW‚²ÖÓÂöÆ“â—ÓÂööÃç¶æ÷&ÖÆ—¦F–öå7VÖÖ&–W2æÖ‚‡7VÖÖ'’Â–æFW‚’ÓâÇ6V7F–öâ¶W“×¶G·7VÖÖ'’çFööÄæÖWÒÒG¶–æFW‡ÖÓãÆƒSç·FööÄÆ&VÂ‡7VÖÖ'’çFööÄæÖR—Ò–çWBæ÷&ÖÆ—¦F–öãÂöƒSãÇç·7VÖÖ'’çv&æ–æt6öFW2æÆVæwF‡Òv&æ–æw·7VÖÖ'’çv&æ–æt6öFW2æÆVæwF‚ÓÓÒò""¢'2'Ó¢·7VÖÖ'’çv&æ–æt6öFW2æ¦ö–â‚"Â"—ÓÂ÷ãÆFÃãÆF—cãÆGCäÖöFVÂ×7WÆ–VB&wVÖVçG3ÂöGCãÆFCãÆ6öFSç´¥4ôâç7G&–æv–g’‡7VÖÖ'’æ÷&–v–æÄ&wVÖVçG2—ÓÂö6öFSãÂöFCãÂöF—cãÆF—cãÆGCäW†V7WFVB&wVÖVçG3ÂöGCãÆFCãÆ6öFSç´¥4ôâç7G&–æv–g’‡7VÖÖ'’ææ÷&ÖÆ—¦VD&wVÖVçG2—ÓÂö6öFSãÂöFCãÂöF—cãÂöFÃãÇVÂ6Æ74æÖSÒ&FÖ–âÖæ÷&ÖÆ—¦F–öâÖÆ—7B#ç·7VÖÖ'’çv&æ–æw2æÖ‚‡v&æ–ærÂv&æ–æt–æFW‚’ÓâÆÆ’¶W“×¶G·v&æ–æræ6öFWÒÒG·v&æ–æræf–VÆGÒÒG·v&æ–æt–æFW‡ÖÓãÇ7G&öæsç·v&æ–æræf–VÆGÓÂ÷7G&öæsãÇ7ãç·v&æ–æræ÷&–v–æÂóò&æ÷B&V6÷&FVB'Ò(i"·v&æ–ærææ÷&ÖÆ—¦VBóò&æ÷BW†V7WFVB'ÓÂ÷7ããÇ6ÖÆÃç·v&æ–æræ6öFW×·v&æ–ærçöÆ–7•fW'6–öâò+rG·v&æ–ærçöÆ–7•fW'6–öçÖ¢"'ÓÂ÷6ÖÆÃãÂöÆ“â—ÓÂ÷VÃãÂ÷6V7F–öãâ—ÓÂöF—cçĞ¢ÂöF—cà¢’¢ÆF—b6Æ74æÖSÒ&FÖ–âÖV×G’#äæòvVçB'Vâ–WBãÂöF—cçĞ¢Â÷6V7F–öãà ¢Ç6V7F–öâ6Æ74æÖSÒ&FÖ–â×æVÂ"–CÒ&'F–f7G2"&–ÖÆ&VÆÆVF'“Ò&'F–f7G2×F—FÆR#à¢ÆF—b6Æ74æÖSÒ&FÖ–â×æVÂÖ†VF–ær#ãÆƒ"–CÒ&'F–f7G2×F—FÆR#ävVæW&FVB'F–f7G3Âöƒ#ãÇ7ãç¶'F–f7G2æÆVæwF‡ÓÂ÷7ããÂöF—cà¢ÆF—b6Æ74æÖSÒ&FÖ–âÖ'F–f7BÖÆ—7B#ç¶'F–f7G2æÖ‚†'F–f7B’ÓâÆ'F–6ÆR¶W“×¶'F–f7Bæ–GÓãÆF—cãÇ7G&öæsç¶'F–f7BæÆöv–6ÅöæÖWÓÂ÷7G&öæsãÇ7ãç¶‡VÖæ—¦TÖ6†–æUfÇVR†'F–f7Bæ'F–f7E÷G—R—Ò+r¶'F–f7Bç&öGV6W'ÓÂ÷7ããÂöF—cãÂö'F–6ÆSâ—ÓÂöF—cà¢Â÷6V7F–öãà ¢Ç6V7F–öâ6Æ74æÖSÒ&FÖ–â×æVÂ"–CÒ'FV6†æ–6ÂÖVF—B"&–ÖÆ&VÆÆVF'“Ò&7F—f—G’×F—FÆR#à¢ÆF—b6Æ74æÖSÒ&FÖ–â×F'2"&öÆSÒ'F&Æ—7B"&–ÖÆ&VÃÒ$'V–ÆB†—7F÷'’#à¢Æ'WGFöâ–CÒ&7F—f—G’×F""&öÆSÒ'F""&–×6VÆV7FVC×¶7F—f—G•F"ÓÓÒ&7F—f—G’'Ò&–Ö6öçG&öÇ3Ò&7F—f—G’×æVÂ"F$–æFWƒ×¶7F—f—G•F"ÓÓÒ&7F—f—G’"ò¢ÓÒöä¶W”F÷vã×¶†æFÆUF$¶W—Òöä6Æ–6³×²‚’Óâ6WD7F—f—G•F"‚&7F—f—G’"—Óä7F—f—G“Âö'WGFöãà¢Æ'WGFöâ–CÒ&VF—B×F""&öÆSÒ'F""&–×6VÆV7FVC×¶7F—f—G•F"ÓÓÒ&VF—B'Ò&–Ö6öçG&öÇ3Ò&VF—B×æVÂ"F$–æFWƒ×¶7F—f—G•F"ÓÓÒ&VF—B"ò¢ÓÒöä¶W”F÷vã×¶†æFÆUF$¶W—Òöä6Æ–6³×²‚’Óâ6WD7F—f—G•F"‚&VF—B"—ÓåFV6†æ–6ÂVF—BÆösÂö'WGFöãà¢ÂöF—cà¢¶7F—f—G•F"ÓÓÒ&7F—f—G’"ò€¢ÆF—b–CÒ&7F—f—G’×æVÂ"&öÆSÒ'F'æVÂ"&–ÖÆ&VÆÆVF'“Ò&7F—f—G’×F"#à¢Æƒ"–CÒ&7F—f—G’×F—FÆR"6Æ74æÖSÒ'7"ÖöæÇ’#ä7F—f—G“Âöƒ#à¢ÆöÂ6Æ74æÖSÒ&FÖ–âÖ7F—f—G’ÖÆ—7B#à¢¶F—7Æ”WfVçG2æÖ‚†WfVçB’ÓâÆÆ’¶W“×¶WfVçBæ–GÓãÇ7â6Æ74æÖSÒ&FÖ–âÖ7F—f—G’ÖÖ&¶W""&–Ö†–FFVâóãÆF—cãÇ7G&öæsç¶7F—f—G”Æ&VÂ†WfVçB—ÓÂ÷7G&öæsãÇç¶7F÷$Æ&VÂ†WfVçBæ7F÷%÷G—R—Ò+r¶æWrFFR†WfVçBæ7&VFVEöB’çFôÆö6ÆU7G&–ær‚—ÓÂ÷ãÂöF—cãÂöÆ“â—Ğ¢ÂööÃà¢¶WfVçG2æÆVæwF‚âbbbÆ'WGFöâ6Æ74æÖSÒ&FÖ–âÖÆ–æ²Ö'WGFöâ"öä6Æ–6³×²‚’Óâ6WDgVÆÄ7F—f—G’‚‡fÇVR’ÓâfÇVR—Óç¶gVÆÄ7F—f—G’ò%6†÷r&V6VçB7F—f—G’"¢%6†÷rgVÆÂ7F—f—G’'ÓÂö'WGFöãçĞ¢ÂöF—cà¢’¢€¢ÆF—b–CÒ&VF—B×æVÂ"&öÆSÒ'F'æVÂ"&–ÖÆ&VÆÆVF'“Ò&VF—B×F"#à¢ÆFÂ6Æ74æÖSÒ&FÖ–âÖVF—BÖ–FVçF–f–W'2#à¢ÆF—cãÆGCä'V–ÆB”CÂöGCãÆFCç¶'V–ÆBæ–GÓÂöFCãÂöF—cà¢·G&6RbbÆF—cãÆGCä7W'&VçB'Vâ”CÂöGCãÆFCç·G&6Ræ–GÓÂöFCãÂöF—cçĞ¢ÂöFÃà¢ÆöÂ6Æ74æÖSÒ&FÖ–âÖVF—BÖÆ—7B#à¢µ²ââæWfVçG5Òç&WfW'6R‚’æÖ‚†WfVçB’ÓâÆÆ’¶W“×¶WfVçBæ–GÓãÆF—cãÇ7G&öæsç¶WfVçBæWfVçE÷G—WÓÂ÷7G&öæsãÇF–ÖSç¶æWrFFR†WfVçBæ7&VFVEöB’çFôÆö6ÆU7G&–ær‚—ÓÂ÷F–ÖSãÂöF—cãÆFÃãÆF—cãÆGCåG&ç6—F–öãÂöGCãÆFCç¶WfVçBæg&öÕ÷7FFRóò&æöæR'ÒFò¶WfVçBçFõ÷7FFRóò&æöæR'ÓÂöFCãÂöF—cãÆF—cãÆGCä7F÷#ÂöGCãÆFCç¶WfVçBæ7F÷%÷G—WÒò¶WfVçBæ7F÷%ö–GÓÂöFCãÂöF—cãÆF—cãÆGCäWfVçB”CÂöGCãÆFCç¶WfVçBæ–GÓÂöFCãÂöF—cãÆF—cãÆGCäWfVçB†6ƒÂöGCãÆFCç¶WfVçBæWfVçEö†6‡ÓÂöFCãÂöF—cãÂöFÃãÂöÆ“â—Ğ¢ÂööÃà¢ÂöF—cà¢—Ğ¢Â÷6V7F–öãà ¢ÆFWF–Ç26Æ74æÖSÒ&FÖ–â×æVÂFÖ–â×FV6†æ–6ÂÖFWF–Ç2#à¢Ç7VÖÖ'“åFV6†æ–6ÂFWF–Ç3Â÷7VÖÖ'“à¢ÆFÃà¢ÆF—cãÆGCäÖ6†–æR7FFSÂöGCãÆFCç¶'V–ÆBæ7W'&VçE÷7FvWÓÂöFCãÂöF—cà¢ÆF—cãÆGCåv÷&¶fÆ÷rfW'6–öãÂöGCãÆFCç¶'V–ÆBçfW'6–öçÓÂöFCãÂöF—cà¢ÆF—cãÆGCä'V–ÆB”CÂöGCãÆFCç¶'V–ÆBæ–GÓÂöFCãÂöF—cà¢·G&6RbbÆF—cãÆGCä7W'&VçB'Vâ”CÂöGCãÆFCç·G&6Ræ–GÓÂöFCãÂöF—cçĞ¢·VæF–ærbbÆF—cãÆGCå&÷÷6Â&–æF–æsÂöGCãÆFCç·VæF–ærç&÷÷6Åö†6‡ÓÂöFCãÂöF—cçĞ¢ÂöFÃà¢ÂöFWF–Ç3à ¢¶W'&÷'2æÆVæwF‚âbbÇ6V7F–öâ6Æ74æÖSÒ&FÖ–â×æVÂFÖ–âÖW'&÷'2×æVÂ#ãÆF—b6Æ74æÖSÒ&FÖ–â×æVÂÖ†VF–ær#ãÆƒ#åv÷&¶fÆ÷rW'&÷'3Âöƒ#ãÇ7ãç¶W'&÷'2æÆVæwF‡ÓÂ÷7ããÂöF—cç¶W'&÷'2æÖ‚†—FVÒ’ÓâÆ'F–6ÆR6Æ74æÖSÒ&FÖ–âÖW'&÷"×&÷r"¶W“×¶—FVÒæ–GÓãÇ7G&öæsç¶‡VÖæ—¦TÖ6†–æUfÇVR†—FVÒæ6öFR—ÓÂ÷7G&öæsãÇç¶—FVÒç6fUöÖW76vWÓÂ÷ãÇ7ãç¶—FVÒç&WG'–&ÆRò%&WG'–&ÆR"¢%FW&Ö–æÂ'ÓÂ÷7ãç¶—FVÒæFWF–Ãòç6÷W&6UöF–væ÷7F–2bbÅ6÷W&6TF–væ÷7F–4FWF–Ç2F–væ÷7F–3×¶—FVÒæFWF–Âç6÷W&6UöF–væ÷7F–7ÒóçÓÂö'F–6ÆSâ—ÓÂ÷6V7F–öãçĞ ¢¶–×÷'BæÖWFæVçbäDUbbb€¢ÆFWF–Ç26Æ74æÖSÒ&FÖ–â×æVÂFÖ–âÖFWfVÆ÷W"×FööÇ2#à¢Ç7VÖÖ'“äFWfVÆ÷W"FööÇ2Ç7ãåFW7BöæÇ“Â÷7ããÂ÷7VÖÖ'“à¢Çå6–×VÆF–öâÖöæÇ’&V6÷fW'’6öçG&öÇ2âF†W6R7F–öç2&Ræ÷B'BöbF†R&öGV7F–öâv÷&¶fÆ÷rãÂ÷à¢¶6äf–ÂòÆ'WGFöâ6Æ74æÖSÒ&FÖ–âÖFævW"Ö÷WFÆ–æR"F—6&ÆVC×¶'W7—Òöä6Æ–6³×²‚’Óâfö–B6öÖÖæB‚'6–×VÆFRÖf–ÇW&R"—ÓåG&–vvW"6öçG&öÆÆVBf–ÇW&SÂö'WGFöãâ¢Ç7ãäæòFW7B7F–öâ—2fÆ–B–âF†—27FFRãÂ÷7ãçĞ¢ÂöFWF–Ç3à¢—Ğ¢Âö6–FSà¢ÂöF—cà ¢¶6öæf—&ÖF–öâbb€¢ÄFÖ–äÖöFÀ¢F—FÆS×¶6öæf—&ÖF–öâæ¶–æBÓÓÒ&&÷fR"ò†—476VÖ&Ç”&÷fÂò$6öæf—&Ò76VÖ&Ç’7G&FVw’&÷fÂ"¢—57V6–f–6F–öä&÷fÂò$6öæf—&ÒF&vWB7V6–f–6F–öâ&÷fÂ"¢$6öæf—&ÒFF6WB&÷fÂ"’¢6öæf—&ÖF–öâæ¶–æBÓÓÒ&6æ6VÂ"ò$6æ6VÂF†—2v÷&¶fÆ÷sò"¢6öæf—&ÒG¶‡VÖæ—¦TÖ6†–æUfÇVR†6öæf—&ÖF–öâæ¶–æB’çFôÆ÷vW$66R‚—ÖĞ¢FW67&—F–öã×¶6öæf—&ÖF–öâæ¶–æBÓÓÒ&&÷fR"ò†—476VÖ&Ç”&÷fÂò%F†—2&÷fW2öæÇ’F†RfW&–f–VB6÷W&6Rw&‚æB&W&F–öâ7G&FVw“²—BFöW2æ÷BWF†÷&—¦RG&–æ–ærâ"¢%&Wf–WrF†R6VÆV7FVB&÷÷6ÂæB&÷fÂ66÷R&Vf÷&R6öçF–çV–ærâ"’¢%F†—2FV6—6–öâv–ÆÂ&R&V6÷&FVB–âF†R–Ö×WF&ÆRVF—B†—7F÷'’â'Ğ¢öä6Æ÷6S×²‚’Óâ6WD6öæf—&ÖF–öâ†çVÆÂ—Ğ¢&WGW&äfö7W3×¶6öæf—&ÖF–öåG&–vvW"æ7W'&VçGĞ¢à¢¶6öæf—&ÖF–öâæ¶–æBÓÓÒ&6æ6VÂ"ò€¢ÆF—b6Æ74æÖSÒ&FÖ–âÖ6öæf—&ÖF–öâ#à¢Çä'V–ÆB·6†÷'D'V–ÆD–B†'V–ÆBæ–B—Òv–ÆÂ&R6æ6VÆÆVBâ6ö×ÆWFVB'F–f7G2æBVF—B†—7F÷'’v–ÆÂ&VÖ–âf–Æ&ÆRãÂ÷à¢ÆF—b6Æ74æÖSÒ&FÖ–âÖÖöFÂÖ7F–öç2#ãÆ'WGFöâ6Æ74æÖSÒ&FÖ–â×6V6öæF'’"öä6Æ–6³×²‚’Óâ6WD6öæf—&ÖF–öâ†çVÆÂ—Óä¶VWv÷&¶fÆ÷sÂö'WGFöããÆ'WGFöâ6Æ74æÖSÒ&FÖ–âÖFævW""F—6&ÆVC×¶'W7—Òöä6Æ–6³×²‚’Óâfö–B6öÖÖæB‚&6æ6VÂ"—Óä6æ6VÂv÷&¶fÆ÷sÂö'WGFöããÂöF—cà¢ÂöF—cà¢’¢€¢ÆF—b6Æ74æÖSÒ&FÖ–âÖ6öæf—&ÖF–öâ#à¢ÆFÃà¢ÆF—cãÆGCå6VÆV7FVB6æF–FFSÂöGCãÆFCç·6VÆV7FVD6æF–FFRòG¶6æF–FFTÆ&VÂ‡6VÆV7FVD6æF–FFRÂ6VÆV7FVD–æFW‚—Ò+rG·6VÆV7FVD6æF–FFRçF—FÆWÖ¢$æò6æF–FFR'ÓÂöFCãÂöF—cà¢ÆF—cãÆGCäWf–FVæ6R&–æF–æsÂöGCãÆFCä7W'&VçB–Ö×WF&ÆR6æF–FFR'F–f7CÂöFCãÂöF—cà¢ÆF—cãÆGCä&÷fÂ66÷SÂöGCãÆFCç¶—476VÖ&Ç”&÷fÂò%fW&–f–VB6÷W&6Rw&‚æBFWFW&Ö–æ—7F–2&W&F–öâ7G&FVw’öæÇ’"¢—57V6–f–6F–öä&÷fÂò%F&vWBG&–æ–ærÖFF6öçG&7BöæÇ’"¢$FF6WB6VÆV7F–öâf÷"F†—2'V–ÆBöæÇ’'ÓÂöFCãÂöF—cà¢ÆF—cãÆGCäæW‡B7FvSÂöGCãÆFCç¶6öæf—&ÖF–öâæ¶–æBÓÓÒ&&÷fR"ò†—476VÖ&Ç”&÷fÂò$6ö×ÆWFR76VÖ&Ç’&Wf–Ws²6öç7G'V7F–öâæBG&–æ–ær&VÖ–âFVfW'&VB"¢—57V6–f–6F–öä&÷fÂò$FW&—fR6ö×öæVçB&WV—&VÖVçG2"¢%&W&RF†R6VÆV7FVBFF6WB"’¢6öæf—&ÖF–öâæ¶–æBÓÓÒ'&WVW7E÷&Wf—6–öâ"ò%&Wf—6RF†R&W&VB6ö×&—6öâ"¢6öæf—&ÖF–öâæ¶–æBÓÓÒ&6†ö÷6UöÇFW&æF—fR"ò%&V6÷&BF†RÇFW&æF—fR6VÆV7F–öâ"¢%7F÷F†—2&÷÷6Â'ÓÂöFCãÂöF—cà¢ÂöFÃà¢¶6öÖÖVçBbbÇãÇ7G&öæså&Wf–WvW"6öÖÖVçC£Â÷7G&öæsâ¶6öÖÖVçGÓÂ÷çĞ¢ÆF—b6Æ74æÖSÒ&FÖ–âÖÖöFÂÖ7F–öç2#ãÆ'WGFöâ6Æ74æÖSÒ&FÖ–â×6V6öæF'’"öä6Æ–6³×²‚’Óâ6WD6öæf—&ÖF–öâ†çVÆÂ—Óävò&6³Âö'WGFöããÆ'WGFöâ6Æ74æÖS×¶6öæf—&ÖF–öâæ¶–æBÓÓÒ'&V¦V7B"ò&FÖ–âÖFævW""¢&FÖ–â×&–Ö'’'ÒF—6&ÆVC×¶'W7—Òöä6Æ–6³×²‚’Óâ6öæf—&ÖF–öâæ&÷fÂbbfö–BFV6–FR†6öæf—&ÖF–öâæ&÷fÂÂ6öæf—&ÖF–öâæ¶–æB2FV6—6–öâ—Óä6öæf—&ÒFV6—6–öãÂö'WGFöããÂöF—cà¢ÂöF—cà¢—Ğ¢ÂôFÖ–äÖöFÃà¢—Ğ¢Â÷6V7F–öãà¢“°§Ğ