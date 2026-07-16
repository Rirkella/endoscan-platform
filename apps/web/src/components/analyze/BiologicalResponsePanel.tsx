import { api } from "../../api/client";
import type {
  BiologicalPathwayCard,
  InputValueType,
  Signature,
} from "../../api/types";
import { useAsync } from "../../hooks/useAsync";
import { ErrorNotice } from "../ErrorNotice";

const INPUT_DESCRIPTIONS: Record<InputValueType, string> = {
  differential_zscore:
    "Biological response induced by the perturbation relative to the matched experimental control.",
  log2_fold_change:
    "Biological response represented by signed log2 fold changes relative to the supplied comparison.",
  ranked_statistic:
    "Biological response represented by the supplied signed ranking statistic.",
  raw_expression:
    "Raw expression values do not contain a direction relative to a matched reference.",
};

export function BiologicalResponsePanel({
  signature,
  inputValueType,
}: {
  signature: Signature;
  inputValueType: InputValueType;
}) {
  const state = useAsync(
    () => api.interpretBiologicalResponse(signature, inputValueType),
    [signature, inputValueType],
  );

  return (
    <section className="biological-response" aria-labelledby="biological-response-title">
      <header className="biological-response-header">
        <div>
          <p className="eyebrow">Full transcriptomic signature</p>
          <h2 id="biological-response-title">Which biological processes appear altered?</h2>
          <p>{INPUT_DESCRIPTIONS[inputValueType]}</p>
        </div>
        <span className="layer-badge">Endpoint-independent</span>
      </header>

      {state.loading && <p className="check-plain">Analyzing the ranked biological response…</p>}
      {state.error != null && <ErrorNotice error={state.error} />}
      {state.data && state.data.status !== "ok" && (
        <div className="biological-empty" role="status">
          <strong>Biological-response pathways are not available for this input.</strong>
          <p>{state.data.reason}</p>
        </div>
      )}
      {state.data?.status === "ok" && (
        <>
          <div className="response-directions">
            <PathwayDirection
              title="Increased biological response"
              tone="increased"
              pathways={state.data.increased_pathways}
            />
            <PathwayDirection
              title="Decreased biological response"
              tone="decreased"
              pathways={state.data.decreased_pathways}
            />
          </div>
          {state.data.method_block && (
            <details className="technical-disclosure">
              <summary>Method and technical details</summary>
              <dl className="technical-grid">
                <div><dt>Exact method</dt><dd>{state.data.method_block.method}</dd></div>
                <div><dt>Method version</dt><dd>{state.data.method_block.method_version}</dd></div>
                <div><dt>Ranking statistic</dt><dd>{state.data.method_block.ranking_statistic}</dd></div>
                <div><dt>Input value type</dt><dd>{state.data.method_block.input_value_type}</dd></div>
                <div><dt>Tested gene universe</dt><dd>{state.data.method_block.universe_size} genes</dd></div>
                <div><dt>Pathways tested</dt><dd>{state.data.method_block.pathways_tested}</dd></div>
                <div><dt>Multiple testing</dt><dd>{state.data.method_block.correction}</dd></div>
                <div>
                  <dt>Gene-set size</dt>
                  <dd>{state.data.method_block.min_gene_set_size}–{state.data.method_block.max_gene_set_size}</dd>
                </div>
              </dl>
            </details>
          )}
        </>
      )}
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
  const strongest = Math.max(1, ...pathways.map((item) => Math.abs(item.enrichment_statistic)));
  return (
    <section className={`response-direction response-${tone}`}>
      <h3>{title}</h3>
      {pathways.length === 0 && <p className="check-plain">No pathway tendency was returned.</p>}
      <div className="response-pathway-list">
        {pathways.slice(0, 8).map((pathway) => (
          <article key={pathway.pathway_id} className="response-pathway-card">
            <div className="response-pathway-title">
              <h4>{pathway.name}</h4>
              <span>{pathway.statistically_supported ? "FDR supported" : "Exploratory tendency"}</span>
            </div>
            <div className="pathway-strength" aria-label={`Enrichment statistic ${pathway.enrichment_statistic.toFixed(2)}`}>
              <span style={{ width: `${Math.max(5, Math.abs(pathway.enrichment_statistic) / strongest * 100)}%` }} />
            </div>
            <div className="pathway-stats">
              <span>Strength {Math.abs(pathway.enrichment_statistic).toFixed(2)}</span>
              <span>FDR {pathway.q_value.toPrecision(2)}</span>
            </div>
            <div className="gene-chips" aria-label="Leading-edge genes">
              {pathway.leading_edge_genes.slice(0, 5).map((gene) => <span key={gene}>{gene}</span>)}
            </div>
            <details>
              <summary>View details</summary>
              <p>Reactome {pathway.pathway_id} · {pathway.pathway_size_in_universe} genes in the tested universe.</p>
              <p>Signed rank enrichment statistic {pathway.enrichment_statistic.toFixed(3)}; p={pathway.p_value.toPrecision(3)}; q={pathway.q_value.toPrecision(3)}.</p>
            </details>
          </article>
        ))}
      </div>
    </section>
  );
}
