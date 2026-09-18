import { useState } from "react";
import { apiDelete, apiGet, apiPost } from "../lib/api";
import { useAsync } from "../lib/useAsync";
import type { InviteResult, Member } from "../lib/types";
import {
  ActionFeedback,
  Badge,
  Card,
  EmptyState,
  ErrorBanner,
  PageHeader,
  Spinner,
} from "../components/ui";
import { usePrompt } from "../components/Prompt";
import { useAction } from "../lib/useAction";
import { titleCase } from "../lib/format";

// tenant_owner is the bootstrap role and cannot be assigned through an invite
// (the API rejects it), so it is not offered here either.
const ASSIGNABLE_ROLES = [
  "security_admin",
  "support_admin",
  "knowledge_manager",
  "support_agent",
  "support_viewer",
  "integration_service",
  "auditor",
];

function idem() {
  return crypto.randomUUID?.() ?? `idem-${Date.now()}-${Math.random()}`;
}

export function Members() {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("support_agent");
  const [invite, setInvite] = useState<InviteResult | null>(null);
  const action = useAction();
  const prompt = usePrompt();

  const members = useAsync<{ items: Member[]; total: number }>(
    () => apiGet<{ items: Member[]; total: number }>(`/v1/identity/members`),
    [],
  );

  /** Server actions report through `action`, so a failure is not rendered as
   *  plain muted text indistinguishable from a success. */
  async function run(fn: () => Promise<void>) {
    await action.run(fn);
  }

  const onInvite = () =>
    run(async () => {
      if (!email.trim()) return;
      const res = await apiPost<InviteResult>(
        "/v1/identity/members/invite",
        { email: email.trim(), role },
        idem(),
      );
      setInvite(res);
      setEmail("");
      members.reload();
    });

  const onChangeRole = (m: Member, next: string) =>
    run(async () => {
      await apiPost(`/v1/identity/members/${m.user_id}`, { role: next }, idem());
      members.reload();
    });

  const onRemove = (m: Member) =>
    run(async () => {
      const ok = await prompt.confirm(
        `Remove ${m.email} from this tenant?`,
        "Remove",
        "They lose access immediately. Their account is not deleted.",
      );
      if (!ok) return;
      await apiDelete(`/v1/identity/members/${m.user_id}`, idem());
      members.reload();
    });

  return (
    <div className="page">
      <PageHeader
        title="Members"
        subtitle="Invite and manage who can access this tenant."
      />

      {prompt.element}

      <Card title="Invite a member">
        <div className="toolbar">
          <input
            className="text-input"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="person@company.com"
            type="email"
          />
          <label className="field">
            <span>Role</span>
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              {ASSIGNABLE_ROLES.map((r) => (
                <option key={r} value={r}>
                  {titleCase(r)}
                </option>
              ))}
            </select>
          </label>
          <button className="btn btn-primary" disabled={action.busy || !email.trim()} onClick={onInvite}>
            Send invite
          </button>
        </div>
        {invite ? (
          <div className="serving">
            {invite.invitation_token ? (
              <>
                <Badge tone="good">invite created</Badge>
                <code className="serving-body">{invite.invitation_token}</code>
                <button
                  className="btn btn-ghost"
                  onClick={() => {
                    void navigator.clipboard?.writeText(invite.invitation_token ?? "");
                    action.succeed("Token copied to clipboard.");
                  }}
                >
                  Copy
                </button>
              </>
            ) : (
              <Badge tone="warn">{invite.message ?? "no token returned"}</Badge>
            )}
          </div>
        ) : null}
        <ActionFeedback error={action.error} notice={action.notice} />
      </Card>

      {members.error ? <ErrorBanner message={members.error} onRetry={members.reload} /> : null}
      {members.loading ? <Spinner label="Loading members…" /> : null}
      {members.data && members.data.items.length === 0 ? (
        <EmptyState message="No members yet." />
      ) : null}

      {members.data && members.data.items.length > 0 ? (
        <table className="table">
          <thead>
            <tr>
              <th>Member</th>
              <th>Role</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {members.data.items.map((m) => (
              <tr key={m.user_id}>
                <td className="cell-strong">
                  {m.display_name}
                  <div className="muted">{m.email}</div>
                </td>
                <td>
                  {m.role === "tenant_owner" ? (
                    <Badge tone="good">{titleCase(m.role)}</Badge>
                  ) : (
                    <select
                      value={m.role}
                      disabled={action.busy}
                      onChange={(e) => onChangeRole(m, e.target.value)}
                    >
                      {ASSIGNABLE_ROLES.map((r) => (
                        <option key={r} value={r}>
                          {titleCase(r)}
                        </option>
                      ))}
                    </select>
                  )}
                </td>
                <td>
                  <Badge tone={m.status === "active" ? "good" : "neutral"}>
                    {titleCase(m.status)}
                  </Badge>
                </td>
                <td className="row-actions">
                  <button
                    className="btn"
                    disabled={action.busy || m.role === "tenant_owner"}
                    onClick={() => onRemove(m)}
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}
