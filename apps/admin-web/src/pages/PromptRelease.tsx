import { useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { useAction } from "../lib/useAction";
import { usePrompt } from "../components/Prompt";
import { useAsync } from "../lib/useAsync";
import type { ActivePrompt, PromptVersion } from "../lib/types";
import {
  ActionFeedback,
  Badge,
  Card,
  EmptyState,
  ErrorBanner,
  PageHeader,
  Spinner,
} from "../components/ui";

export function PromptRelease() {
  const [template, setTemplate] = useState("agent_qa");
  const [draft, setDraft] = useState("");
  const action = useAction();
  const prompt = usePrompt();

  const list = useAsync<{ items: PromptVersion[]; total: number }>(
    () =>
      apiGet<{ items: PromptVersion[]; total: number }>(
        `/v1/prompts?template_name=${encodeURIComponent(template)}`,
      ),
    [template],
  );
  const active = useAsync<ActivePrompt>(
    () =>
      apiGet<ActivePrompt>(`/v1/prompts/active?template_name=${encodeURIComponent(template)}`),
    [template],
  );

  async function act(path: string, body?: unknown, okMsg?: string) {
    const ok = await action.run(
      () => apiPost(path, body).then(() => undefined),
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

  return (
    <div className="page">
      <PageHeader
        title="Prompt Release"
        subtitle="Author, evaluate, and promote prompt template versions."
        actions={
          <div className="toolbar">
            <input
              className="text-input"
              value={template}
              onChange={(e) => setTemplate(e.target.value)}
              placeholder="template_name"
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

      {active.data?.active ? (
        <Card title="Currently serving">
          <div className="serving">
            <Badge tone="good">v{active.data.active.version}</Badge>
            <code className="serving-body">{active.data.active.body.slice(0, 240)}</code>
          </div>
        </Card>
      ) : null}

      <Card title="New draft">
        <textarea
          className="text-area"
          rows={4}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Paste the new prompt body…"
        />
        <div className="toolbar">
          <button
            className="btn btn-primary"
            disabled={!draft.trim()}
            onClick={() => {
              if (!draft.trim()) return;
              act(
                "/v1/prompts",
                { template_name: template, body: draft, notes: "" },
                "Draft created",
              ).then(() => setDraft(""));
            }}
          >
            Create draft
          </button>
        </div>
      </Card>

      <ActionFeedback error={action.error} notice={action.notice} />
      {prompt.element}
      {list.error ? <ErrorBanner message={list.error} onRetry={list.reload} /> : null}
      {list.loading ? <Spinner label="Loading versions…" /> : null}
      {list.data && sorted.length === 0 ? (
        <EmptyState message="No versions for this template yet." />
      ) : null}

      {sorted.length > 0 ? (
        <table className="table">
          <thead>
            <tr>
              <th>Version</th>
              <th>State</th>
              <th>Body</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {sorted.map((v) => (
              <tr key={v.id}>
                <td className="cell-strong">v{v.version}</td>
                <td>
                  {v.id === activeId ? (
                    <Badge tone="good">serving</Badge>
                  ) : v.published ? (
                    <Badge tone="info">published</Badge>
                  ) : (
                    <Badge tone="neutral">draft</Badge>
                  )}
                </td>
                <td>
                  <code className="cell-code">{v.body.slice(0, 120)}</code>
                </td>
                <td className="row-actions">
                  <button
                    className="btn"
                    disabled={v.published}
                    onClick={() => act(`/v1/prompts/${v.id}/candidate`, undefined, "Submitted as candidate")}
                  >
                    Candidate
                  </button>
                  <button
                    className="btn btn-primary"
                    disabled={v.id === activeId}
                    onClick={async () => {
                      const ok = await prompt.confirm(
                        `Promote v${v.version} to serving?`,
                        "Promote",
                        "Every customer-visible answer changes from this moment.",
                      );
                      if (ok) act(`/v1/prompts/${v.id}/promote`, undefined, "Promoted");
                    }}
                  >
                    Promote
                  </button>
                  <button
                    className="btn"
                    disabled={v.id === activeId}
                    onClick={async () => {
                      const values = await prompt.ask({
                        title: `Reject v${v.version}`,
                        confirmLabel: "Reject",
                        fields: [{ name: "reason", label: "Reject reason", required: true }],
                      });
                      if (values) act(`/v1/prompts/${v.id}/reject`, { reason: values.reason });
                    }}
                  >
                    Reject
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}

      {active.data?.active ? (
        <div className="toolbar">
          <button
            className="btn"
            onClick={async () => {
              const values = await prompt.ask({
                title: "Roll back to a previous version",
                confirmLabel: "Roll back",
                detail: "Enter the version id to restore. This takes effect immediately.",
                fields: [{ name: "to_version_id", label: "Version id", required: true }],
              });
              if (values) {
                act("/v1/prompts/rollback", {
                  template_name: template,
                  to_version_id: values.to_version_id,
                  reason: "manual rollback",
                });
              }
            }}
          >
            Roll back to a previous version…
          </button>
        </div>
      ) : null}
    </div>
  );
}
