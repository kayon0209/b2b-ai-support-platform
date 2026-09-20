import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { getToken, setUnauthorizedHandler } from "../lib/api";
import { LangProvider, useLang } from "../lib/i18n";
import { useTheme } from "../lib/theme";
import { ErrorBoundary } from "./ErrorBoundary";
import { TokenDialog } from "./TokenDialog";

const NAV = [
  { to: "/quality", key: "nav.quality", icon: "📊" },
  { to: "/gaps", key: "nav.gaps", icon: "🧩" },
  { to: "/prompts", key: "nav.prompts", icon: "✍️" },
  { to: "/flags", key: "nav.flags", icon: "🚩" },
  { to: "/cases", key: "nav.cases", icon: "🎫" },
  { to: "/approvals", key: "nav.approvals", icon: "✅" },
  { to: "/members", key: "nav.members", icon: "👥" },
  { to: "/usage", key: "nav.usage", icon: "📈" },
  { to: "/branding", key: "nav.branding", icon: "🎨" },
] as const;

function Shell() {
  const { lang, setLang, t } = useLang();
  const { theme, toggle } = useTheme();
  const { pathname } = useLocation();
  // Open on first visit when no token is configured; the unauthorized
  // handler reopens it whenever any API call comes back 401.
  const [tokenOpen, setTokenOpen] = useState(() => !getToken());
  // On a narrow screen the sidebar becomes a block above the content; left
  // expanded it pushes everything below the fold, so it starts collapsed.
  const [navOpen, setNavOpen] = useState(false);

  useEffect(() => {
    setUnauthorizedHandler(() => setTokenOpen(true));
    return () => setUnauthorizedHandler(null);
  }, []);

  // Collapse again after navigating, so tapping a section reveals the page
  // the operator just chose rather than leaving the menu covering it.
  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  // Every page used to share one title, so browser history, tabs and the
  // back-button menu all read "B2B AI Support · Admin" and told you nothing
  // about which of eight screens you were looking at.
  useEffect(() => {
    const section = NAV.find((item) => item.to === pathname);
    document.title = section
      ? `${t(section.key)} · ${t("brand.name")}`
      : `${t("notFound.title")} · ${t("brand.name")}`;
  }, [pathname, t]);

  return (
    <div className="app-shell">
      {/* Keyboard and screen-reader users would otherwise tab through all
          eight nav items on every page before reaching the content. */}
      <a className="skip-link" href="#main">
        {t("common.skipToContent")}
      </a>
      <aside className={`sidebar${navOpen ? " nav-open" : ""}`}>
        <div className="brand">
          <div className="brand-mark">AI</div>
          <div>
            <div className="brand-name">{t("brand.name")}</div>
            <div className="brand-sub">{t("brand.sub")}</div>
          </div>
        </div>
        {/* Only rendered (via CSS) once the shell collapses; on a wide
            screen the nav and the toggles are always visible. */}
        <button
          className="nav-toggle"
          onClick={() => setNavOpen((open) => !open)}
          aria-expanded={navOpen}
          aria-controls="primary-nav"
        >
          <span>{t("nav.ariaLabel")}</span>
          <span aria-hidden>{navOpen ? "▲" : "▼"}</span>
        </button>

        {/* The two shell toggles sit with the brand: always visible, and the
            conventional place operators look for a mode switch. */}
        <div className="shell-controls">
          <button
            className="shell-toggle"
            onClick={toggle}
            aria-label={t("controls.theme")}
            title={t("controls.theme")}
          >
            <span aria-hidden>{theme === "dark" ? "☀️" : "🌙"}</span>
            {/* Names the mode the click switches *to*, and is translated —
                it used to be a hardcoded English literal. */}
            <span>{t(theme === "dark" ? "controls.themeLight" : "controls.themeDark")}</span>
          </button>
          <button
            className="shell-toggle"
            onClick={() => setLang(lang === "zh" ? "en" : "zh")}
            aria-label={t("controls.lang")}
            title={t("controls.lang")}
          >
            <span aria-hidden>🌐</span>
            <span>{lang === "zh" ? "EN" : "中文"}</span>
          </button>
        </div>
        <nav className="nav" id="primary-nav" aria-label={t("nav.ariaLabel")}>
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
            >
              <span className="nav-icon" aria-hidden>
                {item.icon}
              </span>
              <span>{t(item.key)}</span>
            </NavLink>
          ))}
        </nav>
        <footer className="sidebar-footer muted">
          <span>{t("footer.controlPlane")}</span>
          <button className="btn btn-ghost" onClick={() => setTokenOpen(true)}>
            {t("footer.token")}
          </button>
        </footer>
      </aside>
      <main className="content" id="main">
        {/* A page that throws takes down only this subtree: the shell stays
            on screen, so the operator can navigate to another section
            instead of being stranded on a blank page. Changing route clears
            the error. */}
        <ErrorBoundary resetKeys={[pathname]}>
          <Outlet />
        </ErrorBoundary>
      </main>
      <TokenDialog open={tokenOpen} onClose={() => setTokenOpen(false)} />
    </div>
  );
}

export function Layout() {
  // The provider wraps the shell AND the outlet, so every page and the token
  // dialog read the same language state.
  return (
    <LangProvider>
      <Shell />
    </LangProvider>
  );
}
