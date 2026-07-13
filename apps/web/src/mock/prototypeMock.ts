// MOCK / PROPOSED data — NOT from the EndoScan API. Everything in this file is illustrative
// placeholder content for product areas the backend does not support yet. It exists so the ported
// prototype-v2 UX can show its full shape, but it is NEVER passed off as a model result:
//   - the known-molecule lookup below does NOT produce an analysis (EndoScan will not infer a
//     result from molecule identity alone — a real measured signature must be uploaded);
//   - the projects history is a UX placeholder for a persistence layer that does not exist.
// Keep this the ONLY place mock content lives, so real vs. proposed is always obvious in review.

export interface MockMeasuredSignature {
  id: string;
  molecule: string;
  context: string; // cell line / dose / exposure
  source: string;
  coverage: string;
}

// Example measured-signature catalogue for the "Known molecule" lookup tab. Illustrative only —
// real lookup needs identity resolution (PubChem CID / InChIKey), provenance and a real signature.
export const MOCK_MEASURED_SIGNATURES: MockMeasuredSignature[] = [
  { id: "estradiol-mcf7", molecule: "Estradiol", context: "MCF-7 / 10 nM / 24 h", source: "Example catalogue", coverage: "978 genes" },
  { id: "estradiol-t47d", molecule: "Estradiol", context: "T47D / 100 nM / 6 h", source: "Example catalogue", coverage: "978 genes" },
  { id: "bpa-mcf7", molecule: "Bisphenol A", context: "MCF-7 / 10 µM / 24 h", source: "Example catalogue", coverage: "978 genes" },
];

export interface MockProject {
  id: string;
  input: string;
  context: string;
  summary: string;
  updated: string;
  positive: boolean;
}

// Placeholder workspace history for the Projects screen (no persistence backend exists yet).
export const MOCK_PROJECTS: MockProject[] = [
  { id: "ES-EXAMPLE-014", input: "breast-cells-dose-response.csv", context: "MCF-7 / 24 h", summary: "1 endpoint above threshold", updated: "Example entry", positive: true },
  { id: "ES-EXAMPLE-009", input: "Estradiol (example)", context: "MCF-7 / 10 nM / 24 h", summary: "1 endpoint above threshold", updated: "Example entry", positive: true },
  { id: "ES-EXAMPLE-022", input: "reference_batch_03.csv", context: "T47D / 6 h", summary: "No endpoints above threshold", updated: "Example entry", positive: false },
];
