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
  const state = useAsync(
    () => api.interpretLiterature(endpointId, genes, pathways, compound),
    [endpointId, genes.join(","), JSON.stringify(pathways), compound],
  );
  const articles = state.data?.articles ?? [];
  const visible = showAll ? articles : articles.slice(0, 5);

  return (
    <section className="literature-bibliography" data-testid="literature-panel">
      <div className="literature-heading"><h3>Supporting literature</h3></div>
      {state.loading && <p className="check-plain">Finding supporting literature…</p>}
      {state.error != null && <ErrorNotice error={state.error} />}
      {state.data && state.data.status !== "ok" && (
        <p className="check-plain">{state.data.reason ?? "No eligible supporting publications were found."}</p>
      )}
      {state.data?.status === "ok" && (
        <ol className="bibliography-list">
          {visible.map((article) => <BibliographyEntry key={article.pmid} article={article} />)}
        </ol>
      )}
      {articles.length > 5 && (
        <button className="detail-link" type="button" onClick={() => setShowAll((value) => !value)}>
          {showAll ? "Show strongest articles" : "Show more"}
        </button>
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
      <a href={article.pubmed_url} target="_blank" rel="noreferrer">View in PubMed</a>
    </li>
  );
}
