// Validation step (ported from prototype-v2's input check). Driven by REAL data:
//   - gene coverage comes from the upload parse preview when present (matched / missing / extra vs
//     the model's landmark schema); for pasted/demo input we show the provided gene count and note
//     the server makes the final schema decision on run.
//   - model compatibility lists the endpoints the API actually returned (/endpoints) — never a
//     hardcoded ER/AR pair. The final gene-set validation is the server's, per endpoint.
// Experimental context fields are shown for the record but are NOT sent to the model (the models
// consume the transcriptomic signature only); this is stated plainly so nothing is implied.

import type { EndpointSummary } from "../../api/types";
import type { PreparedInput } from "../../pages/Analyze";

function endpointCodeClass(id: string): string {
  const k = id.toLowerCase();
  return k === "er" ? "code-er" : k === "ar" ? "code-ar" : "code-generic";
}

export function ValidationStep({
  input,
  endpoints,
  onBack,
  onRun,
}: {
  input: PreparedInput;
  endpoints: EndpointSummary[];
  onBack: () => void;
  onRun: () => void;
}) {
  const preview = input.parse?.preview;
  const schemaN = input.parse?.n_schema_genes ?? null;
  const provided = Object.keys(input.signature).length;
  const coveragePct =
    preview && schemaN ? Math.round((preview.n_matched / schemaN) * 100) : null;

  return (
    <section className="validation-layout">
      <div className="validation-main">
        <div className="section-title-row">
          <div>
            <p className="eyebrow">Input check</p>
            <h2>Confirm what will be analyzed</h2>
          </div>
          <span className="validation-state">
            <span className="status-dot" aria-hidden />
            Ready to run
          </span>
        </div>

        <div className="input-summary">
          <div className="file-mark">
            {input.kind === "file" ? "CSV" : input.kind === "demo" ? "DEMO" : "JSON"}
          </div>
          <div>
            <strong>{input.title}</strong>
            <span>{input.subtitle}</span>
          </div>
          <button className="button outline" onClick={onBack}>
            Change input
          </button>
        </div>

        <div className="check-section">
          <div className="check-heading">
            <div>
              <span className="check-number">1</span>
              <div>
                <h3>Gene coverage</h3>
                <p>Coverage is checked against the required landmark schema.</p>
              </div>
            </div>
            {preview && schemaN ? (
              <span className="status-chip status-good">
                {preview.n_matched} / {schemaN} found
              </span>
            ) : (
              <span className="status-chip status-note">Checked on run</span>
            )}
          </div>
          {preview && schemaN ? (
            <>
              <div className="coverage-bar">
                <span style={{ width: `${coveragePct}%` }} />
              </div>
              <div className="coverage-legend">
                <span>
                  <span className="status-dot" aria-hidden />
                  Required genes {schemaN}
                </span>
                <span>Missing {preview.n_missing}</span>
                <span>Extra {preview.n_extra} excluded</span>
              </div>
            </>
          ) : (
            <p className="check-plain">
              {provided} gene values provided. The server validates the gene set against each
              endpoint&rsquo;s schema when models are run.
            </p>
          )}
        </div>

        <div className="check-section">
          <div className="check-heading">
            <div>
              <span className="check-number">2</span>
              <div>
                <h3>Experimental context</h3>
                <p>Recorded for your notes. Models consume the signature only, not these fields.</p>
              </div>
            </div>
            <span className="status-chip status-note">Optional</span>
          </div>
          <div className="context-grid">
            <label>
              <span>Cell context</span>
              <input placeholder="e.g. MCF-7" />
            </label>
            <label>
              <span>Exposure time</span>
              <input placeholder="e.g. 24 h" />
            </label>
            <label>
              <span>Dose</span>
              <input placeholder="e.g. 10 µM" />
            </label>
            <label>
              <span>Value scale</span>
              <select defaultValue="log2 fold change">
                <option>log2 fold change</option>
                <option>z-score</option>
                <option>Normalized expression</option>
              </select>
            </label>
          </div>
        </div>
      </div>

      <aside className="compatibility-panel">
        <p className="eyebrow">Model compatibility</p>
        <h2>
          {endpoints.length} available endpoint model{endpoints.length === 1 ? "" : "s"}
        </h2>
        <p>Each model checks the gene set against its own schema when it runs.</p>
        <div className="compatibility-list">
          {endpoints.map((e) => (
            <div key={e.endpoint_id}>
              <span className={`endpoint-code ${endpointCodeClass(e.endpoint_id)}`}>
                {e.endpoint_id}
              </span>
              <span>
                <strong>{e.biological_target}</strong>
                <small>{e.status}</small>
              </span>
              <b>Available</b>
            </div>
          ))}
        </div>
        <div className="model-note">
          <span className="status-dot status-dot-amber" aria-hidden />
          <p>
            Scores indicate similarity to learned endpoint-associated patterns. They are not
            clinical, regulatory or safety conclusions.
          </p>
        </div>
        <button
          className="button primary full-button"
          disabled={endpoints.length === 0}
          onClick={onRun}
        >
          Run {endpoints.length} endpoint model{endpoints.length === 1 ? "" : "s"}
        </button>
        <button className="button quiet full-button" onClick={onBack}>
          Back
        </button>
      </aside>
    </section>
  );
}
