export interface ExampleSignatureFile {
  id: string;
  name: string;
  filename: string;
  format: "csv" | "tsv";
  url: string;
  provenance: string;
}

export const exampleSignatureFiles: ExampleSignatureFile[] = [
  {
    id: "lincs-caffeic-acid-csv",
    name: "Caffeic acid · CSV",
    filename: "lincs-caffeic-acid-mcf7-a549.csv",
    format: "csv",
    url: "/examples/lincs-caffeic-acid-mcf7-a549.csv",
    provenance:
      "Real measured LINCS Level 5 COMPZ.MODZ signature, condition-averaged across MCF7 and A549; GSE92742; InChIKey QAIPRVGONGVQAS-DUXPYHPUSA-N. Values are unchanged from the committed curated demo.",
  },
  {
    id: "lincs-cid-450-tsv",
    name: "CID 450 signature · TSV",
    filename: "lincs-cid-450-mcf7-a549.tsv",
    format: "tsv",
    url: "/examples/lincs-cid-450-mcf7-a549.tsv",
    provenance:
      "Real measured LINCS Level 5 COMPZ.MODZ signature, condition-averaged across MCF7 and A549; GSE92742; InChIKey VOXZDWNPVJITMN-UHFFFAOYSA-N. Values are unchanged from the committed curated demo.",
  },
];
