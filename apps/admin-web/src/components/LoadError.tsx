import { useLangSafe } from "../lib/i18n";
import { ErrorBanner } from "./ui";

/**
 * A failed read, split by whether the viewer can do anything about it.
 *
 * A 403 is not a failure of the page: the console is working and this
 * principal simply may not read that section. Rendering it as a red
 * "failed to load" banner sends the operator to support for a permission
 * question, which is exactly what the banner is for and not what this is.
 * Only billing used to make that distinction.
 */
export function LoadError({
  error,
  status,
  onRetry,
  scope,
}: {
  error: string | null;
  status: number | null;
  onRetry: () => void;
  /** What was being read, for a permission note that names it. */
  scope?: string;
}) {
  const { t } = useLangSafe();

  if (!error) return null;

  if (status === 403) {
    return <p className="muted">{t("error.forbidden", { scope: scope ?? "" })}</p>;
  }

  return <ErrorBanner message={error} onRetry={onRetry} />;
}
