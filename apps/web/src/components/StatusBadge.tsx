// Status badge, driven purely by the API's status string. `experimental` (and any non-validated
// status) is amber/provisional; green is RESERVED for a hypothetical future `validated_mvp`.
// We never assume ER/AR specifics — whatever status the API returns is what we render.

const STYLES: Record<string, string> = {
  validated_mvp: "bg-green-100 text-green-800 border-green-300",
  experimental: "bg-amber-100 text-amber-900 border-amber-300",
};

function labelFor(status: string): string {
  return status
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

export function StatusBadge({ status }: { status: string }) {
  const style = STYLES[status] ?? "bg-slate-100 text-slate-700 border-slate-300";
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium ${style}`}
      title={`Model status: ${status}`}
    >
      {labelFor(status)}
    </span>
  );
}
