import { useState } from "react";
import { apiDelete, apiGet, apiPost } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
import { useAsync } from "../lib/useAsync";
import type { InviteResult, Member } from "../lib/types";
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
import { usePrompt } from "../components/Prompt";
import { useAction } from "../lib/useAction";
import { useLang, type DictKey } from "../lib/i18n";

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

export function Members() {
  const { t } = useLang();
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
  async function run(fn: () => Promise<void>, okMsg?: string) {
    return action.run(fn, okMsg);
  }

  const onInvite = () =>
    run(async () => {
      if (!email.trim()) return;
      // Clear the previous result first: a one-time token left on screen
      // while a *different* address is being invited reads as that
      // address's token, and it survives a failed attempt too.
      setInvite(null);
      const res = await apiPost<InviteResult>(
        "/v1/identity/members/invite",
        { email: email.trim(), role },
        newIdempotencyKey(),
      );
      setInvite(res);
      setEmail("");
      members.reload();
    }, t("members.invited", { email: email.trim() }));

  const onChangeRole = (m: Member, next: string) =>
    run(async () => {
      // A role change grants or removes powers the moment it lands, and the
      // select commits on a single click - so it confirms like removal does.
      const ok = await prompt.confirm(
        t("members.changeRoleConfirm", {
          email: m.email,
          role: t(`role.${next}` as DictKey),
        }),
        t("members.changeRole"),
        t("members.changeRoleDetail"),
      );
      if (!ok) {
        members.reload(); // snap the select back to the stored role
        return;
      }
      await apiPost(`/v1/identity/members/${m.membership_id}`, { role: next }, newIdempotencyKey());
      members.reload();
    }, t("members.roleChanged", { email: m.email }));

  const onRemove = (m: Member) =>
    run(async () => {
      const ok = await prompt.confirm(
        t("members.removeConfirm", { email: m.email }),
        t("members.remove"),
        t("members.removeDetail"),
      );
      if (!ok) return;
      await apiDelete(`/v1/identity/members/${m.membership_id}`, newIdempotencyKey());
      members.reload();
    }, t("members.removed", { email: m.email }));

  return (
    <div className="page">
      <PageHeader title={t("members.title")} subtitle={t("members.subtitle")} />

      {prompt.element}

      <Card title={t("members.inviteTitle")}>
        <div className="toolbar">
          <input
            className="text-input"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder={t("members.emailPlaceholder")}
            type="email"
          />
          <label className="field">
            <span>{t("common.role")}</span>
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              {ASSIGNABLE_ROLES.map((r) => (
                <option key={r} value={r}>
                  {t(`role.${r}` as DictKey)}
                </option>
              ))}
            </select>
          </label>
          <button
            className="btn btn-primary"
            disabled={action.busy || !email.trim()}
            onClick={onInvite}
          >
            {t("members.sendInvite")}
          </button>
        </div>
        {invite ? (
          <div className="serving">
            {invite.invitation_token ? (
              <>
                <Badge tone="good">{t("members.inviteCreated")}</Badge>
                <code className="serving-body">{invite.invitation_token}</code>
                <button
                  className="btn btn-ghost"
                  onClick={() => {
                    void navigator.clipboard?.writeText(invite.invitation_token ?? "");
                    action.succeed(t("members.copied"));
                  }}
                >
                  {t("common.copy")}
                </button>
              </>
            ) : (
              <Badge tone="warn">{invite.message ?? t("members.noToken")}</Badge>
            )}
          </div>
        ) : null}
        <ActionFeedback error={action.error} notice={action.notice} />
      </Card>

      <LoadError error={members.error} status={members.errorStatus} onRetry={members.reload} />
      {members.loading ? <Spinner label={t("members.loading")} /> : null}
      {members.data && members.data.items.length === 0 ? (
        <EmptyState message={t("members.empty")} />
      ) : null}

      {members.data && members.data.items.length > 0 ? (
        <div className="table-scroll">
        <table className="table">
          <thead>
            <tr>
              <th scope="col">{t("members.headerMember")}</th>
              <th scope="col">{t("common.role")}</th>
              <th scope="col">{t("members.headerStatus")}</th>
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
                    <Badge tone="good">{t(`role.${m.role}` as DictKey)}</Badge>
                  ) : (
                    <select
                      value={m.role}
                      disabled={action.busy}
                      onChange={(e) => onChangeRole(m, e.target.value)}
                    >
                      {ASSIGNABLE_ROLES.map((r) => (
                        <option key={r} value={r}>
                          {t(`role.${r}` as DictKey)}
                        </option>
                      ))}
                    </select>
                  )}
                </td>
                <td>
                  <Badge tone={m.status === "active" ? "good" : "neutral"}>
                    {t(`member.status.${m.status}` as DictKey)}
                  </Badge>
                </td>
                <td className="row-actions">
                  <button
                    className="btn"
                    disabled={action.busy || m.role === "tenant_owner"}
                    onClick={() => onRemove(m)}
                  >
                    {t("members.remove")}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      ) : null}
      <ListTotal shown={members.data?.items.length ?? 0} total={members.data?.total ?? 0} />
    </div>
  );
}
