import { lazy, StrictMode, Suspense, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import {
  createBrowserRouter,
  Navigate,
  RouterProvider,
  useLocation,
} from "react-router-dom";

import { RouteError } from "./components/ErrorBoundary";
import { Layout } from "./components/Layout";
import "./styles.css";

const Approvals = lazy(() => import("./pages/Approvals").then((page) => ({ default: page.Approvals })));
const AuthCallback = lazy(() => import("./pages/AuthCallback").then((page) => ({ default: page.AuthCallback })));
const Branding = lazy(() => import("./pages/Branding").then((page) => ({ default: page.Branding })));
const Cases = lazy(() => import("./pages/Cases").then((page) => ({ default: page.Cases })));
const Customers = lazy(() => import("./pages/Customers").then((page) => ({ default: page.Customers })));
const Channels = lazy(() => import("./pages/Channels").then((page) => ({ default: page.Channels })));
const Experiments = lazy(() => import("./pages/Experiments").then((page) => ({ default: page.Experiments })));
const FeatureFlags = lazy(() => import("./pages/FeatureFlags").then((page) => ({ default: page.FeatureFlags })));
const GapQueue = lazy(() => import("./pages/GapQueue").then((page) => ({ default: page.GapQueue })));
const Conversations = lazy(() => import("./pages/Conversations").then((page) => ({ default: page.Conversations })));
const Knowledge = lazy(() => import("./pages/Knowledge").then((page) => ({ default: page.Knowledge })));
const Landing = lazy(() => import("./pages/Landing").then((page) => ({ default: page.Landing })));
const Members = lazy(() => import("./pages/Members").then((page) => ({ default: page.Members })));
const NotFound = lazy(() => import("./pages/NotFound").then((page) => ({ default: page.NotFound })));
const PromptRelease = lazy(() => import("./pages/PromptRelease").then((page) => ({ default: page.PromptRelease })));
const QualityDashboard = lazy(() => import("./pages/QualityDashboard").then((page) => ({ default: page.QualityDashboard })));
const SupportChat = lazy(() => import("./pages/SupportChat").then((page) => ({ default: page.SupportChat })));
const SupportNotFound = lazy(() => import("./pages/SupportNotFound").then((page) => ({ default: page.SupportNotFound })));
const Usage = lazy(() => import("./pages/Usage").then((page) => ({ default: page.Usage })));
const Workbench = lazy(() => import("./pages/Workbench").then((page) => ({ default: page.Workbench })));

function withPageSuspense(element: ReactNode) {
  return (
    <Suspense fallback={<div className="route-loading" role="status">页面加载中…</div>}>
      {element}
    </Suspense>
  );
}

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
 * The retired internal `/chat` URL returns to the customer landing page.
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
  { path: "customers", element: <Customers /> },
  { path: "workbench", element: <Workbench /> },
  { path: "workbench/conversation/:conversationRef", element: <Workbench /> },
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
      ...OPERATOR_PAGES.map((page) => ({
        ...page,
        element: withPageSuspense(page.element),
        path: `admin/${page.path}`,
      })),
      // Anything else **under `/admin`**. Scoped to the prefix on purpose: an
      // unknown console URL keeps the sidebar and can navigate away, while an
      // unknown URL anywhere else is not the operator's problem. A bare `*`
      // here caught the whole site, which is how a customer who mistyped
      // `/support` as `/suport` ended up looking at the fourteen-item admin
      // sidebar. Caught by `admin_render_check.cjs`'s `expectAbsent` case.
      { path: "admin/*", element: withPageSuspense(<NotFound />) },
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
    path: "/auth/callback",
    element: withPageSuspense(<AuthCallback />),
    errorElement: <RouteError />,
  },
  {
    // The customer's entry point. Top-level, so it never inherits the sidebar.
    path: "/",
    element: withPageSuspense(<Landing />),
    errorElement: <RouteError />,
  },
  { path: "/chat", element: <Navigate to="/" replace /> },
  {
    // The real customer surface (ADR 0011): no operator token, no operator
    // layout. It opens a visitor session instead of using operator auth.
    path: "/support",
    element: withPageSuspense(<SupportChat />),
    errorElement: <RouteError />,
  },
  {
    // A mistyped support address (`/support/xyz`) gets the customer's own 404,
    // with no way into the console - the customer has no business there.
    path: "/support/*",
    element: withPageSuspense(<SupportNotFound />),
    errorElement: <RouteError />,
  },
  {
    // Everything else. A customer-neutral page, with one link into the console
    // for the operator who mistyped an address of their own. It must be the
    // LAST route: the console's catch-all is scoped to `/admin/*` precisely so
    // this one can own the rest of the namespace.
    path: "*",
    element: withPageSuspense(<SupportNotFound showConsole />),
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
