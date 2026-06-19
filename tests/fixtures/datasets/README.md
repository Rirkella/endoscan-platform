# Dataset-construction test fixtures — schemas (FLAGGED FOR HUMAN REVIEW)

Tiny, **offline** stand-ins for the real sources, used by the M2 toolchain tests.
They contain no real data and make no scientific claims. Git-tracked via the M0
`.gitignore` exception for `tests/fixtures/**`.

> **Reviewer note:** these shapes are best-effort mimics of the real source
> schemas. **A human must confirm the real ToxCast / Tox21 / CERAPP / CoMPARA /
> LINCS / PubChem column names and encodings before the real download adapters
> are built.** The fixture adapter resolves a source to `<id>.csv` by `source.id`.

All compounds are synthetic. Canonical id is the **full InChIKey** (27 chars).
The fixtures deliberately include edge cases: an unmapped compound, an ambiguous
mapping, a cross-source label conflict, a duplicate signature, a compound with
multiple signatures, and off-target rows (AR) that an ER run must exclude.

## `lincs.csv` — type `signatures`
Columns: `sig_id, pert_id, pert_iname, cell_id, pert_dose, pert_dose_unit,
pert_time, pert_time_unit, GENE_A..GENE_E`.
- Metadata columns are fixed; **every other column is a landmark-gene feature**
  (here 5 symbolic genes; real L1000 has 978). Values are z-scored differential
  expression (float).
- `pert_id` is the perturbagen id (`PERT_ID`), harmonized to InChIKey by the mapper.
- `S1`/`S1b` are an intentional **duplicate signature** (same compound+cell+dose+time);
  `S1c` is a second signature for the same compound on a different cell line.
- `S7` (BRD-C7) is dropped downstream because its compound has a label conflict.

## `toxcast.csv` — type `labels`
Columns: `dsstox_sid, casrn, preferred_name, assay_name, target, hitcall, ac50,
ac50_unit`.
- `hitcall` ∈ {0,1}; `ac50` nullable (µM). `compound_id` = `casrn` (`CASRN`).
- Row `100-00-7` is **inactive** here (conflicts with CERAPP active).
- `100-00-8` is unmapped (absent from the mapping fixture); `100-00-9` is ambiguous.
- The `AR` row (`100-00-99`) must be **excluded** by an ER run (target filter).

## `tox21.csv` — type `labels`
Columns: `sample_id, casrn, pubchem_cid, assay_name, target, activity, potency`.
- `activity` ∈ {`active`,`inactive`,`inconclusive`} → mapped to 1/0; **inconclusive
  is dropped**. `compound_id` = `casrn`.

## `cerapp.csv` — type `labels` (ER)
Columns: `casrn, chemical_name, inchikey, target, consensus_call, consensus_score`.
- `consensus_call` ∈ {`active`,`inactive`} → 1/0; `consensus_score` ∈ [0,1] →
  `confidence`. `compound_id` = `casrn` (so mapping is exercised, even though the
  source also carries an InChIKey).
- `100-00-7` is **active** here (conflicts with ToxCast inactive).

## `compara.csv` — type `labels` (AR)
Same columns as `cerapp.csv` with `target = AR`. Present so an ER run excludes it
and to mirror the real CERAPP/CoMPARA pair.

## `pubchem.csv` — type `mapping`
Columns: `input_id, input_id_type, inchikey, cid, smiles, mapping_confidence`.
- `input_id_type` ∈ {`CASRN`,`CID`,`SMILES`,`PERT_ID`,`DTXSID`} (here CASRN and
  PERT_ID are used). `mapping_confidence` ∈ {`exact`,`ambiguous`}.
- `100-00-9` appears **twice with different InChIKeys** → treated as ambiguous
  (recorded as a conflict, never silently resolved).
- `100-00-8` is intentionally **absent** → unmapped.
- `smiles` is carried only as an identifier/provenance field — it is **never** an
  input to any signature or risk prediction.

## Expected derived quantities (ER target)
- Labels (after ER filter): compounds `100-00-1..9` (8 dropped as unmapped, 9 as
  ambiguous, 7 as conflicted).
- Overlap (label ∩ signature): InChIKeys for C1..C7 = **7** (`per_class` excludes
  the conflicted C7 → `{1: 3, 0: 3}`).
- Candidate table: usable compounds C1..C6 → **8 signature rows** (C1 has 3),
  `per_class_compound_counts = {1: 3, 0: 3}`, one excluded conflict (C7).
- Duplicate-signature rate `0.125`; metadata coverage `1.0`; label-conflict rate
  `≈0.143`. With `n_groups=2` the compound-level split is feasible and leakage-free.
