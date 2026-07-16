import { useState } from "react";

import { api } from "../api/client";
import type { LiteratureArticle, LiteraturePathwayInput } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { ErrorNotice } from "./ErrorNotice";

export function LiteraturePanel({
  endpointId,
  genes,
  pathways,
  compound,
}: {
  endpointId: string;
  genes: string[];
  pathways: LiteraturePathwayInput[];
  compound?: string;
}) {
  const [showAll, setShowAll] = useState(false);
  const pathwayKey = JSON.stringify(pathways);
  const geneKey = genes.join(",");
  const state = useAsync(
    () => api.interpretLiterature(endpointId, genes, pathways, compound),
    [endpointId, geneKey, pathwayKey, compound],
  );
  const visible = showAll ? state.data?.articles ?? [] : (state.data?.articles ?? []).slice(0, 5);

  return (
    <section className="literature-bibliography" data-testid="literature-panel">
      <div className="literature-heading">
        <div>
          <h3>Traceable supporting literature</h3>
          <p>Publications are included only when they directly connect entities already displayed in this result.</p>
        </div>
        <span className="layer-badge">Closed evidence graph</span>
      </div>
      <p className="literature-disclaimer">
        These publications provide biological context for entities identified by the current
        analysis. They do not establish that the model attribution or pathway association is causal.
      </p>

      <div aria-live="polite">
        {state.loading && <p className="check-plain">Searching PubMed with traceable relationships…</p>}
        {state.error != null && <ErrorNotice error={state.error} />}
        {state.data && state.data.status !== "ok" && (
          <p className="check-plain">{state.data.reason ?? "No directly connected publications were found."}</p>
        )}
        {state.data?.status === "ok" && (
          <ol className="bibliography-list">
            {visible.map((article) => <BibliographyEntry key={article.pmid} article={article} />)}
          </ol>
        )}
      </div>

      {(state.data?.articles.length ?? 0) > 5 && (
        <button className="detail-link" type="button" onClick={() => setShowAll((value) => !value)}>
          {showAll ? "Show strongest articles" : "Show more"}
        </button>
      )}
      {state.data && (
        <details className="technical-disclosure" data-testid="literature-technical">
          <summary>Method and technical details</summary>
          <p>{state.data.provenance.provider} · retrieved {state.data.provenance.retrieved_at} · cache {state.data.provenance.cache_hit ? "hit" : "miss"}</p>
          <ol>
            {state.data.queries.map((query, index) => (
              <li key={`${query.category}-${index}`}><code>{query.query}</code></li>
            ))}
          </ol>
        </details>
      )}
    </section>
  );
}

function BibliographyEntry({ article }: { article: LiteratureArticle }) {
  return (
    <li>
      <h4>{article.title}</h4>
      <p className="bibliography-citation">
        {article.journal || "Journal unavailable"} · {article.year || "Year unavailable"} · PMID {article.pmid}
      </p>
      <strong className="literature-relationship">{article.displayed_relationship}</strong>
      <span className="evidence-category">{article.evidence_category}</span>
      <div className="bibliography-actions">
        <a href={article.pubmed_url} target="_blank" rel="noreferrer">View in PubMed</a>
        {article.abstract_excerpt && (
          <details>
            <summary>Show abstract</summary>
            <p>{article.abstract_excerpt}</p>
          </details>
        )}
      </div>
      <details className="literature-trace">
        <summary>Why this article is included</summary>
        <p>{article.relevance_reason}</p>
        <p>Endpoint concept: {article.endpoint_concept_used}</p>
        <p>Title terms: {article.matched_title_terms.join(", ") || "none"}; abstract terms: {article.matched_abstract_terms.join(", ") || "none"}.</p>
        <p>{article.ranking_reason}</p>
      </details>
    </li>
  );
}
