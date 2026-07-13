// Status badge, driven purely by the API's status string. `experimental` (and any non-validated
// status) is amber/provisional; green is RESERVED for a hypothetical future `validated_mvp`.
// We never assume ER/AR specifics — whatever status the API returns is what we render.

const STYLES: Record<string, string> = {
  // `success` (emerald) is RESERVED for a validated endpoint — deliberately unused in the live UI.
  validated_mvp: "bg-success/10 text-success border-success/30",
  // Experimental / provisional — amber, neither alarming nor reassuring.
  experimental: "bg-warn-bg text-warn border-warn-line",
};

function labelFor(status: string): string {
  return status
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

export function StatusBadge({ status }: { status: string }) {
  const style = STYLES[status] ?? "bg-surface text-muted border-line";
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide ${style}`}
      title={`Model status: ${status}`}
    >
      {labelFor(status)}
    </span>
  );
}
