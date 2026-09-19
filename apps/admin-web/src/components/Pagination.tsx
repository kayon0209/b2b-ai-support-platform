import { useLang } from "../lib/i18n";

/**
 * Offset paging for a list the API caps with `limit`.
 *
 * Every list in this console used to request `limit=100` and render whatever
 * came back, with no way to reach the rest — fine on a demo tenant, useless
 * on a real one, and silent about it because several endpoints report
 * `total` as the length of the page they just returned. Without a control
 * here the operator has no way to tell "these are all of them" from "these
 * are the first hundred".
 */
export function Pagination({
  offset,
  limit,
  total,
  busy,
  onChange,
}: {
  offset: number;
  limit: number;
  total: number;
  busy?: boolean;
  onChange: (nextOffset: number) => void;
}) {
  const { t } = useLang();
  if (total <= 0) return null;

  const page = Math.floor(offset / limit) + 1;
  const from = offset + 1;
  const to = Math.min(offset + limit, total);
  const hasPrev = offset > 0;
  const hasNext = offset + limit < total;

  // Nothing to page through: showing a disabled pair of buttons on every
  // short list is noise, and hides the fact that paging exists at all.
  if (!hasPrev && !hasNext) return null;

  return (
    <div className="pagination">
      <span className="muted">{t("common.showingRange", { from, to, total })}</span>
      <div className="row">
        <button
          className="btn"
          disabled={!hasPrev || busy}
          onClick={() => onChange(Math.max(0, offset - limit))}
        >
          {t("common.previous")}
        </button>
        <span className="muted">{t("common.pageOf", { page })}</span>
        <button
          className="btn"
          disabled={!hasNext || busy}
          onClick={() => onChange(offset + limit)}
        >
          {t("common.next")}
        </button>
      </div>
    </div>
  );
}
