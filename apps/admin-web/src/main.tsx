import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import {
  createBrowserRouter,
  Navigate,
  RouterProvider,
  useLocation,
} from "react-router-dom";

import { RouteError } from "./components/ErrorBoundary";
import { Layout } from "./components/Layout";
import { LangProvider } from "./lib/i18n";
import { Approvals } from "./pages/Approvals";
import { Branding } from "./pages/Branding";
import { Cases } from "./pages/Cases";
import { CustomerChat } from "./pages/CustomerChat";
import { Channels } from "./pages/Channels";
import { Experiments } from "./pages/Experiments";
import { FeatureFlags } from "./pages/FeatureFlags";
import { GapQueue } from "./pages/GapQueue";
import { Conversations } from "./pages/Conversations";
import { Knowledge } from "./pages/Knowledge";
import { Landing } from "./pages/Landing";
import { Members } from "./pages/Members";
import { NotFound } from "./pages/NotFound";
import { PromptRelease } from "./pages/PromptRelease";
import { QualityDashboard } from "./pages/QualityDashboard";
import { SupportChat } from "./pages/SupportChat";
import { SupportNotFound } from "./pages/SupportNotFound";
import { Usage } from "./pages/Usage";
import { Workbench } from "./pages/Workbench";
import "./styles.css";

/**
 * Route table mirrors the sidebar in components/Layout.tsx. Every page talks
 * to an endpoint that already exists on the control plane; the admin UI adds
 * no backend surface of its own.
 *
 * The console lives under **`/admin/*`**, so the root namespace belongs to the
 * customer. Before this, the console owned `/quality`, `/cases` and twelve
 * other top-level names, and a customer who mistyped the support address
 * (`/suport`) landed on the operator's 404 with the full admin sidebar beside
 * it. `/support/*` covers the near-misses *inside* the support path; a prefix
 * is what covers everything else, and it is why this change was held back until
 * the URL contract was signed off.
 *
 * Every old address still resolves, through the redirect routes built from
 * `OPERATOR_PAGES` below, so a bookmark or a documented link keeps working.
 *
 * `/` is the customer's landing page, not a redirect to the console. It used
 * to be `<Navigate to="/quality" />`, which put the operator's dashboard - and
 * its bearer-token prompt - in front of anyone who opened the domain. The
 * console is one link away from that page for staff; see `pages/Landing.tsx`.
 *
 * `/chat` is deliberately OUTSIDE the layout too — it is the internal
 * verification panel, not the customer surface. Hiding it inside Layout would
 * show the admin sidebar to the very person we are trying to help.
 */
const OPERATOR_PAGES = [
  { path: "quality", element: <QualityDashboard /> },
  { path: "gaps", element: <GapQueue /> },
  { path: "knowledge", element: <Knowledge /> },
  { path: "conversations", element: <Conversations /> },
  { path: "prompts", element: <PromptRelease /> },
  { path: "flags", element: <FeatureFlags /> },
  { path: "channels", element: <Channels /> },
  { path: "experiments", element: <Experiments /> },
  { path: "cases", element: <Cases /> },
  { path: "workbench", element: <Workbench /> },
  // The case id is part of the address so a refresh keeps the agent where they
  // were and a link can be shared. `/admin/workbench` alone still works - the
  // page then picks the first case as before.
  { path: "workbench/:caseId", element: <Workbench /> },
  { path: "approvals", element: <Approvals /> },
  { path: "members", element: <Members /> },
  { path: "usage", element: <Usage /> },
  { path: "branding", element: <Branding /> },
];

/**
 * Send an old operator address to its `/admin` home, keeping the rest of the
 * path.
 *
 * A plain `<Navigate to="/admin/workbench" />` would drop the case id, so a
 * bookmarked `/workbench/123` would land on the workbench with no case open -
 * the bookmark would appear to work and quietly show something else, which is
 * the failure mode the console's own catch-all comment warns about.
 */
function LegacyOperatorRedirect({ to }: { to: string }) {
  const { pathname, search } = useLocation();
  const rest = pathname.replace(/^\/[^/]+/, "");
  return <Navigate to={`${to}${rest}${search}`} replace />;
}

const router = createBrowserRouter([
  {
    // Pathless: the operator shell wraps the `/admin` URLs without adding a
    // segment of its own, so the prefix comes from the child paths alone.
    element: <Layout />,
    // Catches a failure in the shell itself (the sidebar, the language
    // provider). A failure inside a page is caught by the boundary around
    // <Outlet/>, so the shell stays on screen and the operator can still
    // navigate away.
    errorElement: <RouteError />,
    children: [
      ...OPERATOR_PAGES.map((page) => ({ ...page, path: `admin/${page.path}` })),
      // Anything else **under `/admin`**. Scoped to the prefix on purpose: an
      // unknown console URL keeps the sidebar and can navigate away, while an
      // unknown URL anywhere else is not the operator's problem. A bare `*`
      // here caught the whole site, which is how a customer who mistyped
      // `/support` as `/suport` ended up looking at the fourteen-item admin
      // sidebar. Caught by `admin_render_check.cjs`'s `expectAbsent` case.
      { path: "admin/*", element: <NotFound /> },
    ],
  },
  // The pre-`/admin` addresses. Top level rather than inside the shell so a
  // redirect does not paint the sidebar on the way through, and declared
  // before the customer routes so a static segment still wins over the shell's
  // catch-all.
  ...OPERATOR_PAGES.map((page) => ({
    path: page.path,
    element: <LegacyOperatorRedirect to={`/admin/${page.path}`} />,
  })),
  {
    // The customer's entry point. Top-level, so it never inherits the sidebar.
    path: "/",
    element: <Landing />,
    errorElement: <RouteError />,
  },
  {
    // Customer-facing chat. Top-level so it never inherits the operator
    // sidebar. Wrapped in its own LangProvider because it is no longer a
    // child of the Layout that carries the admin shell's provider.
    path: "/chat",
    element: (
      <LangProvider>
        <CustomerChat />
      </LangProvider>
    ),
    errorElement: <RouteError />,
  },
  {
    // The real customer surface (ADR 0011): no operator token, no operator
    // layout. `/chat` above is the internal verification panel and keeps its
    // operator auth; this one opens a visitor session instead.
    path: "/support",
    element: <SupportChat />,
    errorElement: <RouteError />,
  },
  {
    // A mistyped support address (`/support/xyz`) gets the customer's own 404,
    // with no way into the console - the customer has no business there.
    path: "/support/*",
    element: <SupportNotFound />,
    errorElement: <RouteError />,
  },
  {
    // Everything else. A customer-neutral page, with one link into the console
    // for the operator who mistyped an address of their own. It must be the
    // LAST route: the console's catch-all is scoped to `/admin/*` precisely so
    // this one can own the rest of the namespace.
    path: "*",
    element: <SupportNotFound showConsole />,
    errorElement: <RouteError />,
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
