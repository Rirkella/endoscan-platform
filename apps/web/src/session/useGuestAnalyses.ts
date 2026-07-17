import { useCallback, useEffect, useState } from "react";

import {
  getActiveAnalysisId,
  guestAnalysisRepository,
  type GuestAnalysisRecord,
} from "./guestAnalysis";

export function useGuestAnalyses(): {
  analyses: GuestAnalysisRecord[];
  loading: boolean;
  error: unknown;
  reload: () => void;
} {
  const [analyses, setAnalyses] = useState<GuestAnalysisRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const reload = useCallback(() => {
    setLoading(true);
    void guestAnalysisRepository.list()
      .then((items) => { setAnalyses(items); setError(null); })
      .catch(setError)
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    reload();
    window.addEventListener("endoscan:workspace-changed", reload);
    return () => window.removeEventListener("endoscan:workspace-changed", reload);
  }, [reload]);
  return { analyses, loading, error, reload };
}

export function useActiveAnalysisId(): string | null {
  const [id, setId] = useState(getActiveAnalysisId());
  useEffect(() => {
    const update = () => setId(getActiveAnalysisId());
    window.addEventListener("endoscan:workspace-changed", update);
    return () => window.removeEventListener("endoscan:workspace-changed", update);
  }, []);
  return id;
}
