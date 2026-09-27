import { useEffect, useState } from "react";
import { AlertCircle, ChevronDown, ClipboardList, ShieldCheck } from "lucide-react";
import { apiGet } from "../lib/api";

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

const REASON_LABELS: Record<string, string> = {
  FLOW_CAPABILITY_MISSING: "当前坐席或租户没有所需的工具权限。",
  FLOW_CAPABILITY_RISK_MISMATCH: "已登记能力的风险等级与模板要求不一致。",
  FLOW_CONNECTOR_CAPABILITY_MISSING: "需要的业务连接器尚未处于启用状态。",
  FLOW_OWNER_UNASSIGNED: "没有配置并分配对应的业务负责人组。",
  FLOW_EXTERNAL_WRITE_UNAVAILABLE: "当前没有受控的外部写入能力，需由业务人员处理。",
  FLOW_READY: "当前角色、已启用连接器和负责人配置满足模板基础条件。",
};

const CONNECTOR_LABELS: Record<string, string> = {
  orders_read: "订单查询",
  shipments_read: "发运查询",
  invoices_read: "发票查询",
};
const BUSINESS_LINE_LABELS: Record<string, string> = {
  component_procurement: "元器件采购",
  pcb: "PCB",
  pcba: "PCBA",
  component: "元器件",
  supply_chain: "供应链",
};
const OWNER_GROUP_LABELS: Record<string, string> = {
  quality: "质量团队",
  finance: "财务团队",
  engineering: "工程团队",
};

function fieldLabel(name: string): string {
  return FIELD_LABELS[name] ?? name;
}

function toolLabel(name: string): string {
  const names: Record<string, string> = {
    "order.get_status": "订单状态查询",
    "shipment.track": "物流状态查询",
    "billing.get_invoice": "已有发票查询",
    "case.create": "创建内部工单",
  };
  return names[name] ?? name;
}

function sourceLabel(source: string): string {
  const names: Record<string, string> = {
    customer: "客户提供",
    verified_business_record: "业务记录核验",
    server_context: "服务端上下文",
    human: "人工核验",
  };
  return names[source] ?? source;
}

export function StandardFlowCatalog() {
  const [catalog, setCatalog] = useState<FlowCatalogResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void apiGet<FlowCatalogResponse>("/v1/workbench/standard-flows")
      .then((result) => {
        if (active) setCatalog(result);
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => { active = false; };
  }, []);

  return (
    <section className="wb-panel wb-flow-catalog" aria-labelledby="wb-flow-catalog-title">
      <h3 id="wb-flow-catalog-title"><ClipboardList size={18} />标准服务流程</h3>
      <p className="wb-muted">展示已配置的流程边界与人工退出条件。实际执行仍经权限校验和 Tool Gateway。</p>
      {error ? <div className="wb-flow-error" role="alert"><AlertCircle size={15} />{error}</div> : null}
      {!catalog && !error ? <p className="wb-muted" role="status">正在读取租户能力配置…</p> : null}
      {catalog ? <div className="wb-flow-list">
        {catalog.items.map(({ template, availability }) => (
          <details className="wb-flow-item" key={template.key}>
            <summary>
              <span className="wb-flow-summary-copy">
                <strong>{template.title}</strong>
                <small>{template.business_lines.map((line) => BUSINESS_LINE_LABELS[line] ?? line).join(" · ")}</small>
              </span>
              <span className={`wb-flow-status is-${availability.status}`}>
                {availability.status === "available" ? "配置具备" : "需人工处理"}
              </span>
              <ChevronDown className="wb-flow-chevron" size={16} aria-hidden="true" />
            </summary>
            <div className="wb-flow-details">
              <p className="wb-flow-readiness">{REASON_LABELS[availability.reason_code] ?? "需要人工确认流程条件。"}</p>
              {availability.available_tools.length ? <p><strong>当前能力：</strong>{availability.available_tools.map(toolLabel).join("、")}</p> : null}
              {availability.unavailable_tools.length ? <p><strong>未具备：</strong>{availability.unavailable_tools.map(toolLabel).join("、")}</p> : null}
              {availability.optional_unavailable_tools.length ? <p><strong>可选能力未配置：</strong>{availability.optional_unavailable_tools.map(toolLabel).join("、")}</p> : null}
              {availability.unavailable_connector_capabilities.length ? <p><strong>连接器：</strong>{availability.unavailable_connector_capabilities.map((key) => CONNECTOR_LABELS[key] ?? key).join("、")}</p> : null}
              <FlowFields title="所需信息" fields={template.required_fields} />
              {template.optional_fields.length ? <FlowFields title="可补充信息" fields={template.optional_fields} /> : null}
              <p><strong>负责人：</strong>{template.owner_group ? (OWNER_GROUP_LABELS[template.owner_group] ?? template.owner_group) : "当前坐席"}{template.owner_group && !availability.owner_group_configured ? "（未配置活跃负责人）" : ""}</p>
              <p><strong>写入确认：</strong>{template.confirmation_required ? "需要坐席确认" : "只读"}</p>
              {template.allowed_confirmed_write_tools.length ? <p><strong>受控写入：</strong>{template.allowed_confirmed_write_tools.map(toolLabel).join("、")}</p> : null}
              {template.blocked_external_write ? <p className="wb-flow-caution"><ShieldCheck size={14} />外部业务写入暂未开放；不能据此向客户承诺已开票。</p> : null}
              <p><strong>部分完成：</strong>{template.partial_completion_rule}</p>
              <p><strong>超时：</strong>{template.timeout_rule}</p>
              <p><strong>取消：</strong>{template.cancellation_rule}</p>
              <p><strong>转人工条件：</strong>{template.human_exit_conditions.join("、")}</p>
            </div>
          </details>
        ))}
      </div> : null}
    </section>
  );
}

function FlowFields({ title, fields }: { title: string; fields: FlowField[] }) {
  return (
    <div className="wb-flow-field-group">
      <strong>{title}：</strong>
      <ul>{fields.map((field) => <li key={field.name}>
        {fieldLabel(field.name)} · {sourceLabel(field.source)}{field.sensitive ? " · 敏感信息" : ""}
      </li>)}</ul>
    </div>
  );
}
