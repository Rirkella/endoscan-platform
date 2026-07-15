// Landing / product entry — rendered full-bleed OUTSIDE the workspace shell. Positions EndoScan as
// a broad toxicology platform (ER/AR are the first available endpoints, not the whole identity),
// in plain English, with a consistent CTA system (filled primary / outlined secondary / text links)
// and every CTA pointing at a REAL route. No API data is used here; the endpoint result shown in
// "More than a score" is an explicitly-labelled illustrative example.

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
            <a href="#how">How it works</a>
            <a href="#evidence">Evidence</a>
            <a href="#endpoints">Endpoint library</a>
          </nav>
          <Link className="landing-nav-action" to="/analyze">
            Start analysis
          </Link>
        </header>

        <div className="landing-hero-content">
          <p className="landing-overline">Mechanistic toxicology, powered by transcriptomics</p>
          <h1>Understand toxicological effects — and the biology behind them.</h1>
          <p className="landing-hero-copy">
            Explore public toxicology data or analyze a measured gene-expression response across a
            growing library of endpoint models. See the genes, pathways, reference compounds and
            evidence behind every signal.
          </p>
          <div className="landing-actions">
            <Link className="landing-primary-action" to="/analyze">
              Start an analysis
            </Link>
            <Link className="landing-secondary-action" to="/explore">
              Explore public data
            </Link>
          </div>
          <p className="landing-hero-disclaimer">Research use only · Experimental models</p>
        </div>
      </section>

      {/* 2 — Two ways to use EndoScan */}
      <section className="landing-section landing-ways">
        <div className="landing-section-head">
          <p className="landing-kicker">Two ways to use EndoScan</p>
          <h2>Explore what&rsquo;s known, or analyze your own data.</h2>
        </div>
        <div className="ways-grid">
          <article>
            <h3>Explore public toxicology data</h3>
            <p>
              Public toxicology evidence is fragmented across databases and studies. EndoScan brings
              measured gene-expression responses together so you can see how experiments relate.
            </p>
            <Link className="landing-text-action" to="/explore">
              Explore reference data
            </Link>
          </article>
          <article>
            <h3>Analyze your own transcriptomic response</h3>
            <p>
              Upload a measured signature — or run a real demo — and see which endpoint signals it
              shows, with the genes, pathways and limitations behind each result.
            </p>
            <Link className="landing-text-action" to="/analyze">
              Start an analysis
            </Link>
          </article>
        </div>
      </section>

      {/* 3 — Why transcriptomics */}
      <section className="landing-approach" id="how">
        <div className="landing-section approach-inner">
          <div className="landing-section-head landing-section-head-light">
            <p className="landing-kicker">Why transcriptomics</p>
            <h2>Start with the measured biological response.</h2>
          </div>
          <div className="why-grid">
            <article>
              <span>01</span>
              <p>
                A molecule&rsquo;s effect depends on dose, exposure time, cell type and biological
                context — not on its name or structure alone.
              </p>
            </article>
            <article>
              <span>02</span>
              <p>
                Transcriptomics measures how thousands of genes respond at once, giving a broad
                readout of what actually happened in the cells.
              </p>
            </article>
            <article>
              <span>03</span>
              <p>
                Those gene-expression changes can reveal several biological mechanisms and early
                signals that a single number would hide.
              </p>
            </article>
          </div>
        </div>
      </section>

      {/* 4 — How it works */}
      <section className="landing-section landing-how">
        <div className="landing-section-head">
          <p className="landing-kicker">How it works</p>
          <h2>Four steps from signature to evidence.</h2>
        </div>
        <ol className="how-steps">
          <li>
            <span>1</span>
            <div>
              <strong>Add a signature</strong>
              <p>Upload a measured response, or run a real demo.</p>
            </div>
          </li>
          <li>
            <span>2</span>
            <div>
              <strong>Check compatibility</strong>
              <p>Confirm gene coverage against the model schema.</p>
            </div>
          </li>
          <li>
            <span>3</span>
            <div>
              <strong>Run endpoint models</strong>
              <p>Score the signature across the available models.</p>
            </div>
          </li>
          <li>
            <span>4</span>
            <div>
              <strong>Review the evidence</strong>
              <p>See genes, pathways, reference context and limitations.</p>
            </div>
          </li>
        </ol>
      </section>

      {/* 5 — More than a score (the strong result visual, moved up) */}
      <section className="landing-section landing-evidence" id="evidence">
        <div className="landing-evidence-copy">
          <p className="landing-kicker">More than a score</p>
          <h2>See the evidence behind every result.</h2>
          <p>
            EndoScan is not a black-box score. Each endpoint result stays connected to the biology
            behind it, so you can prioritize the right follow-up experiments.
          </p>
          <dl>
            <div>
              <dt>Signal</dt>
              <dd>Score, decision threshold and model call</dd>
            </div>
            <div>
              <dt>Genes</dt>
              <dd>The genes that contributed to the result</dd>
            </div>
            <div>
              <dt>Pathways</dt>
              <dd>Biological pathways among those genes</dd>
            </div>
            <div>
              <dt>Reference</dt>
              <dd>Nearby measured signatures and context</dd>
            </div>
            <div>
              <dt>Limitations</dt>
              <dd>Model status and what a result does not mean</dd>
            </div>
          </dl>
          <Link className="button outline" to="/library">
            View model library
          </Link>
        </div>

        <div className="landing-product-preview" aria-label="Illustrative EndoScan endpoint result">
          <div className="preview-topline">
            <span>EXAMPLE RESULT / ER</span>
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
              <span>Coverage</span>
              <strong>978 / 978</strong>
            </div>
            <div>
              <span>Model call</span>
              <strong>Active</strong>
            </div>
          </div>
          <div className="preview-evidence">
            <p>Top contributing genes</p>
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
            Illustrative example. A signal is a research lead, not a safety conclusion.
          </div>
        </div>
      </section>

      {/* 6 — Growing endpoint library */}
      <section className="landing-value" id="endpoints">
        <div className="landing-section">
          <div className="landing-section-head">
            <p className="landing-kicker">A growing endpoint library</p>
            <h2>Endocrine endpoints today, more toxicology to come.</h2>
          </div>
          <div className="endpoints-grid">
            <article className="endpoints-now">
              <p className="endpoints-tag endpoints-tag-now">Available today</p>
              <h3>Experimental ER and AR models</h3>
              <p>
                Estrogen-receptor and androgen-receptor activity — the first endpoint models. Both
                are experimental.
              </p>
            </article>
            <article>
              <p className="endpoints-tag endpoints-tag-planned">Planned</p>
              <h3>Additional toxicology endpoints</h3>
              <p>
                Areas such as liver stress, DNA damage, oxidative stress and more are planned. The
                platform does not cover them yet.
              </p>
            </article>
            <article>
              <p className="endpoints-tag endpoints-tag-planned">Future scaling</p>
              <h3>Agent-assisted dataset &amp; model pipeline</h3>
              <p>
                A future workflow to help discover datasets, build training sets and register new
                endpoint models with recorded provenance.
              </p>
            </article>
          </div>
          <div className="landing-positioning">
            <p>
              <strong>EndoScan does not replace experimental validation.</strong> It helps you decide
              which compounds and experiments deserve a closer look.
            </p>
          </div>
        </div>
      </section>

      {/* 7 — Current scope + final CTA */}
      <section className="landing-final-cta">
        <div>
          <p className="landing-kicker">Current scope</p>
          <h2>Clear about what works now.</h2>
          <p>
            Today: measured transcriptomic signatures, compatibility checks, experimental ER and AR
            models, contributing genes, pathway context, reference signatures and model limitations.
            Molecule search, report export and additional endpoints are planned.
          </p>
          <div className="final-cta-actions">
            <Link className="landing-primary-action" to="/analyze">
              Start an analysis
            </Link>
            <Link className="landing-secondary-action" to="/explore">
              Explore public data
            </Link>
          </div>
        </div>
      </section>

      <footer className="landing-footer">
        <span>EndoScan / transcriptomics-first toxicology pre-screening</span>
        <span>Research use only · experimental models</span>
      </footer>
    </div>
  );
}
