import { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, ChevronDown, ClipboardList, ShieldCheck } from "lucide-react";
import { apiGet, apiPost } from "../lib/api";
import { useLang, type Lang } from "../lib/i18n";
import { newIdempotencyKey } from "../lib/idempotency";

type Availability = {
  status: "available" | "needs_human";
  reason_code: string;
  available_tools: string[];
  unavailable_tools: string[];
  optional_unavailable_tools: string[];
  unavailable_connector_capabilities: string[];
  optional_unavailable_connector_capabilities: string[];
  owner_group_configured: boolean;
};

type FlowField = {
  name: string;
  source: string;
  sensitive: boolean;
  required: boolean;
};

type FlowTemplate = {
  key: string;
  title: string;
  business_lines: string[];
  intent_codes: string[];
  required_fields: FlowField[];
  optional_fields: FlowField[];
  required_read_tools: string[];
  optional_read_tools: string[];
  allowed_confirmed_write_tools: string[];
  blocked_external_write: boolean;
  owner_group: string | null;
  confirmation_required: boolean;
  partial_completion_rule: string;
  timeout_rule: string;
  cancellation_rule: string;
  human_exit_conditions: string[];
};

interface FlowItem {
  template: FlowTemplate;
  availability: Availability;
}

interface FlowCatalogResponse {
  items: FlowItem[];
  execution_requires_tool_gateway: true;
  instances_enabled: boolean;
}

interface StandardFlowCatalogProps {
  conversationRef: string;
  leaseVersion: number;
  isOwner: boolean;
  onStarted?: () => void;
}

const FIELD_LABELS: Record<string, string> = {
  order_id: "订单号",
  customer_account_ref: "已核验客户归属",
  shipment_id: "物流单号",
  product_ref: "产品/料号",
  issue_summary: "问题描述",
  lot_or_work_order_ref: "批次或工单号",
  expected_vs_actual: "期望与实际情况",
  invoice_type: "发票类型",
  tax_id: "税号",
  question_or_symptom: "技术问题或现象",
  revision: "产品版本",
  work_order_ref: "生产工单号",
};
const FIELD_LABELS_EN: Record<string, string> = {
  order_id: "Order number",
  customer_account_ref: "Verified customer account",
  shipment_id: "Shipment number",
  product_ref: "Product reference",
  issue_summary: "Issue description",
  lot_or_work_order_ref: "Lot or work-order reference",
  expected_vs_actual: "Expected versus actual",
  invoice_type: "Invoice type",
  tax_id: "Tax ID",
  question_or_symptom: "Technical issue or symptom",
  revision: "Product revision",
  work_order_ref: "Work-order reference",
};

const REASON_LABELS: Record<string, string> = {
  FLOW_CAPABILITY_MISSING: "当前坐席或租户没有所需的工具权限。",
  FLOW_CAPABILITY_RISK_MISMATCH: "已登记能力的风险等级与模板要求不一致。",
  FLOW_CONNECTOR_CAPABILITY_MISSING: "需要的业务连接器尚未处于启用状态。",
  FLOW_OWNER_UNASSIGNED: "没有配置并分配对应的业务负责人组。",
  FLOW_EXTERNAL_WRITE_UNAVAILABLE: "当前没有受控的外部写入能力，需由业务人员处理。",
  FLOW_READY: "当前角色、已启用连接器和负责人配置满足模板基础条件。",
};
const REASON_LABELS_EN: Record<string, string> = {
  FLOW_CAPABILITY_MISSING: "The agent or tenant lacks a required tool permission.",
  FLOW_CAPABILITY_RISK_MISMATCH: "The registered tool risk does not match this flow's requirements.",
  FLOW_CONNECTOR_CAPABILITY_MISSING: "A required business capability is not enabled.",
  FLOW_OWNER_UNASSIGNED: "No active owner group is configured for this flow.",
  FLOW_EXTERNAL_WRITE_UNAVAILABLE: "No controlled external write is available; a business owner must handle this step.",
  FLOW_READY: "The role, enabled capabilities, and owner configuration meet the flow's basic requirements.",
};

const CONNECTOR_LABELS: Record<string, string> = {
  orders_read: "订单查询",
  shipments_read: "发运查询",
  invoices_read: "发票查询",
};
const CONNECTOR_LABELS_EN: Record<string, string> = {
  orders_read: "Order lookup",
  shipments_read: "Shipment lookup",
  invoices_read: "Invoice lookup",
};
const BUSINESS_LINE_LABELS: Record<string, string> = {
  component_procurement: "元器件采购",
  pcb: "PCB",
  pcba: "PCBA",
  component: "元器件",
  supply_chain: "供应链",
};
const BUSINESS_LINE_LABELS_EN: Record<string, string> = {
  component_procurement: "Component procurement",
  pcb: "PCB",
  pcba: "PCBA",
  component: "Components",
  supply_chain: "Supply chain",
};
const OWNER_GROUP_LABELS: Record<string, string> = {
  quality: "质量团队",
  finance: "财务团队",
  engineering: "工程团队",
};
const OWNER_GROUP_LABELS_EN: Record<string, string> = {
  quality: "Quality team",
  finance: "Finance team",
  engineering: "Engineering team",
};

