// Dependency-free interactive SVG over real committed UMAP coordinates. Points are never fitted,
// projected, renamed, or invented here; this component only zooms, pans, filters, and selects them.

import {
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type WheelEvent,
  useRef,
  useState,
} from "react";

import type { ExploreLocateResult, ExplorePoint } from "../api/types";

const W = 520;
const H = 360;
const PAD = 24;

const FILL: Record<string, string> = {
  active: "#b45309",
  inactive: "#475569",
};
const UNLABELED = "#cbd5e1";

interface Props {
  points: ExplorePoint[];
  locate?: ExploreLocateResult | null;
  selectedId?: string | null;
  pointNames?: Record<string, string>;
  labelFilter?: "all" | "active" | "inactive" | "unlabeled";
  onSelect?: (point: ExplorePoint) => void;
}

interface ViewBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

function scaleFor(points: ExplorePoint[]) {
  const xs = points.map((point) => point.x);
  const ys = points.map((point) => point.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const spanX = maxX - minX || 1;
  const spanY = maxY - minY || 1;
  const sx = (x: number) => PAD + ((x - minX) / spanX) * (W - 2 * PAD);
  const sy = (y: number) => H - PAD - ((y - minY) / spanY) * (H - 2 * PAD);
  return { sx, sy };
}

export function ExploreScatter({
  points,
  locate,
  selectedId,
  pointNames = {},
  labelFilter = "all",
  onSelect,
}: Props) {
  const [view, setView] = useState<ViewBox>({ x: 0, y: 0, width: W, height: H });
  const panStart = useRef<{
    clientX: number;
    clientY: number;
    view: ViewBox;
  } | null>(null);

  if (points.length === 0) return null;
  const { sx, sy } = scaleFor(points);
  const neighborIds = new Set(locate?.neighbors.map((neighbor) => neighbor.compound_id) ?? []);

  function zoomAt(factor: number, focusX?: number, focusY?: number) {
    setView((current) => {
      const nextWidth = Math.min(W, Math.max(W * 0.2, current.width * factor));
      const nextHeight = (nextWidth / W) * H;
      const x = focusX ?? current.x + current.width / 2;
      const y = focusY ?? current.y + current.height / 2;
      const ratio = nextWidth / current.width;
      return {
        x: Math.min(W - nextWidth, Math.max(0, x - (x - current.x) * ratio)),
        y: Math.min(H - nextHeight, Math.max(0, y - (y - current.y) * ratio)),
        width: nextWidth,
        height: nextHeight,
      };
    });
  }

  function onWheel(event: WheelEvent<SVGSVGElement>) {
    event.preventDefault();
    const rect = event.currentTarget.getBoundingClientRect();
    const focusX = view.x + ((event.clientX - rect.left) / (rect.width || 1)) * view.width;
    const focusY = view.y + ((event.clientY - rect.top) / (rect.height || 1)) * view.height;
    zoomAt(event.deltaY < 0 ? 0.8 : 1.25, focusX, focusY);
  }

  function startPan(event: ReactPointerEvent<SVGSVGElement>) {
    if ((event.target as SVGElement).tagName.toLowerCase() !== "svg") return;
    panStart.current = { clientX: event.clientX, clientY: event.clientY, view };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function movePan(event: ReactPointerEvent<SVGSVGElement>) {
    if (!panStart.current) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const initial = panStart.current.view;
    const dx = ((event.clientX - panStart.current.clientX) / (rect.width || 1)) * initial.width;
    const dy = ((event.clientY - panStart.current.clientY) / (rect.height || 1)) * initial.height;
    setView({
      ...initial,
      x: Math.min(W - initial.width, Math.max(0, initial.x - dx)),
      y: Math.min(H - initial.height, Math.max(0, initial.y - dy)),
    });
  }

  function selectWithKeyboard(event: KeyboardEvent<SVGCircleElement>, point: ExplorePoint) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onSelect?.(point);
    }
  }

  return (
    <div className="explore-scatter-wrap">
      <div className="explore-zoom-controls" aria-label="Map zoom controls">
        <button type="button" aria-label="Zoom in" onClick={() => zoomAt(0.8)}>+</button>
        <button type="button" aria-label="Zoom out" onClick={() => zoomAt(1.25)}>−</button>
        <button type="button" onClick={() => setView({ x: 0, y: 0, width: W, height: H })}>
          Reset
        </button>
      </div>
      <svg
        role="img"
        aria-label="Interactive data-space map of measured compound signatures; scroll to zoom and drag to pan"
        viewBox={`${view.x} ${view.y} ${view.width} ${view.height}`}
        className="explore-scatter-svg"
        data-testid="explore-scatter"
        onWheel={onWheel}
        onPointerDown={startPan}
        onPointerMove={movePan}
        onPointerUp={() => { panStart.current = null; }}
        onPointerCancel={() => { panStart.current = null; }}
      >
        {points.map((point) => {
          const highlighted = neighborIds.has(point.compound_id);
          const selected = selectedId === point.compound_id;
          const pointLabel = point.label ?? "unlabeled";
          const visible = labelFilter === "all" || labelFilter === pointLabel;
          const fill = point.label ? (FILL[point.label] ?? UNLABELED) : UNLABELED;
          const name = pointNames[point.compound_id] ?? point.preferred_name;
          return (
            <circle
              key={point.compound_id}
              role="button"
              tabIndex={visible ? 0 : -1}
              aria-label={`${name ?? "Unresolved reference record"}, ${point.label ? `${point.label} dataset label` : "unlabelled"}`}
              data-compound={point.compound_id}
              data-label={pointLabel}
              data-neighbor={highlighted ? "true" : undefined}
              data-selected={selected ? "true" : undefined}
              display={visible ? undefined : "none"}
              cx={sx(point.x)}
              cy={sy(point.y)}
              r={selected ? 7.5 : highlighted ? 6 : 3.5}
              fill={fill}
              fillOpacity={selected || highlighted ? 1 : 0.7}
              stroke={selected ? "#0369a1" : highlighted ? "#111827" : "none"}
              strokeWidth={selected ? 2.5 : highlighted ? 1.5 : 0}
              onClick={() => onSelect?.(point)}
              onKeyDown={(event) => selectWithKeyboard(event, point)}
            >
              <title>
                {name ? `${name} — ` : ""}{point.compound_id}. Condition-selected,
                cell-line-fused measured compound signature; dataset class: {point.label ??
                  "unlabelled"}. Select for details.
              </title>
            </circle>
          );
        })}

        {locate && (
          <g data-testid={locate.exact_match ? "explore-exact-marker" : "explore-approx-marker"}>
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
            <title>
              {locate.exact_match
                ? "Exact existing reference record at its stored map coordinates"
                : "Approximate position based on the most similar known signatures"}
            </title>
          </g>
        )}
      </svg>
    </div>
  );
}
