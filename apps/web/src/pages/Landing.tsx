// Landing / product entry — ported from prototype-v2's LandingPage, rendered full-bleed OUTSIDE
// the workspace shell. Copy follows the prototype; the only deviations from the prototype are
// honesty-driven: every call-to-action navigates to a REAL route (/analyze, /library, /explore),
// and prototype-only "mock results" phrasing is replaced with experimental/research-use framing.
// No API data is used here.

import { Link } from "react-router-dom";

import heroImage from "../assets/endoscan-hero.png";

export function Landing() {
  return (
    <div className="landing-page">
      <section className="landing-hero" style={{ backgroundImage: `url(${heroImage})` }}>
        <header className="landing-nav">
          <button
            className="landing-brand"
            onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}
            aria-label="EndoScan home"
          >
            <span className="landing-brand-mark">E</span>
            <span>
              Endo<span>Scan</span>
            </span>
          </button>
          <nav aria-label="Landing navigation">
            <a href="#approach">Approach</a>
            <a href="#evidence">Evidence</a>
            <a href="#value">Value</a>
          </nav>
          <Link className="landing-nav-action" to="/analyze">
            Open workspace
          </Link>
        </header>

        <div className="landing-hero-content">
          <p className="landing-overline">Transcriptomics-first endocrine intelligence</p>
          <h1>EndoScan</h1>
          <h2>See biological signals earlier.</h2>
          <p className="landing-hero-copy">
            EndoScan turns measured gene-expression responses into explainable endocrine endpoint
            signals, helping research teams prioritize what to investigate next.
          </p>
          <div className="landing-actions">
            <Link className="landing-primary-action" to="/analyze">
              Open the workspace
            </Link>
            <Link className="landing-secondary-action" to="/library">
              Review model evidence
            </Link>
          </div>
          <div className="landing-hero-facts" aria-label="Platform principles">
            <span>Measured biological response</span>
            <span>Endpoint-level explanation</span>
            <span>Auditable research context</span>
          </div>
        </div>

        <div className="landing-visual-caption">
          <span>01</span>
          <p>
            <strong>From cellular response to research evidence</strong>Transcriptomic signature,
            model signal, genes, pathways and reference context.
          </p>
        </div>
      </section>

      <section className="landing-proof-strip" aria-label="Current platform scope">
        <div>
          <strong>978</strong>
          <span>landmark genes checked before analysis</span>
        </div>
        <div>
          <strong>2</strong>
          <span>registered endocrine endpoint models</span>
        </div>
        <div>
          <strong>3</strong>
          <span>evidence layers: genes, pathways, references</span>
        </div>
        <p>
          <span className="proof-dot" />
          Experimental research platform
        </p>
      </section>

      <section className="landing-section landing-problem">
        <div className="landing-section-intro">
          <p className="landing-kicker">The opportunity</p>
          <h2>Endocrine signals are expensive to discover late.</h2>
          <p>
            Research teams generate rich biological data, but turning that response into a traceable
            screening hypothesis still requires fragmented tools and specialist interpretation.
          </p>
        </div>
        <div className="landing-problem-grid">
          <article>
            <span>01</span>
            <h3>Data arrives before clarity</h3>
            <p>
              Gene-expression measurements contain signal, but raw matrices do not tell a team which
              endocrine endpoints deserve attention.
            </p>
          </article>
          <article>
            <span>02</span>
            <h3>Black-box scores are not enough</h3>
            <p>
              A number without genes, pathways, model status and applicability context is difficult
              to trust or act on.
            </p>
          </article>
          <article>
            <span>03</span>
            <h3>Confirmation is resource-intensive</h3>
            <p>
              Teams need a defensible way to prioritize follow-up experiments, not a replacement for
              those experiments.
            </p>
          </article>
        </div>
      </section>

      <section className="landing-approach" id="approach">
        <div className="landing-section approach-inner">
          <div className="landing-section-intro landing-section-intro-light">
            <p className="landing-kicker">The EndoScan approach</p>
            <h2>Start with what biology actually did.</h2>
            <p>
              EndoScan is built around measured transcriptomic response. Molecule identity can
              retrieve a public signature, but it is never treated as the result itself.
            </p>
          </div>
          <div className="landing-flow" aria-label="EndoScan workflow">
            <article>
              <span>01</span>
              <div>
                <strong>Add response</strong>
                <p>Upload a signature or select a measured public perturbation.</p>
              </div>
            </article>
            <article>
              <span>02</span>
              <div>
                <strong>Verify fit</strong>
                <p>Confirm columns, gene coverage and experimental context.</p>
              </div>
            </article>
            <article>
              <span>03</span>
              <div>
                <strong>Assess endpoints</strong>
                <p>Run registered models with visible thresholds and status.</p>
              </div>
            </article>
            <article>
              <span>04</span>
              <div>
                <strong>Inspect evidence</strong>
                <p>Review genes, pathways, neighbors and limitations.</p>
              </div>
            </article>
          </div>
          <Link className="landing-inline-action button" to="/analyze">
            Walk through the analysis flow
          </Link>
        </div>
      </section>

      <section className="landing-section landing-evidence" id="evidence">
        <div className="landing-evidence-copy">
          <p className="landing-kicker">Explainability by design</p>
          <h2>A result that can be questioned, traced and discussed.</h2>
          <p>
            Every endpoint signal stays connected to the evidence behind it. The interface separates
            the prediction for one signature from the quality and limitations of the model itself.
          </p>
          <dl>
            <div>
              <dt>Signal</dt>
              <dd>Score, decision threshold and model call</dd>
            </div>
            <div>
              <dt>Explanation</dt>
              <dd>Contributing genes and pathway context</dd>
            </div>
            <div>
              <dt>Reference</dt>
              <dd>Nearby measured signatures and provenance</dd>
            </div>
            <div>
              <dt>Boundary</dt>
              <dd>Model status and interpretation limitations</dd>
            </div>
          </dl>
          <Link className="landing-text-action" to="/library">
            Open the model library
          </Link>
        </div>

        <div className="landing-product-preview" aria-label="Example EndoScan endpoint result">
          <div className="preview-topline">
            <span>SCREENING RESULT / ER</span>
            <b>Experimental model</b>
          </div>
          <div className="preview-summary">
            <div>
              <span>Endpoint signal score</span>
              <strong>0.82</strong>
            </div>
            <span className="preview-call">Above threshold</span>
          </div>
          <div className="preview-track">
            <span />
            <i />
          </div>
          <div className="preview-meta">
            <div>
              <span>Threshold</span>
              <strong>0.50</strong>
            </div>
            <div>
              <span>Input coverage</span>
              <strong>978 / 978</strong>
            </div>
            <div>
              <span>Model call</span>
              <strong>Active</strong>
            </div>
          </div>
          <div className="preview-evidence">
            <p>Top contributing evidence</p>
            <div>
              <strong>ESR1</strong>
              <span>
                <i style={{ width: "91%" }} />
              </span>
              <b>0.091</b>
            </div>
            <div>
              <strong>FOXA1</strong>
              <span>
                <i style={{ width: "76%" }} />
              </span>
              <b>0.076</b>
            </div>
            <div>
              <strong>GATA3</strong>
              <span>
                <i style={{ width: "62%" }} />
              </span>
              <b>0.062</b>
            </div>
          </div>
          <div className="preview-footnote">
            <span />
            Illustrative example. Pattern similarity is a research signal, not a safety conclusion.
          </div>
        </div>
      </section>

      <section className="landing-value" id="value">
        <div className="landing-section">
          <div className="landing-value-heading">
            <p className="landing-kicker">Why it matters</p>
            <h2>More informed prioritization before costly confirmation.</h2>
          </div>
          <div className="landing-value-grid">
            <article>
              <span>For research teams</span>
              <h3>One coherent path from signature to hypothesis</h3>
              <p>
                Reduce manual switching between data validation, model output and biological context.
              </p>
            </article>
            <article>
              <span>For R&amp;D programs</span>
              <h3>Prioritize candidates with visible reasoning</h3>
              <p>
                Use endpoint evidence to decide which compounds and experiments deserve deeper
                investigation.
              </p>
            </article>
            <article>
              <span>For platform growth</span>
              <h3>A reusable framework for additional endpoints</h3>
              <p>
                Registered models, evidence contracts and reproducible reports create a foundation
                that can expand.
              </p>
            </article>
          </div>
          <div className="landing-positioning">
            <p>
              <strong>EndoScan does not replace experimental validation.</strong> It is designed to
              make the path toward validation more focused, explainable and reproducible.
            </p>
            <Link to="/explore">Explore reference data</Link>
          </div>
        </div>
      </section>

      <section className="landing-final-cta">
        <div>
          <p className="landing-kicker">Experimental research platform</p>
          <h2>Follow a biological response from input to evidence.</h2>
          <p>
            Work through the analysis flow on your own transcriptomic signature. Endpoint models are
            experimental and for research use only.
          </p>
        </div>
        <Link to="/analyze">Enter the EndoScan workspace</Link>
      </section>

      <footer className="landing-footer">
        <span>EndoScan / transcriptomics-first endocrine pre-screening</span>
        <span>Experimental research use only</span>
      </footer>
    </div>
  );
}
