// TIER 1 — deterministic external links, built purely from the gene SYMBOL at render time. A
// link to a gene's page asserts NOTHING (no biological claim), so it is honest by construction
// and needs no dataset. Every gene card shows these.

export interface GeneLink {
  label: string;
  url: string;
}

export function geneLinks(symbol: string): GeneLink[] {
  const s = encodeURIComponent(symbol);
  const ncbiTerm = encodeURIComponent(`${symbol}[sym] AND Homo sapiens[orgn]`);
  return [
    { label: "GeneCards", url: `https://www.genecards.org/cgi-bin/carddisp.pl?gene=${s}` },
    { label: "NCBI Gene", url: `https://www.ncbi.nlm.nih.gov/gene/?term=${ncbiTerm}` },
    {
      label: "UniProt",
      url: `https://www.uniprot.org/uniprotkb?query=gene:${s}+AND+organism_id:9606`,
    },
    { label: "Ensembl", url: `https://www.ensembl.org/Homo_sapiens/Gene/Summary?g=${s}` },
  ];
}
