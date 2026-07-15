import { api } from "../api/client";
import type { LiteraturePathwayInput } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { ErrorNotice } from "./ErrorNotice";

export function LiteraturePanel({
  endpointId,
  genes,
  pathways,
}: {
  endpointId: string;
  genes: string[];
  pathways: LiteraturePathwayInput[];
}) {
  const pathwayKey = JSON.stringify(pathways);
  const geneKey = genes.join(",");
  const state = useAsync(
    () => api.interpretLiterature(endpointId, genes, pathways),
    [endpointId, geneKey, pathwayKey],
  );

  return (
    <section className="mt-3 rounded-md border border-line bg-surface p-3" data-testid="literature-panel">
      <h4 className="text-sm font-semibold text-ink">Supporting literature</h4>
      <p className="mt-1 text-xs text-muted">
        PubMed records matched to contributing genes, enriched pathways, and this endpoint. These
        links provide context, not proof of causality for this result.
      </p>

      <div className="mt-2" aria-live="polite">
        {state.loading && <p className="text-xs text-muted">Searching PubMed…</p>}
        {state.error != null && <ErrorNotice error={state.error} />}
        {state.data && state.data.status !== "ok" && (
          <p className="text-xs text-muted">{state.data.reason ?? "No supporting records found."}</p>
        )}
        {state.data?.status === "ok" && (
          <ul className="space-y-2">
            {state.data.articles.map((article) => (
              <li key={article.pmid} className="rounded-md border border-line bg-white p-3">
                <a
                  className="text-sm font-medium text-brand underline"
                  href={article.pubmed_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  {article.title}
                </a>
                <p className="mt-1 text-[11px] text-muted">
                  {article.authors.slice(0, 3).join(", ")}
                  {article.journal ? ` · ${article.journal}` : ""}
                  {article.year ? ` · ${article.year}` : ""} · PMID {article.pmid}
                </p>
                {article.abstract_excerpt && (
                  <p className="mt-2 text-xs text-ink">{article.abstract_excerpt}</p>
                )}
                <p className="mt-2 text-[11px] text-muted">{article.relevance_reason}</p>
              </li>
            ))}
          </ul>
        )}
      </div>

      {state.data && (
        <details className="mt-2" data-testid="literature-technical">
          <summary className="cursor-pointer text-[11px] font-medium text-ink">
            Technical details — exact PubMed queries and provenance
          </summary>
          <div className="mt-1 space-y-1 text-[11px] text-muted">
            <p>
              {state.data.provenance.provider} · retrieved {state.data.provenance.retrieved_at} ·
              cache {state.data.provenance.cache_hit ? "hit" : "miss"}
            </p>
            <ol className="list-decimal space-y-1 pl-4">
              {state.data.queries.map((query, index) => (
                <li key={`${query.category}-${index}`} className="break-words font-mono">
                  {query.query}
                </li>
              ))}
            </ol>
          </div>
        </details>
      )}
    </section>
  );
}
