// Workspace shell — ported from prototype-v2 (sidebar + topbar + page stage). The landing page
// renders full-bleed OUTSIDE this shell (see main.tsx); everything under the workspace uses this
// chrome. Navigation is real React-Router routes, not the prototype's local view state. Honesty
// framing is kept: a persistent "Experimental models" status in the topbar and a disclaimer footer
// that states EndoScan is not a regulatory, clinical, or diagnostic tool.

import { useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";

interface NavItem {
  to: string;
  label: string;
  short: string;
}

// Sidebar workspace nav. Labels mirror the prototype; targets are the real routes. "Projects" is a
// planned (mock) area — the screen itself is clearly marked, so the nav stays honest.
const NAV: NavItem[] = [
  { to: "/analyze", label: "Analyze", short: "01" },
  { to: "/projects", label: "Projects", short: "02" },
  { to: "/library", label: "Model library", short: "03" },
  { to: "/explore", label: "Reference data", short: "04" },
];

function currentLabel(pathname: string): string {
  // Longest matching route prefix wins (so /library/:id still reads "Model library").
  const match = [...NAV]
    .sort((a, b) => b.to.length - a.to.length)
    .find((n) => pathname.startsWith(n.to));
  return match?.label ?? "Workspace";
}

export default function App() {
  const [mobileNav, setMobileNav] = useState(false);
  const { pathname } = useLocation();

  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileNav ? "sidebar-open" : ""}`}>
        <div className="brand-row">
          <Link
            to="/"
            className="brand"
            aria-label="EndoScan home"
            onClick={() => setMobileNav(false)}
          >
            <span className="brand-mark">E</span>
            <span>
              Endo<span>Scan</span>
            </span>
          </Link>
          <button
            className="mobile-close"
            onClick={() => setMobileNav(false)}
            aria-label="Close navigation"
          >
            Close
          </button>
        </div>

        <p className="nav-label">Workspace</p>
        <nav className="primary-nav" aria-label="Main navigation">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => (isActive ? "nav-active" : "")}
              onClick={() => setMobileNav(false)}
            >
              <span>{item.label}</span>
              <small>{item.short}</small>
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className="workspace-avatar">ES</div>
          <div>
            <strong>Research workspace</strong>
            <span>Experimental build</span>
          </div>
        </div>
      </aside>

      {mobileNav && (
        <button
          className="nav-scrim"
          onClick={() => setMobileNav(false)}
          aria-label="Close navigation"
        />
      )}

      <main className="main-stage">
        <header className="topbar">
          <button
            className="mobile-menu"
            onClick={() => setMobileNav(true)}
            aria-label="Open navigation"
          >
            Menu
          </button>
          <div className="breadcrumb">
            <span>EndoScan</span>
            <b>/</b>
            {currentLabel(pathname)}
          </div>
          <div className="topbar-actions">
            <span className="experimental" role="note">
              <span className="status-dot status-dot-amber" aria-hidden />
              Experimental models
            </span>
          </div>
        </header>

        <div className="page-stage">
          <Outlet />
        </div>

        <footer className="landing-footer">
          <span>EndoScan / transcriptomics-first endocrine pre-screening</span>
          <span>
            Experimental research use only — not a regulatory, clinical, or diagnostic tool
          </span>
        </footer>
      </main>
    </div>
  );
}
