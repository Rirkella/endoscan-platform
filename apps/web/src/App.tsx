import { Link, NavLink, Outlet } from "react-router-dom";

import { HonestyBanner, HonestyFooter } from "./components/HonestyBanner";

function navClass({ isActive }: { isActive: boolean }): string {
  return [
    "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
    isActive
      ? "bg-brand text-white shadow-sm"
      : "text-muted hover:text-ink hover:bg-line/70",
  ].join(" ");
}

export default function App() {
  return (
    <div className="flex min-h-screen flex-col">
      <HonestyBanner />
      <header className="border-b border-line bg-card">
        <div className="mx-auto flex max-w-6xl items-center gap-4 px-5 py-3">
          <Link to="/" className="flex items-center gap-2" aria-label="EndoScan home">
            <span
              aria-hidden
              className="grid h-7 w-7 place-items-center rounded-md border border-brand/40 bg-brand text-[11px] font-bold text-white"
            >
              ES
            </span>
            <span className="text-[17px] font-bold tracking-tight text-ink">
              Endo<span className="text-brand">Scan</span>
            </span>
          </Link>
          <span className="hidden text-xs text-muted sm:inline">
            experimental transcriptomic pre-screening
          </span>
          <nav className="ml-auto flex items-center gap-1">
            <NavLink to="/analyze" className={navClass}>
              Analyze
            </NavLink>
            <NavLink to="/library" className={navClass}>
              Model Library
            </NavLink>
            <NavLink to="/explore" className={navClass}>
              Explore
            </NavLink>
          </nav>
        </div>
      </header>

      <main className="flex-1">
        <div className="mx-auto max-w-6xl px-5 py-8">
          <Outlet />
        </div>
      </main>

      <HonestyFooter />
    </div>
  );
}
