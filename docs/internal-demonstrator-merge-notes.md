# Internal demonstrator merge notes

This branch turns the existing EndoScan application into a coherent internal research demonstrator while preserving the transcriptomics-first architecture. It adds real multipart examples, measured-signature catalogue and reference-profile entry points, endpoint-aware analysis, biological-response and evidence views, interactive reference maps, bounded PubMed retrieval, and browser-local guest analysis persistence.

## Validation

- Backend: 111 targeted tests pass across startup/health, parsing and analysis, explanations, biological response, pathways, UMAP/locate and exact reference retrieval, catalogue, PubMed failure/rate/cache behavior, inference, and affected explore/identity jobs.
- Frontend: all 81 Vitest tests pass; TypeScript typecheck and the Vite production build pass.
- Shared-browser smoke: Caffeic Acid, Closantel, and an InChIKey-only exact reference profile complete against the real local stack. Back/Projects restore cached results without repeating model or literature requests; console errors and HTTP 500 responses are zero.
- `main` synchronization: remote `main` (`86e28e5`) is the branch merge base; the branch is 14 commits ahead and 0 behind before this note.

## Honest limitations

- EndoScan remains an experimental pre-screening and prioritization tool. It is not regulatory-grade, clinical, diagnostic, or a safety determination.
- Endpoint scores are model signals, not calibrated real-world probabilities. Current committed endpoint coverage is limited and applicability-domain information must be read with each result.
- Analysis requires a measured transcriptomic signature. Catalogue identities and reference-map points only retrieve committed measured profiles; molecule identity is never converted directly into risk.
- Reference profiles aggregate selected LINCS MCF7 and A549 conditions. Underlying per-condition details are not available in the committed aggregate, and opposing cell-specific responses may be attenuated.
- Supporting literature is live, bounded PubMed retrieval rather than a persistent literature index. It requires `NCBI_EMAIL`, may be unavailable or rate-limited, and does not establish causality.
- Guest analyses are scoped to the current browser session and stored in IndexedDB with an in-memory fallback. There are no accounts, server synchronization, collaboration, or cross-device recovery.
- The production build currently warns about a 538 kB minified JavaScript chunk, and the landing image is approximately 1.9 MB; code splitting and image optimization remain performance follow-ups.