const FLOW_COPY_EN: Record<string, { title: string; partial: string; timeout: string; cancel: string; exits: Record<string, string> }> = {
  order_status: {
    title: "Order status lookup",
    partial: "Show verified order status only; do not infer shipping or delivery when shipment data is missing.",
    timeout: "Mark a timed-out lookup as unknown and route it for reconciliation; do not present stale data as current.",
    cancel: "Cancel unsubmitted follow-up lookups. Preserve the source and read time of facts already returned.",
    exits: { "客户归属不匹配": "Customer ownership mismatch", "订单号歧义": "Ambiguous order number", "来源数据过期": "Source data is stale", "连接器不可用": "Read capability unavailable" },
  },
  repair_quality_intake: {
    title: "PCB/PCBA repair and quality intake",
    partial: "Confirm only that an internal Case was created; do not claim repair, replacement, or a quality decision.",
    timeout: "Reconcile an uncertain Case creation by idempotency key; never create a duplicate blindly.",
    cancel: "A draft can be cancelled before confirmation. After Case creation, use a compensating close flow and preserve the audit record.",
    exits: { "无法验证客户或产品归属": "Customer or product ownership cannot be verified", "缺陷影响生产安全": "The defect may affect production safety", "需要质量判定": "A quality decision is required" },
  },
  invoice_application: {
    title: "Invoice request and issue intake",
    partial: "Only check an existing invoice or record an internal request. Do not claim an invoice was issued without invoice-system write access.",
    timeout: "Mark timed-out lookups as unknown; route failed request intake to Finance without inferring an invoice result.",
    cancel: "An internal request can be cancelled before invoicing. Finance must reconcile any external action already taken.",
    exits: { "缺少财务负责人配置": "No Finance owner is configured", "需要开具/红冲/变更发票": "An invoice must be issued, reversed, or changed", "税务信息需人工核验": "Tax information needs human verification" },
  },
  technical_escalation: {
    title: "Manufacturing and technical escalation",
    partial: "Report only that information was handed off or the internal Case status; do not invent process parameters or manufacturability claims.",
    timeout: "Keep the task open for human follow-up if the engineering queue is unavailable; do not report a timeout as an escalation.",
    cancel: "A handoff can be cancelled before transfer. After acceptance, the engineering owner records the withdrawal or closure reason.",
    exits: { "需要工程判断": "Engineering judgment is required", "涉及安全/法规风险": "Safety or regulatory risk is involved", "关键产品版本无法确认": "The product revision cannot be confirmed" },
  },
};

function fieldLabel(name: string, lang: Lang): string {
  const labels = lang === "zh" ? FIELD_LABELS : FIELD_LABELS_EN;
  return labels[name] ?? name.replace(/_/g, " ");
}

function toolLabel(name: string, lang: Lang): string {
  const names: Record<string, string> = lang === "zh" ? {
    "order.get_status": "订单状态查询",
    "shipment.track": "物流状态查询",
    "billing.get_invoice": "已有发票查询",
    "case.create": "创建内部工单",
  } : {
    "order.get_status": "Order status lookup",
    "shipment.track": "Track shipment",
    "billing.get_invoice": "Check existing invoice",
    "case.create": "Create internal Case",
  };
  return names[name] ?? name;
}

function sourceLabel(source: string, lang: Lang): string {
  const names: Record<string, string> = lang === "zh" ? {
    customer: "客户提供",
    verified_business_record: "业务记录核验",
    server_context: "服务端上下文",
    human: "人工核验",
  } : {
    customer: "Customer-provided",
    verified_business_record: "Verified business record",
    server_context: "Server context",
    human: "Human verified",
  };
  return names[source] ?? source;
}

