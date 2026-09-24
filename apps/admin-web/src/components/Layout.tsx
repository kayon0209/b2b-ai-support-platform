import { useEffect, useState } from "react";
import {
  BarChart3, BookOpenText, ChevronLeft, Headset, Menu, Settings2,
  Ticket, UsersRound, X,
} from "lucide-react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { apiGet, getToken, setUnauthorizedHandler } from "../lib/api";
import { restoreOperatorSession } from "../lib/operatorAuth";
import { LangProvider, useLang } from "../lib/i18n";
import { useTheme } from "../lib/theme";
import { ErrorBoundary } from "./ErrorBoundary";
import { TokenDialog } from "./TokenDialog";

// All operator routes remain reachable in the expanded menu. The narrow rail
// reserves the primary workspace for the five jobs used during an active shift.
const NAV = [
  { to: "/admin/workbench", key: "nav.workbench" },
  { to: "/admin/customers", key: "nav.customers" },
  { to: "/admin/knowledge", key: "nav.knowledge" },
  { to: "/admin/cases", key: "nav.cases" },
  { to: "/admin/quality", key: "nav.quality" },
  { to: "/admin/gaps", key: "nav.gaps" },
  { to: "/admin/conversations", key: "nav.conversations" },
  { to: "/admin/approvals", key: "nav.approvals" },
  { to: "/admin/channels", key: "nav.channels" },
  { to: "/admin/experiments", key: "nav.experiments" },
  { to: "/admin/prompts", key: "nav.prompts" },
  { to: "/admin/flags", key: "nav.flags" },
  { to: "/admin/members", key: "nav.members" },
  { to: "/admin/usage", key: "nav.usage" },
  { to: "/admin/branding", key: "nav.branding" },
] as const;

const PRIMARY = [
  { to: "/admin/workbench", key: "nav.workbench", icon: Headset },
  { to: "/admin/customers", key: "nav.customers", icon: UsersRound },
  { to: "/admin/knowledge", key: "nav.knowledge", icon: BookOpenText },
  { to: "/admin/cases", key: "nav.cases", icon: Ticket },
  { to: "/admin/quality", key: "nav.quality", icon: BarChart3 },
] as const;

function Shell() {
  const { lang, setLang, t } = useLang();
  const { theme, toggle } = useTheme();
  const { pathname } = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const [tokenOpen, setTokenOpen] = useState(() => import.meta.env.DEV && !getToken());
  const [authReady, setAuthReady] = useState(import.meta.env.DEV);
  const [tenantSlug, setTenantSlug] = useState("");
  const isWorkbench = pathname.startsWith("/admin/workbench");

  useEffect(() => {
    setUnauthorizedHandler(() => setTokenOpen(true));
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    if (import.meta.env.DEV) return;
    let active = true;
    void restoreOperatorSession()
      .then((ok) => {
        if (!active) return;
        setAuthReady(true);
        setTokenOpen(!ok);
      })
      .catch(() => {
        if (!active) return;
        setAuthReady(true);
        setTokenOpen(true);
      });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    setMenuOpen(false);
    const section = NAV.filter(
      (item) => pathname === item.to || pathname.startsWith(`${item.to}/`),
    ).sort((a, b) => b.to.length - a.to.length)[0];
    document.title = section
      ? `${t(section.key)} · ${t("brand.name")}`
      : `${t("notFound.title")} · ${t("brand.name")}`;
  }, [pathname, t]);

  useEffect(() => {
    if (!authReady || !getToken()) return;
    let active = true;
    void apiGet<{ branding: { slug: string } }>("/v1/tenant/branding")
      .then((result) => { if (active) setTenantSlug(result.branding.slug); })
      .catch(() => { /* The token dialog and page loaders report auth errors. */ });
    return () => { active = false; };
  }, [authReady]);

  const customerHref = tenantSlug
    ? `/support?tenant=${encodeURIComponent(tenantSlug)}`
    : "/support";
  const moreLabel = lang === "zh" ? "更多页面" : "More pages";
  const settingsLabel = lang === "zh" ? "设置" : "Settings";

  return (
    <div className={`app-shell workspace-shell${isWorkbench ? " workspace-shell--workbench" : ""}`}>
      <a className="skip-link" href="#main">{t("common.skipToContent")}</a>
      <aside className="workspace-rail" aria-label={t("nav.ariaLabel")}>
        <NavLink className="workspace-rail-logo" to="/admin/workbench" aria-label={t("brand.name")}>
          <Headset size={27} strokeWidth={2.3} />
        </NavLink>
        <nav className="workspace-rail-primary" aria-label={t("nav.ariaLabel")}>
          {PRIMARY.map(({ to, key, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) => `workspace-rail-item${isActive ? " active" : ""}`}
              title={t(key)}
              aria-label={t(key)}
            >
              <Icon size={22} strokeWidth={1.8} aria-hidden="true" />
              <span>{t(key)}</span>
            </NavLink>
          ))}
        </nav>
        <div className="workspace-rail-bottom">
          <button
            className={`workspace-rail-item${menuOpen ? " active" : ""}`}
            type="button"
            title={moreLabel}
            aria-label={moreLabel}
            aria-expanded={menuOpen}
            aria-controls="workspace-more-menu"
            onClick={() => setMenuOpen((current) => !current)}
          >
            {menuOpen ? <X size={22} /> : <Menu size={22} />}
            <span>{moreLabel}</span>
          </button>
          <NavLink
            className={({ isActive }) => `workspace-rail-item${isActive ? " active" : ""}`}
            to="/admin/branding"
            title={settingsLabel}
            aria-label={settingsLabel}
          >
            <Settings2 size={22} strokeWidth={1.8} aria-hidden="true" />
            <span>{settingsLabel}</span>
          </NavLink>
        </div>
      </aside>
      {menuOpen ? (
        <div className="workspace-more" id="workspace-more-menu">
          <div className="workspace-more-head">
            <strong>{lang === "zh" ? "全部页面" : "All pages"}</strong>
            <button type="button" onClick={() => setMenuOpen(false)} aria-label={t("common.close")}>
              <ChevronLeft size={18} />
            </button>
          </div>
          <nav aria-label={moreLabel}>
            {NAV.map((item) => (
              <NavLink key={item.to} to={item.to} className={({ isActive }) => isActive ? "active" : ""}>
                {t(item.key)}
              </NavLink>
            ))}
            <a href={customerHref} target="_blank" rel="noreferrer">
              {t("nav.customerWindow")} ↗
            </a>
          </nav>
          <div className="workspace-more-controls">
            <button type="button" onClick={toggle}>
              {t(theme === "dark" ? "controls.themeLight" : "controls.themeDark")}
            </button>
            <button type="button" onClick={() => setLang(lang === "zh" ? "en" : "zh")}>
              {lang === "zh" ? "English" : "中文"}
            </button>
            <button type="button" onClick={() => setTokenOpen(true)}>{t("auth.manage")}</button>
          </div>
        </div>
      ) : null}
      <main className={`content workspace-content${isWorkbench ? " workspace-content--workbench" : ""}`} id="main">
        <ErrorBoundary resetKeys={[pathname]}>
          {authReady && getToken() ? <Outlet /> : <div className="auth-wait">
            {authReady ? "请通过企业身份验证进入控制台。" : "正在检查登录状态…"}
          </div>}
        </ErrorBoundary>
      </main>
      <TokenDialog open={tokenOpen} onClose={() => setTokenOpen(false)} />
    </div>
  );
}

export function Layout() {
  return <LangProvider><Shell /></LangProvider>;
}
