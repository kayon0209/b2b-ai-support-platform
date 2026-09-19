import { useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
import { useAction } from "../lib/useAction";
import { usePrompt } from "../components/Prompt";
import { useAsync } from "../lib/useAsync";
import type { ActivePrompt, PromptVersion } from "../lib/types";
import {
  ActionFeedback,
  Badge,
  Card,
  EmptyState,
  PageHeader,
  Spinner,
  ListTotal,
} from "../components/ui";
import { LoadError } from "../components/LoadError";
import { useLang, type DictKey } from "../lib/i18n";
import { useDebounced } from "../lib/useDebounced";

export function PromptRelease() {
  const { t } = useLang();
  const [template, setTemplate] = useState("agent_qa");
  const [draft, setDraft] = useState("");
  // The version whose body is shown in full. Truncation is for scanning the
  // list; a version under review has to be readable somewhere.
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const action = useAction();
  const prompt = usePrompt();

  // Both reads key on the template name, so every keystroke used to fire two
  // requests and an empty field fired a 422 for a name the operator was
  // still typing. Only the settled value is queried.
  const queryTemplate = useDebounced(template.trim(), 250);

  const list = useAsync<{ items: PromptVersion[]; total: number }>(
    () =>
      queryTemplate
        ? apiGet<{ items: PromptVersion[]; total: number }>(
            `/v1/prompts?template_name=${encodeURIComponent(queryTemplate)}`,
          )
        : Promise.resolve({ items: [], total: 0 }),
    [queryTemplate],
  );
  const active = useAsync<ActivePrompt>(
    () =>
      queryTemplate
        ? apiGet<ActivePrompt>(
            `/v1/prompts/active?template_name=${encodeURIComponent(queryTemplate)}`,
          )
        : Promise.resolve({ active: null, template_name: queryTemplate }),
    [queryTemplate],
  );

  async function act(path: string, body?: unknown, okMsg?: string) {
    const ok = await action.run(
      () => apiPost(path, body, newIdempotencyKey()).then(() => undefined),
      okMsg,
    );
    if (ok) {
      list.reload();
      active.reload();
    }
  }

  const sorted = list.data
    ? [...list.data.items].sort((a, b) => b.version - a.version)
    : [];
  const activeId = active.data?.active?.id ?? null;
  // Promote, Reject and Rollback all act *relative to* the active version.
  // When that read has failed or has not resolved, offering them means
  // offering to change production without knowing what is serving now.
  const activeUnknown = active.error !== null || (active.data === null && active.loading);
  // Rollback targets come from the list already on screen, so the operator
  // never has to copy a version id from somewhere else.
  const rollbackable = sorted.filter((v) => v.id !== activeId);
  const rollbackLabel = (v: PromptVersion) => `v${v.version}`;
  const rollbackIds = new Map(rollbackable.map((v) => [rollbackLabel(v), v.id]));

  return (
    <div className="page">
      <PageHeader
        title={t("prompts.title")}
        subtitle={t("prompts.subtitle")}
        actions={
          <div className="toolbar">
            <input
              className="text-input"
              value={template}
              onChange={(e) => setTemplate(e.target.value)}
              placeholder={t("prompts.templatePlaceholder")}
              list="prompt-templates"
            />
            <datalist id="prompt-templates">
              <option value="agent_qa" />
              <option value="summarise_case" />
              <option value="handoff_summary" />
            </datalist>
          </div>
        }
      />

      {/* If the active-version read fails and says nothing, every version
          below looks promotable — including the one already serving, which
          is a production change offered on the strength of a silence. */}
      <LoadError error={active.error} status={active.errorStatus} onRetry={active.reload} />
      {active.data?.active ? (
        <Card title={t("prompts.currentlyServing")}>
          <div className="serving">
            <Badge tone="good">v{active.data.active.version}</Badge>
            <code className="serving-body">{active.data.active.body.slice(0, 240)}</code>
          </div>
        </Card>
      ) : null}

      <Card title={t("prompts.newDraft")}>
        <textarea
          className="text-area"
          rows={4}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={t("prompts.bodyPlaceholder")}
        />
        <div className="toolbar">
          <button
            className="btn btn-primary"
            disabled={!draft.trim() || !template.trim()}
            onClick={() => {
              if (!draft.trim() || !template.trim()) return;
              act(
                "/v1/prompts",
                { template_name: template.trim(), body: draft, notes: "" },
                t("prompts.draftCreated"),
              ).then(() => setDraft(""));
            }}
          >
            {t("prompts.createDraft")}
          </button>
        </div>
      </Card>

      <ActionFeedback error={action.error} notice={action.notice} />
      {prompt.element}
      <LoadError error={list.error} status={list.errorStatus} onRetry={list.reload} />
      {list.loading ? <Spinner label={t("prompts.loading" as DictKey)} /> : null}
      {list.data && sorted.length === 0 ? (
        <EmptyState message={t("prompts.empty")} />
      ) : null}

      {sorted.length > 0 ? (
        <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">{t("prompts.headerVersion")}</th>
              <th scope="col">{t("prompts.headerState")}</th>
              <th scope="col">{t("prompts.headerBody")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {sorted.map((v) => (
              <tr key={v.id}>
                <td className="cell-strong">v{v.version}</td>
                <td>
                  {v.id === activeId ? (
                    <Badge tone="good">{t("prompts.stateServing")}</Badge>
                  ) : v.published ? (
                    <Badge tone="info">{t("prompts.statePublished")}</Badge>
                  ) : (
                    <Badge tone="neutral">{t("prompts.stateDraft")}</Badge>
                  )}
                </td>
                <td>
                  <code className="cell-code">
                    {expandedId === v.id
                      ? v.body
                      : `${v.body.slice(0, 120)}${v.body.length > 120 ? "…" : ""}`}
                  </code>
                </td>
                <td className="row-actions">
                  <button
                    className="btn"
                    onClick={() => setExpandedId(expandedId === v.id ? null : v.id)}
                  >
                    {expandedId === v.id ? t("prompts.hide") : t("prompts.view")}
                  </button>
                  <button
                    className="btn"
                    disabled={v.published}
                    onClick={() => act(`/v1/prompts/${v.id}/candidate`, undefined, t("prompts.submitted"))}
                  >
                    {t("prompts.candidate")}
                  </button>
                  <button
                    className="btn btn-primary"
                    disabled={v.id === activeId || activeUnknown}
                    onClick={async () => {
                      const ok = await prompt.confirm(
                        t("prompts.promoteConfirm", { version: v.version }),
                        t("prompts.promote"),
                        t("prompts.promoteDetail"),
                      );
                      if (ok) act(`/v1/prompts/${v.id}/promote`, undefined, t("prompts.promoted"));
                    }}
                  >
                    {t("prompts.promote")}
                  </button>
                  <button
                    className="btn"
                    disabled={v.id === activeId || activeUnknown}
                    onClick={async () => {
                      const values = await prompt.ask({
                        title: t("prompts.rejectTitle", { version: v.version }),
                        confirmLabel: t("prompts.reject"),
                        fields: [{ name: "reason", label: t("prompts.rejectReason"), required: true }],
                      });
                      if (values) act(`/v1/prompts/${v.id}/reject`, { reason: values.reason });
                    }}
                  >
                    {t("prompts.reject")}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      ) : null}
      <ListTotal shown={sorted.length} total={list.data?.total ?? 0} />

      {active.data?.active && rollbackable.length > 0 && !activeUnknown ? (
        <div className="toolbar">
          <button
            className="btn"
            onClick={async () => {
              const values = await prompt.ask({
                title: t("prompts.rollbackTitle"),
                confirmLabel: t("prompts.rollback"),
                detail: t("prompts.rollbackDetail"),
                fields: [
                  {
                    name: "to_version_id",
                    label: t("prompts.versionLabel"),
                    options: rollbackable.map(rollbackLabel),
                    required: true,
                  },
                ],
              });
              const target = values ? rollbackIds.get(values.to_version_id) : undefined;
              if (target) {
                act("/v1/prompts/rollback", {
                  template_name: template,
                  to_version_id: target,
                  reason: "manual rollback",
                });
              }
            }}
          >
            {t("prompts.rollback")}
          </button>
        </div>
      ) : null}
    </div>
  );
}
