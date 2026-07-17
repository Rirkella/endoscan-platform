import React from "react";
import ReactDOM from "react-dom/client";
import { RouterProvider, createBrowserRouter } from "react-router-dom";

import App from "./App";
import "./index.css";
import "./prototype.css"; // ported prototype-v2 visual system (shell, landing, analyze, cards)
import { Analyze } from "./pages/Analyze";
import { Explore } from "./pages/Explore";
import { Landing } from "./pages/Landing";
import { ModelEvidence } from "./pages/ModelEvidence";
import { ModelLibrary } from "./pages/ModelLibrary";
import { Projects } from "./pages/Projects";

const router = createBrowserRouter([
  // The landing renders full-bleed, OUTSIDE the workspace shell.
  { path: "/", element: <Landing /> },
  // The workspace: a pathless layout route (sidebar + topbar) wrapping the app screens.
  {
    element: <App />,
    children: [
      { path: "/analyze", element: <Analyze /> },
      { path: "/analyze/:analysisId", element: <Analyze /> },
      { path: "/projects", element: <Projects /> },
      { path: "/library", element: <ModelLibrary /> },
      { path: "/library/:id", element: <ModelEvidence /> },
      { path: "/explore", element: <Explore /> },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
