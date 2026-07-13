// Landing / marketing entry for EndoScan (production app, real routes — NO mock API data).
// Positioning copy is the product owner's source of truth. Honesty guardrails applied:
//  - "Research use only. Experimental models." framing is prominent.
//  - Current scope is separated from future direction (a dedicated, distinct "Current scope" block).
//  - Features the backend does not support yet (report export, SMILES→transcriptomics, the broader
//    endpoint library beyond ER/AR, literature, similar-compounds/data search) are marked "Planned"
//    or clearly framed as future — never shown as already-working product features.
//  - CTAs navigate ONLY to existing real screens (/analyze, /explore). No fake pages.

import { Link } from "react-router-dom";

function Planned() {
  return (
    <span className="ml-2 inline-flex items-center rounded-full border border-warn-line bg-warn-bg px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-warn align-middle">
      Planned
    </span>
  );
}

function Available() {
  return (
    <span className="ml-2 inline-flex items-center rounded-full border border-line bg-surface px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-brand align-middle">
      Available now
    </span>
  );
}

function Developing() {
  return (
    <span className="ml-2 inline-flex items-center rounded-full border border-warn-line bg-warn-bg px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-warn align-middle">
      Developing
    </span>
  );
}

function Section({
  eyebrow,
  title,
  children,
}: {
  eyebrow?: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="border-t border-line py-12">
      {eyebrow && (
        <p className="text-[11px] font-bold uppercase tracking-widest text-brand">{eyebrow}</p>
      )}
      <h2 className="mt-1 text-2xl font-bold tracking-tight text-ink">{title}</h2>
      <div className="mt-5 space-y-5 text-[15px] leading-relaxed text-muted">{children}</div>
    </section>
  );
}

function Bullets({ items }: { items: string[] }) {
  return (
    <ul className="grid gap-1.5 sm:grid-cols-2">
      {items.map((it) => (
        <li key={it} className="flex gap-2">
          <span aria-hidden className="mt-2 h-1.5 w-1.5 flex-none rounded-full bg-accent" />
          <span>{it}</span>
        </li>
      ))}
    </ul>
  );
}

function QA({
  q,
  badge,
  children,
}: {
  q: string;
  badge?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-line bg-card p-4 shadow-card">
      <h3 className="text-base font-semibold text-ink">
        {q}
        {badge}
      </h3>
      <p className="mt-1.5 text-sm leading-relaxed text-muted">{children}</p>
    </div>
  );
}

function Step({ n, title, children }: { n: number; title: React.ReactNode; children: React.ReactNode }) {
  return (
    <li className="flex gap-3">
      <span
        aria-hidden
        className="grid h-7 w-7 flex-none place-items-center rounded-full bg-brand text-xs font-bold text-white"
      >
        {n}
      </span>
      <div>
        <p className="font-semibold text-ink">{title}</p>
        <p className="mt-0.5 text-sm leading-relaxed text-muted">{children}</p>
      </div>
    </li>
  );
}

