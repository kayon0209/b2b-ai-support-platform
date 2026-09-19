import type { ReactNode } from "react";
import { useLang } from "../lib/i18n";

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="spinner" role="status" aria-live="polite">
      <span className="spinner-dot" />
      {label ? <span className="spinner-label">{label}</span> : null}
    </div>
  );
}

export function ErrorBanner({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  const { t } = useLang();
  return (
    <div className="banner banner-error" role="alert">
      <span>{message}</span>
      {onRetry ? (
        <button className="btn btn-ghost" onClick={onRetry}>
          {t("common.retry")}
        </button>
      ) : null}
    </div>
  );
}

export function EmptyState({ message }: { message: string }) {
  return <div className="empty-state">{message}</div>;
}

/**
 * The result of a write action: a failure banner or a success banner, never
 * both. Kept in one component so every page reports an action the same way -
 * the alternative was four pages each growing their own variant.
 *
 * Both branches are `role="alert"`/`role="status"` so a screen reader
 * announces the outcome of a button press that produced no visible change.
 */
export function ActionFeedback({
  error,
  notice,
  onRetry,
}: {
  error: string | null;
  notice?: string | null;
  onRetry?: () => void;
}) {
  const { t } = useLang();
  if (error) {
    return (
      <div className="banner banner-error" role="alert">
        <span>{error}</span>
        {onRetry ? (
          <button className="btn btn-ghost" onClick={onRetry}>
            {t("common.retry")}
          </button>
        ) : null}
      </div>
    );
  }
  if (notice) {
    return (
      <div className="banner banner-ok" role="status">
        <span>{notice}</span>
      </div>
    );
  }
  return null;
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle ? <p className="muted">{subtitle}</p> : null}
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  );
}

export function Card({
  title,
  children,
  className,
}: {
  title?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card${className ? ` ${className}` : ""}`}>
      {title ? <h2 className="card-title">{title}</h2> : null}
      {children}
    </section>
  );
}

/**
 * Footer under a list capped by the API's limit, so truncation is visible
 * instead of silent: the envelope's `total` was fetched and then thrown
 * away, and a queue that holds more than the page shows reads as empty.
 */
export function ListTotal({ shown, total }: { shown: number; total: number }) {
  const { t } = useLang();
  if (total <= 0) return null;
  return (
    <p className="muted">
      {shown < total
        ? t("common.showing", { shown, total })
        : t("common.total", { total })}
    </p>
  );
}

export type BadgeTone = "neutral" | "good" | "warn" | "bad" | "info";

export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: BadgeTone;
}) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: ReactNode;
  tone?: BadgeTone;
}) {
  return (
    <div className="stat">
      <div className={`stat-value${tone ? ` text-${tone}` : ""}`}>{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}
