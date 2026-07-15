// Biological pathways for an explain result. The VISIBLE layer speaks biology (pathway names, the
// genes from THIS result, an evidence label); the statistics (p, adjusted p, test, universe,
// mapping, Reactome version/license) live one level deeper in Technical details. Framing is
// strict: these are genes that INFLUENCED THIS RESULT (model-contributing) — never "affected"/
// "perturbed" genes, and a pathway is a clue for investigation, never proof the compound acts
// through it. Empty / too-few / unavailable are all honest, plain-language states — never faked.

import { useEffect } from "react";

import { api } from "../api/client";
import type { PathwayCard, PathwaysResponse, Signature } from "../api/types";
import { useAsync } from "../hooks/useAsync";
import { ErrorNotice } from "./ErrorNotice";

const EVIDENCE_STYLE: Record<string, string> = {
  High: "bg-brand text-white",
  Medium: "bg-amber-100 text-amber-800",
  Low: "bg-slate-100 text-slate-600",
};

function EvidenceBadge({ label }: { label: string }) {
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${EVIDENCE_STYLE[label] ?? "bg-slate-100 text-slate-600"}`}
    >
      {label} evidence
    </span>
  );
}

function PathwayCardView({ card }: { card: PathwayCard }) {
  return (
    <li className="rounded-md border border-line bg-white p-3">
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm font-medium text-ink">{card.name}</p>
        <EvidenceBadge label={card.evidence} />
      </div>
      {card.description && <p className="mt-1 text-xs text-muted">{card.description}</p>}
      <p className="mt-2 text-[11px] font-medium text-ink">
        Genes from this result in this pathway
      </p>
      <div className="mt-1 flex flex-wrap gap-1">
        {card.genes_influencing_result.map((g) => (
          <span key={g} className="rounded bg-surface px-1.5 py-0.5 font-mono text-[11px] text-ink">
            {g}
          </span>
        ))}
      </div>
      <p className="mt-2 text-[11px] text-muted">Source: Reactome</p>

      {/* Technical details — one level deeper. The banned statistical terms live ONLY here. */}
      <details className="mt-2" data-testid="pathway-technical">
        <summary className="cursor-pointer text-[11px] font-medium text-ink">
          Technical details
        </summary>
        <dl className="mt-1 space-y-0.5 text-[11px] text-muted">
          <div>
            raw p-value {card.p_value.toExponential(2)} · adjusted p (BH FDR){" "}
            {card.q_value.toExponential(2)} · overlap {card.overlap_count} of{" "}
            {card.pathway_size_in_universe} pathway genes in the universe
          </div>
        </dl>
      </details>
    </li>
  );
}

export function PathwaysPanel({
  endpointId,
  signature,
  onResult,
}: {
  endpointId: string;
  signature: Signature;
  onResult?: (result: PathwaysResponse) => void;
}) {
  const state = useAsync(() => api.interpretPathways(endpointId, signature), [endpointId, signature]);

  useEffect(() => {
    if (state.data) onResult?.(state.data);
  }, [state.data, onResult]);

  return (
    <section className="mt-3 rounded-md border border-line bg-surface p-3" data-testid="pathways-panel">
      <h4 className="text-sm font-semibold text-ink">Biological pathways</h4>

      {/* Always-visible honest framing (both captions required, never in Technical details). */}
      <p className="mt-1 text-xs text-muted" data-testid="pathways-framing">
        Genes that influenced this result are involved in these biological pathways. These pathways
        are clues for further investigation, not proof that the compound acts through them.
      </p>
      <p className="mt-1 text-[11px] text-muted" data-testid="pathways-landmark-caption">
        Pathway analysis uses the 978 landmark genes EndoScan&rsquo;s models see, not the full
        transcriptome. Broad pathways may appear simply because they contain many landmark genes.
      </p>

      <div className="mt-2">
        {state.loading && <p className="text-xs text-muted">Looking for related pathways…</p>}
        {state.error != null && <ErrorNotice error={state.error} />}

        {state.data?.status === "unavailable" && (
          <p className="text-xs text-muted">Pathway information isn&rsquo;t available for this result yet.</p>
        )}
        {state.data?.status === "too_few_genes" && (
          <p className="text-xs text-muted">
            Too few contributing genes for reliable pathway analysis.
          </p>
        )}
        {state.data?.status === "ok" && state.data.pathways.length === 0 && (
          <p className="text-xs text-muted">No pathways met the evidence threshold for this result.</p>
        )}
        {state.data?.status === "ok" && state.data.pathways.length > 0 && (
          <ul className="mt-1 space-y-2">
            {state.data.pathways.map((c) => (
              <PathwayCardView key={c.pathway_id} card={c} />
            ))}
          </ul>
        )}

        {/* Section-level Technical details: the method block (input rule, test, universe, mapping,
            Reactome version/license) — the only place the statistical vocabulary appears. */}
        {state.data?.method_block && (
          <details className="mt-2" data-testid="pathways-method-block">
            <summary className="cursor-pointer text-[11px] font-medium text-ink">
              Technical details — method
            </summary>
            <dl className="mt-1 space-y-0.5 text-[11px] text-muted">
              <div>Input genes: {state.data.method_block.input_gene_rule}.</div>
              <div>
                Toward-signal genes: {state.data.method_block.n_toward_genes} (pinned top{" "}
                {state.data.method_block.pinned_top_n} contributors).
              </div>
              <div>Test: {state.data.method_block.test}.</div>
              <div>Correction: {state.data.method_block.correction}.</div>
              <div>
                Universe: {state.data.method_block.universe_size} genes (978 landmark ∩ Reactome);
                minimum {state.data.method_block.min_pathway_overlap} landmark genes per pathway;{" "}
                {state.data.method_block.family_size} pathways tested.
              </div>
              <div>
                Evidence mapping:{" "}
                {Object.entries(state.data.method_block.evidence_mapping)
                  .map(([k, v]) => `${k}: ${v}`)
                  .join(" · ")}
                .
              </div>
              {state.data.method_block.reactome && (
                <div>
                  Reactome: version {String(state.data.method_block.reactome.reactome_version)} ·
                  license {String(state.data.method_block.reactome.license)}.
                </div>
              )}
            </dl>
          </details>
        )}
      </div>
    </section>
  );
}
