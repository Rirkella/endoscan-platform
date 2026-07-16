import manifest from "./generated/example-files.json";

export interface ExampleSignatureFile {
  id: string;
  name: string;
  filename: string;
  format: "csv" | "tsv";
  url: string;
  pubchem_cid: number;
  inchikey: string;
  dataset: string;
  accession: string;
  processing_level: string;
  cell_lines: string[];
  dose: string | null;
  timepoint: string | null;
  aggregation: string;
  provenance: string;
  file_sha256: string;
  source_signature_sha256: string;
  expected_result: string;
}

export const exampleSignatureFiles = manifest.examples as ExampleSignatureFile[];
