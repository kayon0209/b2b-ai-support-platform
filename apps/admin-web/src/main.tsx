import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";

import { Layout } from "./components/Layout";
import { Branding } from "./pages/Branding";
import { Cases } from "./pages/Cases";
import { FeatureFlags } from "./pages/FeatureFlags";
import { GapQueue } from "./pages/GapQueue";
import { Members } from "./pages/Members";
import { PromptRelease } from "./pages/PromptRelease";
import { QualityDashboard } from "./pages/QualityDashboard";
import { Usage } from "./pages/Usage";
import "./styles.css";

/**
 * Route table mirrors the sidebar in components/Layout.tsx. Every page talks
 * to an endpoint that already exists on the control plane; the admin UI adds
 * no backend surface of its own.
 */
const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <Navigate to="/quality" replace /> },
      { path: "quality", element: <QualityDashboard /> },
      { path: "gaps", element: <GapQueue /> },
      { path: "prompts", element: <PromptRelease /> },
      { path: "flags", element: <FeatureFlags /> },
      { path: "cases", element: <Cases /> },
      { path: "members", element: <Members /> },
      { path: "usage", element: <Usage /> },
      { path: "branding", element: <Branding /> },
      { path: "*", element: <Navigate to="/quality" replace /> },
    ],
  },
]);

const container = document.getElementById("root");
if (!container) {
  throw new Error("#root element missing from index.html");
}

createRoot(container).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
