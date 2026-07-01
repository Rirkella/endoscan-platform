// Signature file upload — PRESENT BUT DISABLED (Phase 2 wires parsing). The product direction
// treats upload as a primary input, so the UI anticipates it here; no file handler is attached
// and the control is aria-disabled. Do NOT add parsing in Phase 1.

export function UploadZone() {
  return (
    <div
      aria-disabled="true"
      className="cursor-not-allowed rounded-md border-2 border-dashed border-line bg-surface px-4 py-6 text-center opacity-70"
      title="Coming in the next update"
    >
      <p className="text-sm font-medium text-muted">Upload a signature file (CSV/TSV)</p>
      <p className="mt-1 text-xs text-muted">
        Coming in the next update — for now use a demo example or paste JSON below.
      </p>
    </div>
  );
}
