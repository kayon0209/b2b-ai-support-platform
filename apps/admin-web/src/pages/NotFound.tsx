import { Link, useLocation } from "react-router-dom";
import { useLangSafe } from "../lib/i18n";
import { PageHeader } from "../components/ui";

/**
 * Shown for any URL that matches no route.
 *
 * The route table used to end with a catch-all that redirected to the
 * dashboard, so a mistyped URL silently landed somewhere plausible and a
 * bookmark to a removed page appeared to still work. Redirecting is right
 * for `/` but wrong for `/setings`: the operator is told nothing, and a
 * 404 that never surfaces is a dead link nobody will ever find.
 */
export function NotFound() {
  const { t } = useLangSafe();
  const { pathname } = useLocation();

  return (
    <div className="page">
      <PageHeader title={t("notFound.title")} />
      <div className="empty-state">
        <p>{t("notFound.body", { path: pathname })}</p>
        <Link className="btn btn-primary" to="/admin/quality">
          {t("notFound.home")}
        </Link>
        {/*
          A second way out, for the person this page was not built for.
          `/support/*` has its own customer-facing 404, so a mistyped *support*
          address no longer lands here - but a customer can still arrive from a
          stale bookmark to a removed console page, and "返回质量看板" is not an
          instruction they can follow. This is the only link on the page that
          helps them.
        */}
        <p>
          <Link to="/support">{t("notFound.customerWindow")}</Link>
        </p>
      </div>
    </div>
  );
}
