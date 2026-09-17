import { NavLink, Outlet } from "react-router-dom";

const NAV = [
  { to: "/quality", label: "Quality", icon: "📊" },
  { to: "/gaps", label: "Knowledge Gaps", icon: "🧩" },
  { to: "/prompts", label: "Prompt Release", icon: "✍️" },
  { to: "/flags", label: "Feature Flags", icon: "🚩" },
  { to: "/cases", label: "Cases & SLA", icon: "🎫" },
  { to: "/members", label: "Members", icon: "👥" },
  { to: "/branding", label: "Branding", icon: "🎨" },
];

export function Layout() {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">AI</div>
          <div>
            <div className="brand-name">B2B Support</div>
            <div className="brand-sub">Enterprise Admin</div>
          </div>
        </div>
        <nav className="nav">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
            >
              <span className="nav-icon" aria-hidden>
                {item.icon}
              </span>
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <footer className="sidebar-footer muted">
          Control plane · v1
        </footer>
      </aside>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
