import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

const webRoot = resolve(import.meta.dirname, "..");
const repoRoot = resolve(webRoot, "../..");
const catalogue = JSON.parse(readFileSync(resolve(repoRoot, "data/catalogue/v1/catalogue.json"), "utf8"));
const formats = ["csv", "tsv", "csv", "tsv"];
const slugs = ["caffeic-acid", "cid-450", "closantel", "benzethonium"];
const examples = [];
const expectedResults = [
  "Expected example result: ER and AR below threshold",
  "Expected example result: ER above threshold · AR below threshold",
  "Expected example result: ER below threshold · AR above threshold",
  "Expected example result: ER and AR above threshold",
];

for (const [index, compound] of catalogue.compounds.entries()) {
  const measured = compound.signatures[0];
  const payload = JSON.parse(readFileSync(resolve(repoRoot, measured.signature_path), "utf8"));
  const format = formats[index];
  const separator = format === "csv" ? "," : "\t";
  const filename = `lincs-${slugs[index]}-mcf7-a549.${format}`;
  const content = [
    `gene${separator}value`,
    ...Object.entries(payload.signature)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([gene, value]) => `${gene}${separator}${value}`),
  ].join("\n") + "\n";
  const destination = resolve(webRoot, "public/examples", filename);
  mkdirSync(dirname(destination), { recursive: true });
  writeFileSync(destination, content, "utf8");
  examples.push({
    id: measured.signature_id,
    name: compound.preferred_name,
    filename,
    format,
    url: `/examples/${filename}`,
    pubchem_cid: compound.pubchem_cid,
    inchikey: compound.compound_id,
    dataset: measured.dataset,
    accession: measured.accession,
    processing_level: measured.processing_level,
    cell_lines: measured.cell_lines,
    dose: measured.dose,
    timepoint: measured.timepoint,
    aggregation: measured.aggregation,
    provenance: payload.provenance,
    file_sha256: createHash("sha256").update(content).digest("hex"),
    source_signature_sha256: createHash("sha256")
      .update(readFileSync(resolve(repoRoot, measured.signature_path)))
      .digest("hex"),
    expected_result: expectedResults[index],
  });
}

const manifest = {
  schema_version: "2.0.0",
  generated_from: "data/catalogue/v1/catalogue.json",
  description: "Named real measured LINCS Level 5 examples; numeric values are unchanged.",
  examples,
};
const serialized = JSON.stringify(manifest, null, 2) + "\n";
writeFileSync(resolve(webRoot, "public/examples/manifest.json"), serialized, "utf8");
mkdirSync(resolve(webRoot, "src/generated"), { recursive: true });
writeFileSync(resolve(webRoot, "src/generated/example-files.json"), serialized, "utf8");
console.log(`Wrote ${examples.length} named multipart example files.`);
