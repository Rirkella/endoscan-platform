import { render } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";

import App from "../App";
import { Analyze } from "../pages/Analyze";
import { ModelEvidence } from "../pages/ModelEvidence";
import { ModelLibrary } from "../pages/ModelLibrary";

// Render the real App shell (so the honesty banner/footer are present) at a given route.
export function renderApp(initialPath: string) {
  const router = createMemoryRouter(
    [
      {
        path: "/",
        element: <App />,
        children: [
          { index: true, element: <Analyze /> },
          { path: "library", element: <ModelLibrary /> },
          { path: "library/:id", element: <ModelEvidence /> },
        ],
      },
    ],
    { initialEntries: [initialPath] },
  );
  return render(<RouterProvider router={router} />);
}
