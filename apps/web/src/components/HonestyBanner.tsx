// Global honesty framing — present on every page (a hard requirement). EndoScan is a research
// prototype; nothing here is a regulatory, clinical, or diagnostic determination.

export function HonestyBanner() {
  return (
    <div
      role="note"
      className="border-b border-warn-line bg-warn-bg text-xs text-warn sm:text-sm"
    >
      <div className="mx-auto flex max-w-6xl items-center gap-2 px-5 py-2">
        <span
          aria-hidden
          className="inline-block h-2 w-2 flex-none rounded-full bg-warn"
        />
        <span>
          <span className="font-semibold">Experimental research prototype.</span> EndoScan is
          experimental pre-screening / prioritization, <span className="font-semibold">not</span> a
          regulatory, clinical, or diagnostic tool.
        </span>
      </div>
    </div>
  );
}

export function HonestyFooter() {
  return (
    <footer className="border-t border-line bg-card">
      <div className="mx-auto max-w-6xl px-5 py-4 text-xs text-muted">
        EndoScan predictions are experimental pre-screening hypotheses under coverage-limited,
        thin-data models — they must be confirmed experimentally and are not a regulatory,
        clinical, or diagnostic determination.
      </div>
    </footer>
  );
}
