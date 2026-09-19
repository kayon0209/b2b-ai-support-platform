import { Component, type ErrorInfo, type ReactNode } from "react";
import { Link, useRouteError } from "react-router-dom";
import { useLangSafe } from "../lib/i18n";
import { PageHeader } from "./ui";

/**
 * Last line of defence for a render failure.
 *
 * Without this, any throw during render unmounts the whole tree: the
 * operator gets a blank page (or, in dev, React Router's bare error dump)
 * with no way back except a browser reload, and no idea what happened.
 * `useLang` throwing outside its provider was caught here during
 * acceptance testing — a one-line wiring bug took the entire console down.
 *
 * It is a class component because `componentDidCatch` is the only API React
 * exposes for this; the card it renders is a function component so it can
 * still read the language. `resetKeys` lets a caller recover by changing
 * route instead of forcing a reload.
 */
interface Props {
  children: ReactNode;
  /** Changing any of these clears the error (e.g. the current pathname). */
  resetKeys?: readonly unknown[];
}

interface State {
  error: Error | null;
}

function ErrorCard({ error, onRetry }: { error: Error; onRetry: () => void }) {
  const { t } = useLangSafe();
  return (
    <div className="page">
      <PageHeader title={t("error.title")} />
      <div className="banner banner-error" role="alert">
        <span>{t("error.body")}</span>
        <button className="btn" onClick={onRetry}>
          {t("error.retry")}
        </button>
      </div>
      <details className="error-details">
        <summary className="muted">{t("error.details")}</summary>
        <pre className="cell-code">{error.message}</pre>
      </details>
    </div>
  );
}

/**
 * `errorElement` for the route table.
 *
 * React Router renders this *instead of* the route that failed, so it must
 * not render that route again — a shell that threw on render would throw
 * again. It therefore shows only the failure and a way out, and uses
 * `useLangSafe` because the language provider may be what failed.
 */
export function RouteError() {
  const error = useRouteError();
  const { t } = useLangSafe();
  const message = error instanceof Error ? error.message : String(error);

  return (
    <div className="page">
      <PageHeader title={t("error.title")} />
      <div className="banner banner-error" role="alert">
        <span>{t("error.body")}</span>
      </div>
      <details className="error-details">
        <summary className="muted">{t("error.details")}</summary>
        <pre className="cell-code">{message}</pre>
      </details>
      <p>
        <Link className="btn" to="/quality">
          {t("notFound.home")}
        </Link>
      </p>
    </div>
  );
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // The console is the only place this is visible today; a real
    // deployment should forward it to the same sink the API uses.
    console.error("Unhandled render error:", error, info.componentStack);
  }

  componentDidUpdate(prev: Props): void {
    const keys = this.props.resetKeys ?? [];
    const prevKeys = prev.resetKeys ?? [];
    const changed =
      keys.length !== prevKeys.length || keys.some((k, i) => k !== prevKeys[i]);
    if (changed && this.state.error) this.setState({ error: null });
  }

  render(): ReactNode {
    const { error } = this.state;
    if (error) {
      return <ErrorCard error={error} onRetry={() => this.setState({ error: null })} />;
    }
    return this.props.children;
  }
}
