/**
 * A URL that matches no route.
 *
 * Two audiences, one page, and the difference is a single link. A mistyped
 * *support* address (`/support/xyz`) is a customer's problem; anything else
 * (`/suport`, a stale bookmark) could be either, so `showConsole` adds the way
 * into the console without putting the console's chrome on the page.
 *
 * What this replaces: the route table's catch-all lived **inside** `Layout`, so
 * every unknown URL rendered the operator's 404 - a page headed "页面不存在"
 * with the full fourteen-item admin sidebar beside it and a recovery button
 * reading "返回质量看板". Measured 2026-09-23 and again after the `/admin`
 * migration, which is what finally scoped the catch-all to the prefix.
 *
 * It is its own component rather than a branch inside `NotFound` because the two
 * belong to different shells: `NotFound` renders inside `Layout` and needs the
 * operator's navigation to be useful, and this one must not have it at all.
 */

import { Link, useLocation, useSearchParams } from "react-router-dom";

import "../styles-support.css";

const COPY = {
  pageKind: "在线客服",
  title: "这个页面不存在",
  body: (path: string) => `没有找到「${path}」。可能是地址输错了，或者链接已经失效。`,
  start: "开始对话",
  home: "回到客服首页",
  console: "运营控制台",
} as const;

export function SupportNotFound({ showConsole = false }: { showConsole?: boolean }) {
  const { pathname } = useLocation();
  const [params] = useSearchParams();
  const tenant = params.get("tenant");
  const supportHref = tenant ? `/support?tenant=${encodeURIComponent(tenant)}` : "/support";

  return (
    <div className="support-shell support-landing">
      <header className="support-head">
        <div className="support-head-text">
          <h1 className="support-title">
            <span className="support-title-kind">{COPY.pageKind}</span>
          </h1>
        </div>
      </header>

      <main className="support-landing-main">
        <h2 className="support-landing-headline">{COPY.title}</h2>
        <p className="support-landing-body">{COPY.body(pathname)}</p>
        <Link className="support-landing-cta" to={supportHref}>
          {COPY.start}
        </Link>
      </main>

      <footer className="support-landing-foot">
        <Link className="support-landing-staff" to="/">
          {COPY.home} <span aria-hidden>→</span>
        </Link>
        {showConsole ? (
          <Link className="support-landing-staff" to="/admin/quality">
            {COPY.console} <span aria-hidden>→</span>
          </Link>
        ) : null}
      </footer>
    </div>
  );
}
