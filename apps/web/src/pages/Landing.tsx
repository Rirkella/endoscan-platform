import { Link } from "react-router-dom";

export function Landing() {
  return (
    <div className="space-y-8">
      <section>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">
          Transcriptomic pre-screening for endocrine activity
        </h1>
        <p className="mt-3 max-w-2xl text-muted">
          EndoScan estimates whether a transcriptomic signature is consistent with activity at an
          endocrine receptor endpoint. It is a <strong>research prototype</strong> built on
          coverage-limited, thin-data models — every endpoint here is{" "}
          <strong>experimental</strong>, and every result carries its honest limitations.
        </p>
      </section>

      <section className="rounded-lg border border-line bg-white p-5">
        <h2 className="text-sm font-semibold text-ink">What EndoScan is — and is not</h2>
        <ul className="mt-2 space-y-1 text-sm text-muted list-disc pl-5">
          <li>
            <strong>Is:</strong> a prioritization / pre-screening tool over transcriptomic
            signatures, with explicit uncertainty and scope on every prediction.
          </li>
          <li>
            <strong>Is not:</strong> a regulatory, clinical, or diagnostic instrument; it does not
            declare a compound &ldquo;toxic&rdquo; or &ldquo;safe&rdquo;.
          </li>
        </ul>
      </section>

      <div className="flex flex-wrap gap-3">
        <Link
          to="/endpoints"
          className="rounded-md bg-brand px-4 py-2 text-sm font-medium text-white"
        >
          Browse endpoints
        </Link>
        <Link
          to="/analyze"
          className="rounded-md border border-line bg-white px-4 py-2 text-sm font-medium text-ink"
        >
          Analyze a signature
        </Link>
      </div>
    </div>
  );
}
