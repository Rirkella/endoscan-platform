// Fills the gene card's annotation slot: deterministic external links (always) + a SOURCED
// description with attribution IF the artifact has a real entry, else "annotation unavailable".
// Links assert nothing; descriptions are never generated (only from the committed artifact).

import { geneDescription, geneLinks } from "../gene-annotations";

export function GeneAnnotation({ gene }: { gene: string }) {
  const links = geneLinks(gene);
  const desc = geneDescription(gene);

  return (
    <div data-annotation-slot="" className="mt-1.5 space-y-1">
      <div className="flex flex-wrap gap-x-3 gap-y-0.5">
        {links.map((l) => (
          <a
            key={l.label}
            href={l.url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-brand underline"
          >
            {l.label}
          </a>
        ))}
      </div>
      {desc ? (
        <p className="text-xs text-muted">
          {desc.description}{" "}
          <span className="italic">
            (source: {desc.source} · {desc.source_version})
          </span>
        </p>
      ) : (
        <p className="text-xs text-muted/70">annotation unavailable</p>
      )}
    </div>
  );
}
