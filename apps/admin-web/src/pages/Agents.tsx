import { useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
import { useAction } from "../lib/useAction";
import { useAsync } from "../lib/useAsync";
import type { AgentPerformance, AgentProfile } from "../lib/types";
import {
  ActionFeedback,
  Badge,
  Card,
  EmptyState,
  ListTotal,
  PageHeader,
  Spinner,
} from "../components/ui";
import { LoadError } from "../components/LoadError";
import { useLang } from "../lib/i18n";

/**
 * The agent roster and how each person is doing.
 *
 * Two endpoints in one screen on purpose. The roster (`/v1/agents`) says who can
 * take work; the performance report (`/v1/quality/agents`) says how much each
 * one is carrying and how it is going. Separating them would produce two pages
 * that are only ever read together - and the question an operator actually asks
 * is "who is overloaded, and is the copilot helping them", which needs both.
 *
 * Three renderings worth stating:
 *
 * - **Every rate shows "—" when it has no denominator.** `first_time_fix_rate`,
 *   `ai_suggestion_adoption` and the percentiles are all `null` rather than 0
 *   for an agent with no measured cases, and printing 0% would turn "nothing to
 *   measure" into "failing".
 * - **Load is shown against capacity, not as a bare count.** "3 open" is
 *   unreadable without knowing whether the ceiling is 3 or 30.
 * - **A utilisation above 1.0 is flagged.** It should be impossible (the claim
 *   path refuses at capacity), so seeing it means a ceiling changed under an
 *   agent who was already loaded - worth noticing rather than smoothing over.
 */

interface RosterDraft {
  user_ref: string;
  display_name: string;
  skills: string;
  max_concurrent: string;
}

export function Agents() {
  const { t } = useLang();
  const action = useAction();
  const [draft, setDraft] = useState<RosterDraft>({
    user_ref: "",
    display_name: "",
    skills: "",
    max_concurrent: "5",
  });

  const roster = useAsync<{ items: AgentProfile[]; count: number }>(() =>
    apiGet("/v1/agents?status="),
  );
  const performance = useAsync<{ agents: AgentPerformance[] }>(() =>
    apiGet("/v1/quality/agents"),
  );

  const byRef = new Map((performance.data?.agents ?? []).map((a) => [a.user_ref, a]));

  const refValid = draft.user_ref.trim() !== "";
  const nameValid = draft.display_name.trim() !== "";
  const capacityValid = Number(draft.max_concurrent) >= 1;
  const canSave = refValid && nameValid && capacityValid && !action.busy;

  function save(): void {
    if (!canSave) return;
    void action
      .run(
        () =>
          apiPost(
            "/v1/agents",
            {
              user_ref: draft.user_ref.trim(),
              display_name: draft.display_name.trim(),
              skills: draft.skills
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean),
              max_concurrent: Number(draft.max_concurrent),
              status: "active",
            },
            newIdempotencyKey(),
          ).then(() => undefined),
        t("agents.saved", { name: draft.display_name.trim() }),
      )
      .then((ok) => {
        if (ok) {
          setDraft({ user_ref: "", display_name: "", skills: "", max_concurrent: "5" });
          roster.reload();
          performance.reload();
        }
      });
  }

  function toggleStatus(agent: AgentProfile): void {
    const next = agent.status === "active" ? "inactive" : "active";
    void action
      .run(
        () =>
          apiPost(
            `/v1/agents/${encodeURIComponent(agent.user_ref)}/status`,
            { status: next },
            newIdempotencyKey(),
          ).then(() => undefined),
        t("agents.statusChanged", { name: agent.display_name }),
      )
      .then((ok) => {
        if (ok) {
          roster.reload();
          performance.reload();
        }
      });
  }

  /** "—" for a rate with no denominator - never 0%. */
  function rate(value: number | null | undefined): string {
    return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
  }

  function minutes(value: number | null | undefined): string {
    return value === null || value === undefined ? "—" : `${value}m`;
  }

  const items = roster.data?.items ?? [];

  return (
    <div className="page">
      <PageHeader title={t("agents.title")} subtitle={t("agents.subtitle")} />

      <Card title={t("agents.addTitle")}>
        <div className="toolbar">
          <input
            className="text-input"
            value={draft.user_ref}
            onChange={(e) => setDraft({ ...draft, user_ref: e.target.value })}
            placeholder={t("agents.userRefPlaceholder")}
            aria-invalid={draft.user_ref !== "" && !refValid}
          />
          <input
            className="text-input"
            value={draft.display_name}
            onChange={(e) => setDraft({ ...draft, display_name: e.target.value })}
            placeholder={t("agents.namePlaceholder")}
          />
          <input
            className="text-input"
            value={draft.skills}
            onChange={(e) => setDraft({ ...draft, skills: e.target.value })}
            placeholder={t("agents.skillsPlaceholder")}
          />
          <input
            className="text-input text-input-narrow"
            value={draft.max_concurrent}
            inputMode="numeric"
            onChange={(e) => setDraft({ ...draft, max_concurrent: e.target.value })}
            placeholder={t("agents.capacityPlaceholder")}
            aria-invalid={!capacityValid}
          />
          <button className="btn btn-primary" disabled={!canSave} onClick={save}>
            {t("agents.add")}
          </button>
        </div>
        <p className="muted">{t("agents.skillsNote")}</p>
      </Card>

      <ActionFeedback error={action.error} notice={action.notice} />
      <LoadError error={roster.error} status={roster.errorStatus} onRetry={roster.reload} />
      {roster.loading ? <Spinner label={t("agents.loading")} /> : null}
      {roster.data && items.length === 0 ? <EmptyState message={t("agents.empty")} /> : null}

      {items.length > 0 ? (
        <Card title={t("agents.rosterTitle")}>
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">{t("agents.headerName")}</th>
                  <th scope="col">{t("agents.headerSkills")}</th>
                  <th scope="col" className="num">
                    {t("agents.headerLoad")}
                  </th>
                  <th scope="col" className="num">
                    {t("agents.headerFirstResponse")}
                  </th>
                  <th scope="col" className="num">
                    {t("agents.headerFirstTimeFix")}
                  </th>
                  <th scope="col" className="num">
                    {t("agents.headerReplies")}
                  </th>
                  <th scope="col" className="num">
                    {t("agents.headerAdoption")}
                  </th>
                  <th scope="col">{t("agents.headerState")}</th>
                  <th scope="col" />
                </tr>
              </thead>
              <tbody>
                {items.map((agent) => {
                  const stat = byRef.get(agent.user_ref);
                  const utilisation = stat?.utilisation ?? null;
                  return (
                    <tr key={agent.user_ref}>
                      <td className="cell-strong">
                        {agent.display_name}
                        <span className="muted"> · {agent.user_ref.slice(0, 8)}</span>
                      </td>
                      <td className="muted">
                        {agent.skills.length > 0 ? agent.skills.join(", ") : t("agents.anySkill")}
                      </td>
                      <td className="num">
                        {/* Count against capacity: "3 open" is unreadable without
                            knowing whether the ceiling is 3 or 30. */}
                        {stat ? `${stat.open_cases}/${agent.max_concurrent}` : "—"}
                        {utilisation !== null && utilisation > 1 ? (
                          <Badge tone="warn">{t("agents.overCapacity")}</Badge>
                        ) : null}
                      </td>
                      <td className="num">{minutes(stat?.first_response_minutes_p50)}</td>
                      <td className="num">{rate(stat?.first_time_fix_rate)}</td>
                      <td className="num">{stat?.replies_sent ?? 0}</td>
                      <td className="num">{rate(stat?.ai_suggestion_adoption)}</td>
                      <td>
                        <Badge tone={agent.status === "active" ? "good" : "neutral"}>
                          {agent.status === "active"
                            ? t("agents.active")
                            : t("agents.inactive")}
                        </Badge>
                      </td>
                      <td className="row-actions">
                        <button
                          className="btn"
                          disabled={action.busy}
                          onClick={() => toggleStatus(agent)}
                        >
                          {agent.status === "active" ? t("agents.deactivate") : t("agents.activate")}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      ) : null}

      {performance.data ? (
        <Card title={t("agents.teamTitle")}>
          <div className="toolbar">
            <span className="muted">
              {t("agents.teamAdoption", {
                value: rate(performance.data.agents.length ? undefined : undefined),
              })}
            </span>
          </div>
          <p className="muted">{t("agents.teamNote")}</p>
        </Card>
      ) : null}
      <ListTotal shown={items.length} total={roster.data?.count ?? 0} />
    </div>
  );
}
