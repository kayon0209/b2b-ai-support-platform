/**
 * The customer's entry point (`/`).
 *
 * What this replaces
 * ------------------
 * `/` used to be `<Navigate to="/admin/quality" replace />`, so the root URL opened
 * the **operator console**: a quality dashboard whose first screen is "总运行数
 * / 弃答率 / 引用覆盖率", next to a control that asks for an operator bearer
 * token. Measured 2026-09-23. A customer who had only ever been given the
 * domain saw a screen they could not use and a credential they could not hold,
 * and the support window itself was reachable only through one item in the
 * operator's own sidebar.
 *
 * So the root is a customer page now, and the console is one link away for
 * staff. The alternative - moving the console under `/admin` and putting the
 * landing page at the root - is the tidier end state, but it changes every
 * console URL and every bookmark, and it is not what makes the customer's
 * first screen correct. That move can follow on its own.
 *
 * Why it takes a `tenant` parameter
 * ---------------------------------
 * A tenant-specific link (`/?tenant=acme`) has to carry through to the session
 * the customer is about to open, or the landing page would send them to the
 * default tenant's window - the same class of bug as the cached session that
 * ignored `?tenant`. It is read here and passed on unchanged, never defaulted
 * to a tenant this page guessed.
 */

import { Link, useSearchParams } from "react-router-dom";

import "../styles-support.css";

const COPY = {
  pageKind: "在线客服",
  headline: "有问题，随时问。",
  body: "订单进度、交期、发票、技术参数都可以问。回答会附上引用的来源；想找人工同事，随时点「转人工」。",
  start: "开始对话",
  verifyHint: "要查订单或物流，进对话后先核实一下身份即可。",
  staff: "我是客服人员",
  staffHint: "进入运营控制台",
} as const;

export function Landing() {
  const [params] = useSearchParams();
  // Passed through verbatim. Absent means the API's own default, not a guess
  // made here.
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
        <h2 className="support-landing-headline">{COPY.headline}</h2>
        <p className="support-landing-body">{COPY.body}</p>

        <Link className="support-landing-cta" to={supportHref}>
          {COPY.start}
        </Link>
        <p className="support-landing-note">{COPY.verifyHint}</p>
      </main>

      {/*
        Deliberately a link, not a form and not a token field. The console
        authenticates with a bearer token it asks for itself; putting that
        prompt on the customer's landing page is what this page exists to stop.
      */}
      <footer className="support-landing-foot">
        <Link className="support-landing-staff" to="/admin/quality">
          {COPY.staff} <span aria-hidden>→</span>
        </Link>
        <span className="support-landing-staff-hint">{COPY.staffHint}</span>
      </footer>
    </div>
  );
}
