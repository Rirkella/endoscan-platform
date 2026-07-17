import { useMemo, useState } from "react";

import type { Signature } from "../../api/types";

type SortMode = "absolute" | "signed";

export function GeneLevelResponse({ signature }: { signature: Signature }) {
  const [expanded, setExpanded] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [query, setQuery] = useState("");
  const [sortMode, setSortMode] = useState<SortMode>("absolute");
  const rows = useMemo(
    () => Object.entries(signature).map(([gene, value]) => ({ gene, value })),
    [signature],
  );
  const filtered = rows
    .filter((row) => row.gene.toLowerCase().includes(query.trim().toLowerCase()))
    .sort((left, right) => sortMode === "absolute"
      ? Math.abs(right.value) - Math.abs(left.value) || left.gene.localeCompare(right.gene)
      : right.value - left.value || left.gene.localeCompare(right.gene));
  const positive = filtered.filter((row) => row.value >= 0);
  const negative = filtered.filter((row) => row.value < 0).sort((left, right) => left.value - right.value);
  const limit = showAll ? filtered.length : 10;
  const ranked = [...rows].sort((left, right) => Math.abs(right.value) - Math.abs(left.value));
  const rank = new Map(ranked.map((row, index) => [row.gene, index + 1]));
  const csv = ["rank,gene,signed_value", ...ranked.map((row, index) => `${index + 1},${row.gene},${row.value}`)].join("\n");

  return (
    <section className="gene-level-response">
      <button className="gene-level-toggle" type="button" onClick={() => setExpanded((value) => !value)}>
        <span><strong>Gene-level response</strong><small>Inspect the measured values even when no pathway passes FDR.</small></span>
        <span>{expanded ? "Hide" : "Expand"}</span>
      </button>
      {expanded && (
        <div className="gene-level-content">
          <p>Values are ranked measurements, not gene-level statistical significance. For LINCS differential z-scores, larger absolute values indicate stronger differential measurements.</p>
          <div className="gene-level-tools">
            <label>Search gene<input value={query} onChange={(event) => setQuery(event.target.value)} /></label>
            <label>Sort<select value={sortMode} onChange={(event) => setSortMode(event.target.value as SortMode)}><option value="absolute">Absolute value</option><option value="signed">Signed value</option></select></label>
            <a download="endoscan-ranked-gene-response.csv" href={`data:text/csv;charset=utf-8,${encodeURIComponent(csv)}`}>Download ranked gene table</a>
          </div>
          <div className="gene-level-directions">
            <GeneValueList title="Strongest positive differential values" rows={positive.slice(0, limit)} rank={rank} />
            <GeneValueList title="Strongest negative differential values" rows={negative.slice(0, limit)} rank={rank} />
          </div>
          {filtered.length > 10 && <button className="detail-link" type="button" onClick={() => setShowAll((value) => !value)}>{showAll ? "Show top 10" : "Show all genes"}</button>}
        </div>
      )}
    </section>
  );
}

function GeneValueList({ title, rows, rank }: { title: string; rows: Array<{ gene: string; value: number }>; rank: Map<string, number> }) {
  return (
    <section><h3>{title}</h3><ol>{rows.map((row) => <li key={row.gene}><span>{rank.get(row.gene)}</span><strong>{row.gene}</strong><span className="tabular">{row.value >= 0 ? "+" : ""}{row.value.toFixed(4)}</span><small>{category(row.value)}</small></li>)}</ol></section>
  );
}

function category(value: number): string {
  const magnitude = Math.abs(value);
  const strength = magnitude >= 2 ? "strong" : magnitude >= 1 ? "moderate" : "near-zero";
  if (strength === "near-zero") return "near zero";
  return `${strength} ${value >= 0 ? "positive" : "negative"} differential value`;
}
