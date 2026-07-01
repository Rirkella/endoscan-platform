import React from "react";
import ReactDOM from "react-dom/client";
import { RouterProvider, createBrowserRouter } from "react-router-dom";

import App from "./App";
import "./index.css";
import { Analyze } from "./pages/Analyze";
import { EndpointDetailPage } from "./pages/EndpointDetail";
import { EndpointsList } from "./pages/EndpointsList";
import { Landing } from "./pages/Landing";

const router = createBrowserRouter([
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
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