export function Landing() {
  return (
    <div className="mx-auto max-w-3xl">
      {/* Hero */}
      <section className="pb-2 pt-2">
        <p className="text-[11px] font-bold uppercase tracking-widest text-brand">EndoScan</p>
        <h1 className="mt-2 text-4xl font-bold leading-tight tracking-tight text-ink sm:text-5xl">
          A platform for understanding molecular toxicity
        </h1>
        <div className="mt-5 space-y-4 text-[16px] leading-relaxed text-muted">
          <p>
            EndoScan is a toxicology platform for studying how molecules may affect biological
            systems.
          </p>
          <p>
            It brings together public toxicology data, transcriptomic analysis, endpoint models,
            biological pathways, known compound effects, and scientific literature in one place.
          </p>
          <p>
            The goal is not only to estimate whether a molecule may be risky, but to explain why:
            which genes changed, which pathways were affected, which known molecules show similar
            effects, and what evidence supports the result.
          </p>
        </div>
        <div className="mt-7 flex flex-wrap items-center gap-3">
          <Link
            to="/analyze"
            className="rounded-md bg-brand px-5 py-2.5 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-brand-dark"
          >
            Analyze your data
          </Link>
          <Link
            to="/explore"
            className="rounded-md border border-brand/40 bg-card px-5 py-2.5 text-sm font-semibold text-brand transition-colors hover:bg-surface"
          >
            Explore the platform
          </Link>
        </div>
        <p className="mt-4 inline-flex items-center gap-2 rounded-full border border-warn-line bg-warn-bg px-3 py-1 text-xs font-semibold text-warn">
          <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-warn" />
          Research use only. Experimental models.
        </p>
      </section>

      {/* What EndoScan helps you do */}
      <Section eyebrow="Capabilities" title="What EndoScan helps you do">
        <div>
          <h3 className="text-lg font-semibold text-ink">Explore existing toxicology data</h3>
          <p className="mt-2">
            A large amount of toxicology data is already public, but it is spread across many
            databases, papers, assays, and formats. EndoScan makes this data easier to explore.
          </p>
          <p className="mt-3 font-medium text-ink">
            EndoScan is being built to help researchers search and compare:
          </p>
          <div className="mt-2">
            <Bullets
              items={[
                "molecules",
                "experimental conditions",
                "cell types and tissues",
                "doses and exposure times",
                "measured gene-expression responses",
                "toxicological endpoints",
                "affected genes and pathways",
                "similar compounds with known effects, where reference data are available",
              ]}
            />
          </div>
          <p className="mt-3">
            This helps researchers understand what is already known before starting a new analysis.
          </p>
        </div>

        <div className="pt-4">
          <h3 className="text-lg font-semibold text-ink">Analyze your own molecules</h3>
          <p className="mt-2">
            If you have transcriptomic data from an experiment, EndoScan can help you analyze it
            across available toxicology models. You upload a gene-expression signature, and the
            platform checks whether it is compatible with the available endpoint models.
          </p>
          <p className="mt-3 font-medium text-ink">Then EndoScan shows:</p>
          <div className="mt-2">
            <Bullets
              items={[
                "which toxicological signals were detected",
                "how strong the signal is",
                "which endpoint models were used",
                "which genes contributed to the result",
                "which biological pathways may be involved",
                "which known compounds show similar responses",
                "which scientific evidence supports the interpretation",
              ]}
            />
          </div>
        </div>
      </Section>

      {/* Why transcriptomics */}
      <Section eyebrow="Approach" title="Why transcriptomics?">
        <p>A molecule&rsquo;s effect cannot be understood from its name or chemical structure alone.</p>
        <p>
          The same molecule may behave differently depending on the dose, exposure time, tissue,
          cell type, organism, and experimental conditions.
        </p>
        <p>
          Transcriptomics measures how thousands of genes respond after exposure to a molecule. This
          gives a broad picture of what is happening inside the biological system.
        </p>
        <p className="font-medium text-ink">Gene-expression changes can reveal early signs of:</p>
        <Bullets
          items={[
            "hormone disruption",
            "liver stress",
            "DNA damage",
            "inflammation",
            "oxidative stress",
            "mitochondrial dysfunction",
            "immune response",
            "developmental or reproductive toxicity",
          ]}
        />
        <p>
          That is why EndoScan uses transcriptomic signatures as a central input for toxicology
          analysis.
        </p>
      </Section>

      {/* Endpoint library */}
      <Section eyebrow="Endpoints" title="A growing library of toxicology endpoints">
        <p>Toxicity is not one single thing.</p>
        <p>
          A molecule can affect many different biological systems. EndoScan is being built around a
          large library of endpoint models, where each model focuses on a specific toxicological
          effect.
        </p>
        <div className="rounded-xl border border-line bg-card p-4 shadow-card">
          <p className="text-sm font-semibold text-ink">
            Available today
            <Available />
          </p>
          <p className="mt-1 text-sm text-muted">
            Estrogen receptor (ER) activity and androgen receptor (AR) activity — the first,
            endocrine-related endpoint models. Both are experimental.
          </p>
          <p className="mt-3 text-sm font-semibold text-ink">
            Planned areas
            <Planned />
          </p>
          <p className="mt-1 text-sm text-muted">
            liver toxicity; DNA damage; oxidative stress; immune activation; mitochondrial toxicity;
            developmental toxicity; reproductive toxicity.
          </p>
        </div>
        <p>
          The first models focus on endocrine-related endpoints. Over time, the library will expand
          to cover more areas of preclinical toxicology.
        </p>
      </Section>

      {/* Built from open data */}
      <Section eyebrow="Data & provenance" title="Built from open toxicology data">
        <p>EndoScan is designed to grow from public scientific data.</p>
        <p>
          Many useful toxicology datasets already exist, but they are difficult to find, compare,
          and reuse.
        </p>
        <p>
          The platform will use an agent-assisted workflow to help discover relevant datasets,
          prepare them, train endpoint models, evaluate model quality, and record where each dataset
          and model came from. This allows the endpoint library to grow while keeping the process
          transparent and traceable.
        </p>
        <p className="font-medium text-ink">For each model, users should be able to see:</p>
        <Bullets
          items={[
            "what endpoint it detects",
            "what data it was trained on",
            "how the endpoint was defined",
            "how well the model performs",
            "what input data it requires",
            "what the model can and cannot conclude",
          ]}
        />
      </Section>

      {/* Explanation, not just prediction */}
      <Section eyebrow="Explainability" title="Not just prediction — explanation">
        <p>EndoScan is not meant to be a black-box risk score.</p>
        <p>A useful toxicology result should explain the biology behind the signal.</p>
        <p className="font-medium text-ink">For each endpoint result, EndoScan helps answer:</p>
        <div className="grid gap-3 sm:grid-cols-2">
          <QA q="Which genes mattered?">
            See the genes that had the strongest influence on the model result.
          </QA>
          <QA q="Which pathways were affected?">
            Understand whether the response is linked to processes such as hormone signaling,
            inflammation, DNA repair, cell cycle, apoptosis, metabolism, or oxidative stress.
          </QA>
          <QA q="Are there similar known molecules?" badge={<Developing />}>
            Compare your molecule&rsquo;s response with compounds that have already been studied and
            may have known toxicological risks, as the reference layer expands.
          </QA>
          <QA q="What does the literature say?" badge={<Planned />}>
            Future literature support should help connect important genes, pathways, and mechanisms
            to relevant scientific publications.
          </QA>
          <QA q="How reliable is the result?">
            Review model quality, input compatibility, endpoint limitations, and the difference
            between measured data and predicted data.
          </QA>
        </div>
        <p className="text-sm text-muted">
          Gene and pathway context are current analysis layers. Similar-molecule comparison and
          literature support are developing evidence layers, marked above.
        </p>
      </Section>

      {/* How it works */}
      <Section eyebrow="Workflow" title="How it works">
        <ol className="space-y-4">
          <Step n={1} title="Start with data">
            Upload your own transcriptomic signature or explore a molecule with available public
            data.
          </Step>
          <Step n={2} title="Check compatibility">
            EndoScan verifies the gene identifiers, expression values, data columns, coverage, and
            experimental context.
          </Step>
          <Step n={3} title="Run endpoint models">
            The platform applies all compatible toxicology models to the transcriptomic response.
          </Step>
          <Step n={4} title="Review the signals">
            See which endpoints show a potential signal and how strong each result is.
          </Step>
          <Step n={5} title="Understand the biology">
            Explore the genes, pathways, similar compounds, and literature behind the result.
          </Step>
          <Step
            n={6}
            title={
              <>
                Export a report
                <Planned />
              </>
            }
          >
            Save the analysis with methods, model versions, evidence, limitations, and provenance.
          </Step>
        </ol>
      </Section>

      {/* Future direction */}
      <Section eyebrow="Future direction" title="From chemical structure to predicted biological response">
        <p>Today, the main workflow starts with measured transcriptomic data.</p>
        <p>In the future, EndoScan aims to support another route:</p>
        <p className="rounded-lg border border-line bg-surface px-4 py-3 font-mono text-sm text-ink">
          SMILES → predicted transcriptomic response → endpoint models → biological explanation
        </p>
        <p>
          This would allow researchers to screen molecules earlier, even before transcriptomic
          experiments are available.
        </p>
        <p>
          Predicted transcriptomic data will always be clearly marked and separated from real
          experimental data.
        </p>
        <p className="font-medium text-ink">Also on the roadmap:</p>
        <Bullets
          items={[
            "SMILES to predicted transcriptomic response",
            "broader endpoint coverage",
            "better literature support",
            "full multi-endpoint reports",
          ]}
        />
      </Section>

      {/* Current scope — the honest anchor */}
      <section className="border-t border-line py-12">
        <div className="rounded-2xl border border-brand/25 bg-card p-6 shadow-card">
          <p className="text-[11px] font-bold uppercase tracking-widest text-brand">Current scope</p>
          <h2 className="mt-1 text-xl font-bold tracking-tight text-ink">
            What the prototype does today
          </h2>
          <p className="mt-3 text-[15px] leading-relaxed text-muted">
            The current prototype focuses on:
          </p>
          <div className="mt-3 text-[15px] leading-relaxed text-muted">
            <Bullets
              items={[
                "measured transcriptomic signatures",
                "input checking",
                "early endocrine endpoint models, including ER and AR",
                "endpoint scores and thresholds",
                "contributing genes",
                "pathway context",
                "reference signatures",
                "model limitations",
              ]}
            />
          </div>
          <p className="mt-4 text-[15px] leading-relaxed text-muted">
            EndoScan is being built toward a broader platform for preclinical toxicology, with more
            endpoints, more public datasets, better literature support, and full multi-endpoint
            reports.
          </p>
        </div>
      </section>

      {/* Final CTA */}
      <section className="border-t border-line py-12 text-center">
        <h2 className="text-2xl font-bold tracking-tight text-ink">
          Understand toxicity before it becomes a black box
        </h2>
        <p className="mx-auto mt-3 max-w-2xl text-[15px] leading-relaxed text-muted">
          EndoScan helps researchers explore public toxicology data, analyze new molecular
          responses, and understand the biological evidence behind each possible toxicological
          signal.
        </p>
        <div className="mt-6 flex justify-center gap-3">
          <Link
            to="/analyze"
            className="rounded-md bg-brand px-5 py-2.5 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-brand-dark"
          >
            Analyze your data
          </Link>
          <Link
            to="/explore"
            className="rounded-md border border-brand/40 bg-card px-5 py-2.5 text-sm font-semibold text-brand transition-colors hover:bg-surface"
          >
            Explore EndoScan
          </Link>
        </div>
      </section>
    </div>
  );
}
