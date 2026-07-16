import { api } from "../../api/client";
import type { BiologicalPathwayCard, InputValueType, Signature } from "../../api/types";
import { useAsync } from "../../hooks/useAsync";
import { ErrorNotice } from "../ErrorNotice";
import { GeneLevelResponse } from "./GeneLevelResponse";

const INPUT_DESCRIPTIONS: Record<InputValueType, string> = {
  differential_zscore:
    "This analysis is independent of the ER and AR models. Genes are ranked by their LINCS Level 5 differential z-scores, calculated upstream using the corresponding experimental controls. EndoScan does not select a control. Reactome pathways are highlighted when their genes cluster toward either end of that ranked response.",
  log2_fold_change:
    "Genes are ranked by signed log2 fold change relative to the supplied comparison; pathway enrichment is independent of the endpoint models.",
  ranked_statistic:
    "Genes are ordered by the supplied signed statistic; pathway enrichment is independent of the endpoint models.",
  raw_expression:
    "A directional pathway analysis requires a differential signature or matched control.",
};

export function BiologicalResponsePanel({
  signature,
  inputValueType,
  aggregationWarning,
}: {
  signature: Signature;
  inputValueType: InputValueType;
  aggregationWarning?: string;
}) {
  const state = useAsync(
    () => api.interpretBiologicalResponse(signature, inputValueType),
    [signature, inputValueType],
  );

  const supported = state.data?.status === "ok"
    ? [...state.data.increased_pathways, ...state.data.decreased_pathways]
      .filter((item) => item.statistically_supported && item.q_value < 0.05)
    : [];

  return (
    <section className="biological-response" aria-labelledby="biological-response-title">
      <header className="biological-response-header">
        <div>
          <p className="eyebrow">Full transcriptomic signature</p>
          <h2 id="biological-response-title">Pathways enriched in the full gene-expression response</h2>
          <p>{INPUT_DESCRIPTIONS[inputValueType]}</p>
        </div>
      </header>
      {aggregationWarning && <p className="aggregate-warning">{aggregationWarning}</p>}

      {state.loading && <p className="check-plain">Analyzing the ranked biological response…</p>}
      {state.error != null && <ErrorNotice error={state.error} />}
      {state.data && (state.data.status !== "ok" || supported.length === 0) && (
        <div className="biological-empty" role="status">
          <p>{inputValueType === "raw_expression"
            ? "A directional pathway analysis requires a differential signature or matched control."
            : "No Reactome pathway reached the current FDR threshold for this signature."}</p>
          {inputValueType !== "raw_expression" && <p>Individual genes may still show differential values, but their changes did not form a statistically supported Reactome pathway pattern after multiple-testing correction.</p>}
        </div>
      )}
      {state.data?.status === "ok" && supported.length > 0 && (
        <div className="response-directions">
          <PathwayDirection
            title="Pathways enriched among genes with increased expression"
            tone="increased"
            pathways={state.data.increased_pathways.filter((item) => item.statistically_supported && item.q_value < 0.05)}
          />
          <PathwayDirection
            title="Pathways enriched among genes with decreased expression"
            tone="decreased"
            pathways={state.data.decreased_pathways.filter((item) => item.statistically_supported && item.q_value < 0.05)}
          />
        </div>
      )}
      <GeneLevelResponse signature={signature} />
    </section>
  );
}

function PathwayDirection({
  title,
  tone,
  pathways,
}: {
  title: string;
  tone: "increased" | "decreased";
  pathways: BiologicalPathwayCard[];
}) {
  if (pathways.length === 0) return null;
  const strongest = Math.max(1, ...pathways.map((item) => Math.abs(item.enrichment_statistic)));
  return (
    <section className={`response-direction response-${tone}`}>
      <h3>{title}</h3>
      <div className="response-pathway-list">
        {pathways.slice(0, 8).map((pathway) => (
          <article key={pathway.pathway_id} className="response-pathway-card">
            <div className="response-pathway-title">
              <h4>{pathway.name}</h4>
              <span>Enriched among genes with {tone} expression</span>
            </div>
            <div className="pathway-strength" aria-label={`Enrichment score ${pathway.enrichment_statistic.toFixed(2)}`}>
              <span style={{ width: `${Math.max(5, Math.abs(pathway.enrichment_statistic) / strongest * 100)}%` }} />
            </div>
            <div className="pathway-stats">
              <span>Enrichment score {pathway.enrichment_statistic.toFixed(2)}</span>
              <span>FDR {formatFdr(pathway.q_value)}</span>
            </div>
            <div className="gene-chips" aria-label="Leading genes">
              {pathway.leading_edge_genes.slice(0, 5).map((gene) => <span key={gene}>{gene}</span>)}
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

function formatFdr(value: number): string {
  return value < 0.001 ? "< 0.001" : value.toFixed(3);
}
