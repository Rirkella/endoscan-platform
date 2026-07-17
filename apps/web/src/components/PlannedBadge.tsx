// Small honesty chip used to mark a capability that is designed but not built yet.

export function PlannedBadge({ label = "Planned" }: { label?: string }) {
  return <span className="planned-badge">{label}</span>;
}
