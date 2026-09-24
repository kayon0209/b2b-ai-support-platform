import { useEffect, useMemo, useState } from "react";
import { Building2, Search, UserRound } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { apiGet, apiPatch } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
import { useAsync } from "../lib/useAsync";
import { ActionFeedback, Badge, Card, EmptyState, PageHeader, Spinner } from "../components/ui";

type Account = {
  account_id: string;
  name: string;
  tier: string;
  contract_status: string;
  external_crm_ref: string | null;
  attributes: Record<string, unknown>;
};
type Contact = { external_contact_id: string; channel: string | null };

export function Customers() {
  const [params, setParams] = useSearchParams();
  const selected = params.get("account");
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const list = useAsync<{ accounts: Account[] }>(() => apiGet("/v1/identity/accounts"), []);
  const detail = useAsync<{ account: Account } | null>(
    () => selected ? apiGet(`/v1/identity/accounts/${selected}`) : Promise.resolve(null),
    [selected],
  );
  const contacts = useAsync<{ contacts: Contact[] } | null>(
    () => selected ? apiGet(`/v1/identity/accounts/${selected}/contacts`) : Promise.resolve(null),
    [selected],
  );
  const filtered = useMemo(() => (list.data?.accounts ?? []).filter((account) =>
    `${account.name} ${account.external_crm_ref ?? ""}`.toLowerCase().includes(query.toLowerCase()),
  ), [list.data, query]);

  useEffect(() => {
    const account = detail.data?.account;
    if (!account) return;
    setName(account.name);
    setEditing(false);
  }, [detail.data]);

  function choose(id: string) {
    const next = new URLSearchParams(params);
    next.set("account", id);
    setParams(next);
  }
  async function save() {
    if (!selected || !name.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await apiPatch(`/v1/identity/accounts/${selected}`, {
        name: name.trim(),
      }, newIdempotencyKey());
      detail.reload();
      list.reload();
      setEditing(false);
      setNotice("客户资料已保存。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  return <div className="page customers-page">
    <PageHeader title="客户" subtitle="查看企业账户、合同等级和已绑定联系人。客户资料修改受管理员权限控制。" />
    <ActionFeedback error={error || list.error || detail.error || contacts.error} notice={notice} onRetry={() => { list.reload(); detail.reload(); contacts.reload(); }} />
    <div className="grid-case">
      <Card title="企业客户">
        <label className="customer-search"><Search size={16} /><span className="visually-hidden">搜索客户</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索企业名称或 CRM 编号" /></label>
        {list.loading ? <Spinner /> : null}
        {!list.loading && filtered.length === 0 ? <EmptyState message="没有匹配的企业客户。" /> : null}
        <ul className="case-list">{filtered.map((account) => <li key={account.account_id}><button type="button" className={`case-row${selected === account.account_id ? " active" : ""}`} onClick={() => choose(account.account_id)}><span className="case-subject"><Building2 size={16} /> {account.name}</span><span className="case-meta"><Badge tone={account.contract_status === "active" ? "good" : "warn"}>{account.tier}</Badge></span></button></li>)}</ul>
      </Card>
      <div>
        {!selected ? <Card title="客户资料"><EmptyState message="选择左侧企业查看资料。" /></Card> : null}
        {detail.loading ? <Spinner /> : null}
        {detail.data?.account ? <>
          <Card title="企业资料">
            <div className="customer-detail-head"><div><h2>{detail.data.account.name}</h2><p className="muted">CRM：{detail.data.account.external_crm_ref || "未记录"}</p></div><button className="btn" type="button" onClick={() => setEditing((current) => !current)}>{editing ? "取消编辑" : "编辑企业名称"}</button></div>
            {editing ? <div className="customer-edit-form"><label>企业名称<input value={name} maxLength={255} onChange={(event) => setName(event.target.value)} /></label><button className="btn btn-primary" type="button" disabled={busy || !name.trim()} onClick={() => void save()}>保存名称</button></div>
              : <ul className="kv"><li><span>合同等级</span><strong>{detail.data.account.tier}</strong></li><li><span>合同状态</span><strong>{detail.data.account.contract_status}</strong></li><li><span>行业</span><strong>{String(detail.data.account.attributes.industry || "未记录")}</strong></li><li><span>客户经理</span><strong>{String(detail.data.account.attributes.account_manager || "未记录")}</strong></li></ul>}
          </Card>
          <Card title="联系人"><div className="customer-contact-list">{contacts.data?.contacts.length ? contacts.data.contacts.map((contact) => <div key={contact.external_contact_id}><UserRound size={16} /><span>{contact.external_contact_id}</span><Badge tone="neutral">{contact.channel || "渠道未记录"}</Badge></div>) : <EmptyState message="尚未绑定联系人。" />}</div></Card>
        </> : null}
      </div>
    </div>
  </div>;
}