export function StandardFlowCatalog({
  conversationRef,
  leaseVersion,
  isOwner,
  onStarted,
}: StandardFlowCatalogProps) {
  const { lang } = useLang();
  const [catalog, setCatalog] = useState<FlowCatalogResponse | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [startingKey, setStartingKey] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const catalogRequest = useRef(0);

  const loadCatalog = useCallback(async () => {
    const request = ++catalogRequest.current;
    setCatalogLoading(true);
    setCatalogError(null);
    try {
      const result = await apiGet<FlowCatalogResponse>("/v1/workbench/standard-flows");
      if (request === catalogRequest.current) setCatalog(result);
    } catch (reason) {
      if (request === catalogRequest.current) {
        setCatalogError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      if (request === catalogRequest.current) setCatalogLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadCatalog();
    return () => { catalogRequest.current += 1; };
  }, [loadCatalog]);

  async function startFlow(flowKey: string) {
    setStartingKey(flowKey);
    setActionError(null);
    try {
      const result = await apiPost<{ task: { flow_title: string | null }; replayed: boolean }>(
        `/v1/workbench/conversations/${conversationRef}/standard-flows/tasks`,
        { flow_key: flowKey, expected_lease_version: leaseVersion },
        newIdempotencyKey(),
      );
      setAnnouncement(lang === "zh"
        ? `${result.task.flow_title ?? "标准流程"}已加入当前会话任务。`
        : `${FLOW_COPY_EN[flowKey]?.title ?? "Standard flow"} was added to this conversation.`);
      onStarted?.();
      window.dispatchEvent(
        new CustomEvent("workbench:tasks-changed", { detail: { conversationRef } }),
      );
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setStartingKey(null);
    }
  }

  return (
    <section className="wb-panel wb-flow-catalog" aria-labelledby="wb-flow-catalog-title">
      <h3 id="wb-flow-catalog-title"><ClipboardList size={18} />{lang === "zh" ? "标准服务流程" : "Standard service flows"}</h3>
      <p className="wb-muted">{lang === "zh"
        ? "发起后绑定当前会话最新客户消息。local/test Demo 配置读取能力后可查合成订单；质量、技术和发票流程只会准备需确认的平台内部 Case，不调用真实业务系统。"
        : "A flow binds to the latest customer message in this conversation. Local/test Demo can look up synthetic orders; quality, technical, and invoice flows prepare an internal Case for confirmation only."}</p>
      <p className="sr-only" role="status" aria-live="polite">{announcement}</p>
      {catalogError ? (
        <div className="wb-flow-error" role="alert">
          <AlertCircle size={15} />{catalogError}
          <button type="button" className="wb-btn wb-btn-ghost" disabled={catalogLoading} onClick={() => void loadCatalog()}>
            {catalogLoading ? (lang === "zh" ? "正在重试…" : "Retrying…") : (lang === "zh" ? "重试加载" : "Retry loading")}
          </button>
        </div>
      ) : null}
      {actionError ? <div className="wb-flow-error" role="alert"><AlertCircle size={15} />{actionError}</div> : null}
      {catalogLoading && !catalog ? <p className="wb-muted" role="status">{lang === "zh" ? "正在读取租户能力配置…" : "Loading tenant capabilities…"}</p> : null}
      {catalog ? <div className="wb-flow-list">
        {catalog.items.map(({ template, availability }) => (
          <details className="wb-flow-item" key={template.key}>
            <summary>
              <span className="wb-flow-summary-copy">
                <strong>{lang === "zh" ? template.title : FLOW_COPY_EN[template.key]?.title ?? template.title}</strong>
                <small>{template.business_lines.map((line) => (lang === "zh" ? BUSINESS_LINE_LABELS : BUSINESS_LINE_LABELS_EN)[line] ?? line).join(" · ")}</small>
              </span>
              <span className={`wb-flow-status is-${availability.status}`}>
                {lang === "zh"
                  ? availability.status === "available" ? "配置具备" : "需人工处理"
                  : availability.status === "available" ? "Ready" : "Needs human follow-up"}
              </span>
              <ChevronDown className="wb-flow-chevron" size={16} aria-hidden="true" />
            </summary>
            <div className="wb-flow-details">
              <p className="wb-flow-readiness">{(lang === "zh" ? REASON_LABELS : REASON_LABELS_EN)[availability.reason_code] ?? (lang === "zh" ? "需要人工确认流程条件。" : "A human must review the flow requirements.")}</p>
              {availability.available_tools.length ? <p><strong>{lang === "zh" ? "当前能力：" : "Available tools: "}</strong>{availability.available_tools.map((name) => toolLabel(name, lang)).join(lang === "zh" ? "、" : ", ")}</p> : null}
              {availability.unavailable_tools.length ? <p><strong>{lang === "zh" ? "未具备：" : "Unavailable: "}</strong>{availability.unavailable_tools.map((name) => toolLabel(name, lang)).join(lang === "zh" ? "、" : ", ")}</p> : null}
              {availability.optional_unavailable_tools.length ? <p><strong>{lang === "zh" ? "可选能力未配置：" : "Optional tools unavailable: "}</strong>{availability.optional_unavailable_tools.map((name) => toolLabel(name, lang)).join(lang === "zh" ? "、" : ", ")}</p> : null}
              {availability.unavailable_connector_capabilities.length ? <p><strong>{lang === "zh" ? "连接器：" : "Capabilities: "}</strong>{availability.unavailable_connector_capabilities.map((key) => (lang === "zh" ? CONNECTOR_LABELS : CONNECTOR_LABELS_EN)[key] ?? key).join(lang === "zh" ? "、" : ", ")}</p> : null}
              <FlowFields title={lang === "zh" ? "所需信息" : "Required information"} fields={template.required_fields} lang={lang} />
              {template.optional_fields.length ? <FlowFields title={lang === "zh" ? "可补充信息" : "Optional information"} fields={template.optional_fields} lang={lang} /> : null}
              <p><strong>{lang === "zh" ? "负责人：" : "Owner: "}</strong>{template.owner_group ? (lang === "zh" ? OWNER_GROUP_LABELS : OWNER_GROUP_LABELS_EN)[template.owner_group] ?? template.owner_group : (lang === "zh" ? "当前坐席" : "Current agent")}{template.owner_group && !availability.owner_group_configured ? (lang === "zh" ? "（未配置活跃负责人）" : " (no active owner configured)") : ""}</p>
              <p><strong>{lang === "zh" ? "写入确认：" : "Write confirmation: "}</strong>{template.confirmation_required ? (lang === "zh" ? "需要坐席确认" : "Agent confirmation required") : (lang === "zh" ? "只读" : "Read only")}</p>
              {template.allowed_confirmed_write_tools.length ? <p><strong>{lang === "zh" ? "受控写入：" : "Controlled writes: "}</strong>{template.allowed_confirmed_write_tools.map((name) => toolLabel(name, lang)).join(lang === "zh" ? "、" : ", ")}</p> : null}
              {template.blocked_external_write ? <p className="wb-flow-caution"><ShieldCheck size={14} />{lang === "zh" ? "外部业务写入暂未开放；不能据此向客户承诺已开票。" : "External business writes are unavailable; do not tell the customer an invoice was issued."}</p> : null}
              <p><strong>{lang === "zh" ? "部分完成：" : "Partial completion: "}</strong>{lang === "zh" ? template.partial_completion_rule : FLOW_COPY_EN[template.key]?.partial ?? template.partial_completion_rule}</p>
              <p><strong>{lang === "zh" ? "超时：" : "Timeout: "}</strong>{lang === "zh" ? template.timeout_rule : FLOW_COPY_EN[template.key]?.timeout ?? template.timeout_rule}</p>
              <p><strong>{lang === "zh" ? "取消：" : "Cancellation: "}</strong>{lang === "zh" ? template.cancellation_rule : FLOW_COPY_EN[template.key]?.cancel ?? template.cancellation_rule}</p>
              <p><strong>{lang === "zh" ? "转人工条件：" : "Human handoff conditions: "}</strong>{template.human_exit_conditions.map((condition) => lang === "zh" ? condition : FLOW_COPY_EN[template.key]?.exits[condition] ?? condition).join(lang === "zh" ? "、" : "; ")}</p>
              <button
                type="button"
                className="wb-btn wb-btn-primary"
                disabled={!catalog.instances_enabled || !isOwner || startingKey !== null}
                title={!catalog.instances_enabled ? (lang === "zh" ? "租户尚未启用标准流程实例" : "Standard flow instances are disabled for this tenant") : !isOwner ? (lang === "zh" ? "只有当前会话负责人可以发起流程" : "Only the current conversation owner can start a flow") : undefined}
                onClick={() => void startFlow(template.key)}
              >
                {startingKey === template.key ? (lang === "zh" ? "正在发起…" : "Starting…") : (lang === "zh" ? "发起到当前会话" : "Start in this conversation")}
              </button>
              {!catalog.instances_enabled ? (
                <p className="wb-flow-caution">{lang === "zh" ? "会话流程实例默认关闭，需先由管理员开启租户开关。" : "Flow instances are disabled by default; an administrator must enable the tenant flag."}</p>
              ) : !isOwner ? (
                <p className="wb-flow-caution">{lang === "zh" ? "只有当前会话负责人可以发起流程。" : "Only the current conversation owner can start a flow."}</p>
              ) : null}
            </div>
          </details>
        ))}
      </div> : null}
    </section>
  );
}

function FlowFields({ title, fields, lang }: { title: string; fields: FlowField[]; lang: Lang }) {
  return (
    <div className="wb-flow-field-group">
      <strong>{title}{lang === "zh" ? "：" : ": "}</strong>
      <ul>{fields.map((field) => <li key={field.name}>
        {fieldLabel(field.name, lang)} · {sourceLabel(field.source, lang)}{field.sensitive ? ` · ${lang === "zh" ? "敏感信息" : "Sensitive"}` : ""}
      </li>)}</ul>
    </div>
  );
}
