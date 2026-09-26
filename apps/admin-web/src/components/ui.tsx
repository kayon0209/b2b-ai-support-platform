import { useEffect, useRef, type ReactNode } from "react";
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

/**
 * A modal that is actually modal.
 *
 * What was wrong before this existed
 * ----------------------------------
 * The one real dialog in the console declared `aria-modal="false"` on a
 * dialog that covered the page, had no Escape handler, and did not move focus
 * anywhere. A screen-reader user was told "this is a dialog" and then left free
 * to tab into the page behind it, where every control still looked live. That
 * is not a cosmetic gap: the controls behind the dialog are the ones a
 * keyboard user believes they are operating.
 *
 * What this closes, in the order it matters
 * -----------------------------------------
 * 1. `aria-modal="true"` - assistive technology treats the rest of the page as
 *    inert, which is the only way "modal" becomes true rather than asserted.
 * 2. Focus moves in on open, and is **restored to the opener** on close.
 *    Without the restore, closing a dialog drops the user at the top of the
 *    document and they lose their place entirely.
 * 3. Tab cycles inside. Trapping focus is the part most hand-rolled dialogs
 *    skip, and it is the part that makes the modal actually modal for a
 *    keyboard.
 * 4. Escape closes. Expected everywhere, and it is the only way out for a user
 *    who cannot see a close button.
 * 5. Background scroll is locked, so a trackpad wheel does not scroll the page
 *    behind while the dialog is open.
 *
 * `onClose` is called with no arguments, and the opener is captured from
 * `document.activeElement` at open time rather than passed in - a prop for it
 * would be wrong whenever the opener is a row button that re-renders.
 */
export function Dialog({
  open,
  onClose,
  label,
  children,
  className,
}: {
  open: boolean;
  onClose: () => void;
  /** Accessible name. Prefer wording a user would recognise, not "Dialog". */
  label: string;
  children: ReactNode;
  className?: string;
}) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const openerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    openerRef.current = (document.activeElement as HTMLElement | null) ?? null;
    // Focus the first control, or the panel itself if it has none, so the
    // next Tab lands inside rather than behind.
    const focusables = () =>
      Array.from(
        panelRef.current?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      );
    const first = focusables()[0];
    (first ?? panelRef.current)?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusables();
      if (items.length === 0) {
        event.preventDefault();
        panelRef.current?.focus();
        return;
      }
      const firstItem = items[0];
      const lastItem = items[items.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === firstItem || active === panelRef.current)) {
        event.preventDefault();
        lastItem.focus();
      } else if (!event.shiftKey && active === lastItem) {
        event.preventDefault();
        firstItem.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown, true);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      document.body.style.overflow = previousOverflow;
      // Restoring focus is what makes the dialog a round trip. Without it the
      // user is dropped at the top of the page with no idea where they were.
      openerRef.current?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div
        ref={panelRef}
        className={className ? `dialog ${className}` : "dialog"}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}

/**
 * Placeholder rows for a list that is still loading.
 *
 * Why this and not the spinner that was already there
 * ---------------------------------------------------
 * A centred spinner replaces the whole page and then replaces it again with
 * content, so every load is two layout jumps and the operator loses their
 * place. A skeleton holds the shape of what is coming, which means the content
 * arrives into space that already exists.
 *
 * `aria-hidden` because it carries no information: it is a picture of content
 * that does not exist yet, and a screen reader announcing placeholder rows
 * would be noise. The live region beside it (`<LoadingRegion>`) is what
 * actually says "loading".
 */
export function SkeletonRows({ rows = 5, label }: { rows?: number; label?: string }) {
  return (
    <div className="skeleton-wrap">
      {label ? (
        <span role="status" aria-live="polite" className="skeleton-label">
          {label}
        </span>
      ) : null}
      <div className="skeleton-rows" aria-hidden="true">
        {Array.from({ length: Math.max(1, rows) }, (_, index) => (
          <div className="skeleton-row" key={index}>
            <span className="skeleton-bar skeleton-bar--title" />
            <span className="skeleton-bar" />
          </div>
        ))}
      </div>
    </div>
  );
}
