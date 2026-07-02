import React from "react";
import ReactDOM from "react-dom/client";
import { RouterProvider, createBrowserRouter } from "react-router-dom";

import App from "./App";
import "./index.css";
import { Analyze } from "./pages/Analyze";
import { Explore } from "./pages/Explore";
import { ModelEvidence } from "./pages/ModelEvidence";
import { ModelLibrary } from "./pages/ModelLibrary";

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <Analyze /> }, // Analyze is the default landing flow
      { path: "library", element: <ModelLibrary /> },
      { path: "library/:id", element: <ModelEvidence /> },
      { path: "explore", element: <Explore /> },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
