// Clean, non-scary rendering of an API error. The API's own message (e.g. the gene-level 422
// text) is shown verbatim; 501/503 are framed as calm "not available" states, not failures.

import { EndoscanApiError } from "../api/client";

const FRIENDLY: Record<string, string> = {
  endpoint_not_found: "That endpoint isn’t registered.",
  invalid_signature: "The signature didn’t match the endpoint’s gene schema.",
  explain_unsupported_for_model: "Explanations aren’t available for this endpoint’s model type.",
  explain_unavailable: "Explanations are temporarily unavailable for this endpoint.",
  model_unavailable: "The model artifact isn’t available right now.",
};

export function ErrorNotice({ error }: { error: unknown }) {
  const isApi = error instanceof EndoscanApiError;
  const code = isApi ? error.error : "error";
  const detail = isApi ? error.detail : error instanceof Error ? error.message : String(error);
  const soft = code === "explain_unsupported_for_model" || code === "explain_unavailable";

  return (
    <div
      role="alert"
      className={`rounded-lg border p-4 text-sm ${
        soft ? "border-line bg-surface text-muted" : "border-red-200 bg-red-50 text-red-800"
      }`}
    >
      <p className="font-medium">{FRIENDLY[code] ?? "Something went wrong."}</p>
      {detail && <p className="mt-1 text-xs break-words">{detail}</p>}
      {isApi && error.request_id !== "unavailable" && (
        <p className="mt-1 text-xs break-words">Request ID: <span className="font-mono">{error.request_id}</span></p>
      )}
    </div>
  );
}
