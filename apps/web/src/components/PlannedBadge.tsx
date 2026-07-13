// Small honesty chips used across the ported UI to mark areas the backend does not support yet.
//  - PlannedBadge: a capability that is designed but not built (amber, "Planned").
//  - MockBadge: content shown from src/mock illustrative data, not the API ("Example data").
// These keep the prototype's polish while making "not real yet" unmistakable to the user.

export function PlannedBadge({ label = "Planned" }: { label?: string }) {
  return <span className="planned-badge">{label}</span>;
}

export function MockBadge({ label = "Example data" }: { label?: string }) {
  return <span className="mock-badge">{label}</span>;
}
