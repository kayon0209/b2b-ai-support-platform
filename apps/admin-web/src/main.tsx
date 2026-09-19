import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";

import { RouteError } from "./components/ErrorBoundary";
import { Layout } from "./components/Layout";
import { Branding } from "./pages/Branding";
import { Cases } from "./pages/Cases";
import { FeatureFlags } from "./pages/FeatureFlags";
import { GapQueue } from "./pages/GapQueue";
import { Members } from "./pages/Members";
import { NotFound } from "./pages/NotFound";
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
    // Catches a failure in the shell itself (the sidebar, the language
    // provider). A failure inside a page is caught by the boundary around
    // <Outlet/>, so the shell stays on screen and the operator can still
    // navigate away.
    errorElement: <RouteError />,
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
      { path: "*", element: <NotFound /> },
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
