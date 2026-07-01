import { render } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";

import App from "../App";
import { Analyze } from "../pages/Analyze";
import { EndpointDetailPage } from "../pages/EndpointDetail";
import { EndpointsList } from "../pages/EndpointsList";
import { Landing } from "../pages/Landing";

// Render the real App shell (so the honesty banner/footer are present) at a given route.
export function renderApp(initialPath: string) {
  const router = createMemoryRouter(
    [
      {
        path: "/",
        element: <App />,
        children: [
          { index: true, element: <Landing /> },
          { path: "endpoints", element: <EndpointsList /> },
          { path: "endpoints/:id", element: <EndpointDetailPage /> },
          { path: "analyze", element: <Analyze /> },
        ],
      },
    ],
    { initialEntries: [initialPath] },
  );
  return render(<RouterProvider router={router} />);
}
