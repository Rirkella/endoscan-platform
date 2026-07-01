// Global honesty framing — present on every page (a hard requirement). EndoScan is a research
// prototype; nothing here is a regulatory, clinical, or diagnostic determination.

export function HonestyBanner() {
  return (
    <div
      role="note"
      className="bg-amber-50 border-b border-amber-200 text-amber-900 text-xs sm:text-sm"
    >
      <div className="mx-auto max-w-5xl px-4 py-2">
        <span className="font-semibold">Experimental research prototype.</span> EndoScan is
        experimental pre-screening / prioritization, <span className="font-semibold">not</span> a
        regulatory, clinical, or diagnostic tool.
      </div>
    </div>
  );
}

export function HonestyFooter() {
  return (
    <footer className="border-t border-line bg-white">
      <div className="mx-auto max-w-5xl px-4 py-4 text-xs text-muted">
        EndoScan predictions are experimental pre-screening hypotheses under coverage-limited,
        thin-data models — they must be confirmed experimentally and are not a regulatory,
        clinical, or diagnostic determination.
      </div>
    </footer>
  );
}
