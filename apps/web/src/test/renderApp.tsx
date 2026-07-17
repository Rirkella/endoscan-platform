import { render } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";

import App from "../App";
import { Analyze } from "../pages/Analyze";
import { AdminEndpointDetail } from "../pages/AdminEndpointDetail";
import { AdminEndpoints } from "../pages/AdminEndpoints";
import { Explore } from "../pages/Explore";
import { Landing } from "../pages/Landing";
import { ModelEvidence } from "../pages/ModelEvidence";
import { ModelLibrary } from "../pages/ModelLibrary";
import { Projects } from "../pages/Projects";

// Mirror main.tsx: the landing renders full-bleed at "/", and the workspace screens render inside
// the App shell (sidebar + topbar) via a pathless layout route.
export function renderApp(initialPath: string) {
  const router = createMemoryRouter(
    [
      { path: "/", element: <Landing /> },
      {
        element: <App />,
        children: [
          { path: "/analyze", element: <Analyze /> },
          { path: "/analyze/:analysisId", element: <Analyze /> },
          { path: "/projects", element: <Projects /> },
          { path: "/library", element: <ModelLibrary /> },
          { path: "/library/:id", element: <ModelEvidence /> },
          { path: "/explore", element: <Explore /> },
          { path: "/admin/endpoints", element: <AdminEndpoints /> },
          { path: "/admin/endpoints/:buildId", element: <AdminEndpointDetail /> },
        ],
      },
    ],
    { initialEntries: [initialPath] },
  );
  return render(<RouterProvider router={router} />);
}
