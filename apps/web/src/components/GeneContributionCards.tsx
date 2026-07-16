import { useState } from "react";

import type { ExplanationResult } from "../api/types";
import { GeneAnnotation } from "./GeneAnnotation";

export function GeneContributionCards({ explanation }: { explanation: ExplanationResult }) {
  const [showAll, setShowAll] = useState(false);
  const sorted = [...explanation.top_contributors].sort(
    (left, right) => Math.abs(right.shap_value) - Math.abs(left.shap_value),
  );
  const strongest = Math.max(1e-12, ...sorted.map((item) => Math.abs(item.shap_value)));
  const visible = showAll ? sorted : sorted.slice(0, 10);

  return (
    <div className="gene-contributions">
      <div className="gene-contribution-heading">
        <div>
          <h3>Genes contributing to this endpoint signal</h3>
          <p>One global ranking by absolute model contribution. Bars are relative within this result, not biological causality.</p>
        </div>
      </div>
      <ol className="contribution-ranking" aria-label="Ranked gene contributions">
        {visible.map((gene, index) => {
          const positive = gene.shap_value >= 0;
          const relative = Math.abs(gene.shap_value) / strongest;
          return (
            <li key={gene.gene}>
              <span className="contribution-rank">{index + 1}</span>
              <details className="gene-details">
                <summary><strong>{gene.gene}</strong></summary>
                <GeneAnnotation gene={gene.gene} />
              </details>
              <span className={`signed-contribution ${positive ? "positive" : "negative"}`}>
                {positive ? "+" : ""}{gene.shap_value.toFixed(4)}
              </span>
              <div className="diverging-contribution" aria-label={`Relative model contribution ${Math.round(relative * 100)} of 100`}>
                <span className="contribution-zero" />
                <span
                  className={positive ? "bar-positive" : "bar-negative"}
                  style={{ width: `${Math.max(2, relative * 50)}%` }}
                />
              </div>
              <span className="contribution-direction">
                {positive ? "increases score" : "decreases score"}
              </span>
            </li>
          );
        })}
      </ol>
      {sorted.length > 10 && (
        <button className="detail-link" type="button" onClick={() => setShowAll((value) => !value)}>
          {showAll ? "Show leading contributors" : "Show all contributing genes"}
        </button>
      )}
    </div>
  );
}
