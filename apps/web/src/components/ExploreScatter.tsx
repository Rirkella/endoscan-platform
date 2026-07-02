// A dependency-free inline-SVG scatter of the data-space (UMAP) map. Deliberately NOT a
// charting lib: a hand-rolled SVG renders deterministically in jsdom (so points are testable)
// and adds zero deps. UMAP is a VISUALIZATION only — the caption and page say so; this
// component draws real training points and (optionally) highlights nearest neighbours + an
// APPROXIMATE placement marker for a submitted signature. It never invents points.

import type { ExploreLocateResult, ExplorePoint } from "../api/types";

const W = 520;
const H = 360;
const PAD = 24;

// Accessible, honesty-neutral fills (colour-blind-safe amber vs slate); unlabeled stays grey.
const FILL: Record<string, string> = {
  active: "#b45309", // amber-700
  inactive: "#475569", // slate-600
};
const UNLABELED = "#cbd5e1"; // slate-300

interface Props {
  points: ExplorePoint[];
  locate?: ExploreLocateResult | null;
}

function useScale(points: ExplorePoint[]) {
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const spanX = maxX - minX || 1;
  const spanY = maxY - minY || 1;
  const sx = (x: number) => PAD + ((x - minX) / spanX) * (W - 2 * PAD);
  // SVG y grows downward — flip so the map reads like a normal plot.
  const sy = (y: number) => H - PAD - ((y - minY) / spanY) * (H - 2 * PAD);
  return { sx, sy };
}

export function ExploreScatter({ points, locate }: Props) {
  if (points.length === 0) return null;
  const { sx, sy } = useScale(points);
  const neighborIds = new Set(locate?.neighbors.map((n) => n.compound_id) ?? []);

  return (
    <svg
      role="img"
      aria-label="Data-space map of training signatures (UMAP projection)"
      viewBox={`0 0 ${W} ${H}`}
      className="w-full rounded-md border border-line bg-white"
      data-testid="explore-scatter"
    >
      {points.map((p) => {
        const highlighted = neighborIds.has(p.compound_id);
        const fill = p.label ? (FILL[p.label] ?? UNLABELED) : UNLABELED;
        return (
          <circle
            key={p.compound_id}
            data-compound={p.compound_id}
            data-label={p.label ?? "unlabeled"}
            data-neighbor={highlighted ? "true" : undefined}
            cx={sx(p.x)}
            cy={sy(p.y)}
            r={highlighted ? 6 : 3.5}
            fill={fill}
            fillOpacity={highlighted ? 0.95 : 0.7}
            stroke={highlighted ? "#111827" : "none"}
            strokeWidth={highlighted ? 1.5 : 0}
          >
            <title>
              {p.compound_id}
              {p.label ? ` — ${p.label}` : ""}
            </title>
          </circle>
        );
      })}

      {locate && (
        // Approximate placement of the submitted signature = centroid of its neighbours'
        // precomputed coords. A distinct marker, explicitly labelled — NOT an exact projection.
        <g data-testid="explore-approx-marker">
          <circle
            cx={sx(locate.approx_xy.x)}
            cy={sy(locate.approx_xy.y)}
            r={7}
            fill="none"
            stroke="#7c3aed"
            strokeWidth={2}
          />
          <line
            x1={sx(locate.approx_xy.x) - 10}
            y1={sy(locate.approx_xy.y)}
            x2={sx(locate.approx_xy.x) + 10}
            y2={sy(locate.approx_xy.y)}
            stroke="#7c3aed"
            strokeWidth={1.5}
          />
          <line
            x1={sx(locate.approx_xy.x)}
            y1={sy(locate.approx_xy.y) - 10}
            x2={sx(locate.approx_xy.x)}
            y2={sy(locate.approx_xy.y) + 10}
            stroke="#7c3aed"
            strokeWidth={1.5}
          />
          <title>Approximate position based on the most similar known signatures</title>
        </g>
      )}
    </svg>
  );
}
