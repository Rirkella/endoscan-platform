import { useState } from "react";

import type { ExplanationResult, GeneAttribution } from "../api/types";
import { GeneAnnotation } from "./GeneAnnotation";

const METHOD_LABELS: Record<string, string> = {
  tree_shap: "TreeSHAP",
  linear_coefficient: "Linear coefficient × input value",
};

export function GeneContributionCards({ explanation }: { explanation: ExplanationResult }) {
  const [showAll, setShowAll] = useState(false);
  const sorted = [...explanation.top_contributors].sort(
    (left, right) => Math.abs(right.shap_value) - Math.abs(left.shap_value),
  );
  const strongest = Math.max(1e-12, ...sorted.map((item) => Math.abs(item.shap_value)));
  const visible = showAll ? sorted : sorted.slice(0, 10);
  const increases = visible.filter((item) => item.direction === "toward");
  const decreases = visible.filter((item) => item.direction !== "toward");

  return (
    <div className="gene-contributions">
      <div className="gene-contribution-heading">
        <div>
          <h3>Genes contributing to this endpoint signal</h3>
          <p>Ranked by absolute model contribution, not by biological causality.</p>
        </div>
        <span>{METHOD_LABELS[explanation.method] ?? explanation.method}</span>
      </div>
      <div className="contribution-directions">
        <ContributionGroup
          title="Increases this model’s signal"
          tone="increase"
          genes={increases}
          strongest={strongest}
          rankOffset={sorted}
          method={explanation.method}
        />
        <ContributionGroup
          title="Decreases this model’s signal"
          tone="decrease"
          genes={decreases}
          strongest={strongest}
          rankOffset={sorted}
          method={explanation.method}
        />
      </div>
      {sorted.length > 10 && (
        <button className="detail-link" type="button" onClick={() => setShowAll((value) => !value)}>
          {showAll ? "Show leading contributors" : "Show all contributing genes"}
        </button>
      )}
      <details className="technical-disclosure">
        <summary>Attribution method details</summary>
        <p>
          {METHOD_LABELS[explanation.method] ?? explanation.method}; model baseline {explanation.base_value.toFixed(3)};
          {" "}{explanation.n_features} input genes. Bars are normalized to the strongest contributor in this result.
        </p>
      </details>
    </div>
  );
}

function ContributionGroup({
  title,
  tone,
  genes,
  strongest,
  rankOffset,
  method,
}: {
  title: string;
  tone: "increase" | "decrease";
  genes: GeneAttribution[];
  strongest: number;
  rankOffset: GeneAttribution[];
  method: string;
}) {
  return (
    <section className={`contribution-group contribution-${tone}`}>
      <h4>{title}</h4>
      {genes.length === 0 && <p className="check-plain">No leading genes in this direction.</p>}
      <ol>
        {genes.map((gene) => {
          const rank = rankOffset.findIndex((item) => item.gene === gene.gene) + 1;
          const relative = Math.abs(gene.shap_value) / strongest;
          return (
            <li key={gene.gene}>
              <span className="contribution-rank">{rank}</span>
              <strong>{gene.gene}</strong>
              <div className="contribution-bar" aria-label={`Relative contribution ${Math.round(relative * 100)} of 100`}>
                <span style={{ width: `${Math.max(3, relative * 100)}%` }} />
              </div>
              <span className="contribution-direction">{tone === "increase" ? "Increases" : "Decreases"}</span>
              <details>
                <summary>Gene details</summary>
                <p>{method === "tree_shap" ? "TreeSHAP" : "Model"} contribution: {gene.shap_value >= 0 ? "+" : ""}{gene.shap_value.toFixed(4)}</p>
                <GeneAnnotation gene={gene.gene} />
              </details>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
