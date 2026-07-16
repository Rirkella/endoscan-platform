import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { LiteratureArticle, LiteraturePathwayInput, LiteratureResponse } from "../api/types";
import { ErrorNotice } from "./ErrorNotice";

export function LiteraturePanel({
  endpointId,
  genes,
  pathways,
  compound,
  initialResult,
  onResult,
  onError,
}: {
  endpointId: string;
  genes: string[];
  pathways: LiteraturePathwayInput[];
  compound?: string;
  initialResult?: LiteratureResponse | null;
  onResult?: (result: LiteratureResponse) => void;
  onError?: (error: unknown) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{ data: LiteratureResponse | null; error: unknown; loading: boolean }>({
    data: initialResult ?? null,
    error: null,
    loading: !initialResult,
  });

  useEffect(() => {
    if (initialResult && attempt === 0) {
      setState({ data: initialResult, error: null, loading: false });
      return;
    }
    let alive = true;
    setState((current) => ({ data: attempt > 0 ? current.data : null, error: null, loading: true }));
    api.interpretLiterature(endpointId, genes, pathways, compound)
      .then((data) => {
        if (!alive) return;
        setState({ data, error: null, loading: false });
        onResult?.(data);
      })
      .catch((error) => {
        if (!alive) return;
        setState((current) => ({ data: current.data, error, loading: false }));
        onError?.(error);
      });
    return () => { alive = false; };
    // Stable scalar keys keep cached records from repeating PubMed requests on remount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempt, endpointId, genes.join(","), JSON.stringify(pathways), compound]);
  const articles = state.data?.articles ?? [];
  const visible = showAll ? articles : articles.slice(0, 5);

  return (
    <section className="literature-bibliography" data-testid="literature-panel">
      <div className="literature-heading"><h3>Supporting literature</h3></div>
      {state.loading && <p className="check-plain">Finding supporting literature…</p>}
      {state.error != null && <ErrorNotice error={state.error} />}
      {state.error != null && <button className="detail-link" type="button" onClick={() => setAttempt((value) => value + 1)}>Refresh literature</button>}
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
      {state.data && !state.error && <button className="detail-link" type="button" onClick={() => setAttempt((value) => value + 1)}>Refresh literature</button>}
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
