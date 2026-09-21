import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { apiGet } from "../lib/api";
import { useAsync } from "../lib/useAsync";
import { Badge, Card, EmptyState, ErrorBanner, PageHeader, Spinner } from "../components/ui";
import { LoadError } from "../components/LoadError";
import { useLang } from "../lib/i18n";

/**
 * Agent workbench (feature list 7.6): everything needed to answer without
 * re-asking. The bundle comes from one endpoint so the panel cannot disagree
 * with itself - case, conversation, the AI's last proposal with its sources,
 * and same-category cases are all read in one request.
 */

interface CaseSummary {
  case_id: string;
  subject: string;
  status: string;
  category?: string | null;
}

interface Turn {
  role: string;
  text: string;
}

interface Suggestion {
  text: string;
  sources: string[];
}

interface Workbench {
  case: CaseSummary;
  account_tier: string | null;
  /** Every contact bound to this account (2.1): one company, one contact per
   * channel. Without this the same customer looks like several strangers. */
  account_contacts: Array<{ external_contact_id: string; channel: string | null }>;
  conversation: Turn[];
  ai_suggestion: Suggestion | null;
  related_cases: { basis: string; items: CaseSummary[] };
}

const ROLE_TONE: Record<string, "good" | "warn" | "neutral" | "bad"> = {
  customer: "neutral",
  agent: "good",
  tool: "warn",
  system: "warn",
};

export function Workbench() {
  const { t } = useLang();
  const { caseId } = useParams();
  const navigate = useNavigate();
  // The URL is the source of truth for which case is open: that is what makes
  // a refresh keep the agent's place and a link shareable. Component state
  // alone lost both.
  const [selected, setSelected] = useState<string | null>(caseId ?? null);

  useEffect(() => {
    setSelected(caseId ?? null);
  }, [caseId]);

  function selectCase(id: string) {
    setSelected(id);
    // `replace` on the first pick would drop the entry an agent arrived from;
    // each deliberate selection is a step they may want to undo.
    navigate(`/workbench/${id}`);
  }

  const cases = useAsync(() => apiGet<{ items: CaseSummary[]; total: number }>("/v1/cases"), []);
  const bundle = useAsync(
    () =>
      selected === null
        ? Promise.resolve(null)
        : apiGet<Workbench>(`/v1/cases/${selected}/workbench`),
    [selected],
  );

  // The endpoint answers {items, total}, like every other list route, so
  // reading a field named "cases" yielded undefined and the queue rendered
  // "no cases" no matter how many existed. The generic on apiGet is a cast,
  // so TypeScript could not catch a name that was never on the wire - only
  // opening the page could, which is how this survived a passing test suite.
  const list = cases.data?.items ?? [];
  useEffect(() => {
    // Preselect the first case so the panel is never empty on arrival -
    // an agent opening this page wants to work, not to pick a filter first.
    if (selected === null && list.length > 0) selectCase(list[0].case_id);
  }, [list, selected]);

  return (
    <div>
      <PageHeader title={t("workbench.title")} subtitle={t("workbench.subtitle")} />

      {cases.error ? <LoadError error={cases.error} status={cases.errorStatus} onRetry={cases.reload} /> : null}

      <div className="workbench">
        <Card title={t("workbench.queue")}>
          {cases.loading ? <Spinner /> : null}
          {!cases.loading && list.length === 0 ? (
            <EmptyState message={t("workbench.noCases")} />
          ) : null}
          <ul className="workbench-list">
            {list.map((item) => (
              <li key={item.case_id}>
                <button
                  type="button"
                  className={item.case_id === selected ? "workbench-item selected" : "workbench-item"}
                  onClick={() => selectCase(item.case_id)}
                >
                  <span className="workbench-subject">{item.subject}</span>
                  <Badge tone="neutral">{item.status}</Badge>
                </button>
              </li>
            ))}
          </ul>
        </Card>

        <div className="workbench-detail">
          {bundle.error ? <LoadError error={bundle.error} status={bundle.errorStatus} onRetry={bundle.reload} /> : null}
          {bundle.loading ? <Spinner /> : null}
          {!bundle.loading && bundle.data === null ? (
            <EmptyState message={t("workbench.empty")} />
          ) : null}

          {bundle.data ? (
            <>
              <Card title={t("workbench.customer")}>
                <p>
                  <strong>{bundle.data.case.subject}</strong>
                </p>
                <p className="muted">
                  {t("workbench.category")}: {bundle.data.case.category ?? "—"}
                </p>
                {bundle.data.account_tier ? (
                  <Badge tone="warn">{bundle.data.account_tier}</Badge>
                ) : null}
                {bundle.data.account_contacts.length > 0 ? (
                  <div className="workbench-contacts">
                    <span className="muted">{t("workbench.contacts")}:</span>
                    {bundle.data.account_contacts.map((c) => (
                      <Badge key={c.external_contact_id} tone="neutral">
                        {c.channel ? `${c.channel}: ` : ""}
                        {c.external_contact_id}
                      </Badge>
                    ))}
                  </div>
                ) : null}
              </Card>

              <Card title={t("workbench.conversation")}>
                {bundle.data.conversation.length === 0 ? (
                  <EmptyState message={t("workbench.noTurns")} />
                ) : (
                  <ol className="timeline">
                    {bundle.data.conversation.map((turn, index) => (
                      <li key={index}>
                        <Badge tone={ROLE_TONE[turn.role] ?? "neutral"}>{turn.role}</Badge>
                        <span>{turn.text}</span>
                      </li>
                    ))}
                  </ol>
                )}
              </Card>

              <Card title={t("workbench.suggestion")}>
                {bundle.data.ai_suggestion === null ? (
                  <EmptyState message={t("workbench.noSuggestion")} />
                ) : (
                  <>
                    <p>{bundle.data.ai_suggestion.text}</p>
                    {bundle.data.ai_suggestion.sources.length > 0 ? (
                      <ul className="sources">
                        {bundle.data.ai_suggestion.sources.map((uri) => (
                          <li key={uri}>{uri}</li>
                        ))}
                      </ul>
                    ) : (
                      <p className="muted">{t("workbench.noSources")}</p>
                    )}
                  </>
                )}
              </Card>

              <Card
                title={`${t("workbench.related")} (${bundle.data.related_cases.basis})`}
              >
                {bundle.data.related_cases.items.length === 0 ? (
                  <EmptyState message={t("workbench.noRelated")} />
                ) : (
                  <ul>
                    {bundle.data.related_cases.items.map((item) => (
                      <li key={item.case_id}>
                        {item.subject} <Badge tone="neutral">{item.status}</Badge>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>
            </>
          ) : null}
        </div>
      </div>

      {cases.error ? <ErrorBanner message={String(cases.error)} /> : null}
    </div>
  );
}
