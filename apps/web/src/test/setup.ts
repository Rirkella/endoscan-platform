import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

import { guestAnalysisRepository } from "../session/guestAnalysis";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  guestAnalysisRepository.__resetForTests();
  sessionStorage.clear();
});
