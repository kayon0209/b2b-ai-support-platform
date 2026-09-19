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
        <Link className="btn btn-primary" to="/quality">
          {t("notFound.home")}
        </Link>
      </div>
    </div>
  );
}
