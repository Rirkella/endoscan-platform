import { Link, NavLink, Outlet } from "react-router-dom";

import { HonestyBanner, HonestyFooter } from "./components/HonestyBanner";

function navClass({ isActive }: { isActive: boolean }): string {
  return [
    "px-3 py-2 rounded-md text-sm font-medium transition-colors",
    isActive ? "bg-brand text-white" : "text-muted hover:text-ink hover:bg-line",
  ].join(" ");
}

export default function App() {
  return (
    <div className="min-h-screen flex flex-col">
      <HonestyBanner />
      <header className="border-b border-line bg-white">
        <div className="mx-auto max-w-5xl px-4 py-3 flex items-center gap-4">
          <Link to="/" className="font-semibold text-ink tracking-tight">
            Endo<span className="text-brand">Scan</span>
          </Link>
          <span className="text-xs text-muted hidden sm:inline">
            experimental transcriptomic pre-screening
          </span>
          <nav className="ml-auto flex items-center gap-1">
            <NavLink to="/" end className={navClass}>
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
        <div className="mx-auto max-w-5xl px-4 py-8">
          <Outlet />
        </div>
      </main>

      <HonestyFooter />
    </div>
  );
}
