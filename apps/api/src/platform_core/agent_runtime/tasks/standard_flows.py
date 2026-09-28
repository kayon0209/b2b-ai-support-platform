"""Bounded R2-02 business-flow catalog; templates never execute tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from platform_core.agent_runtime.semantic.validator import CapabilityView

FlowStatus = Literal["available", "needs_human"]
FlowReason = Literal[
    "FLOW_CAPABILITY_MISSING",
    "FLOW_CAPABILITY_RISK_MISMATCH",
    "FLOW_CONNECTOR_CAPABILITY_MISSING",
    "FLOW_OWNER_UNASSIGNED",
    "FLOW_EXTERNAL_WRITE_UNAVAILABLE",
    "FLOW_READY",
]

# Operator-created catalog instances remain behind their own default-off
# switch; enabling semantic task suggestions does not implicitly enable this
# new workflow surface.
FLAG_STANDARD_FLOW_INSTANCES = "agent.standard_flow_instances"
FLOW_EXECUTOR_UNAVAILABLE = "FLOW_EXECUTOR_UNAVAILABLE"
FLOW_INTERNAL_CASE_ONLY = "FLOW_INTERNAL_CASE_ONLY"


class FlowField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=63)
    source: Literal["customer", "verified_business_record", "server_context", "human"]
    sensitive: bool = False
    required: bool = True


class ConnectorRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(min_length=1, max_length=127)
    connector_capability: str = Field(min_length=1, max_length=63)


class FlowTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1, max_length=63)
    version: int = Field(default=1, ge=1)
    title: str = Field(min_length=1, max_length=127)
    business_lines: tuple[str, ...]
    intent_codes: tuple[str, ...]
    required_fields: tuple[FlowField, ...]
    optional_fields: tuple[FlowField, ...] = ()
    required_read_tools: tuple[str, ...] = ()
    optional_read_tools: tuple[str, ...] = ()
    required_connector_capabilities: tuple[ConnectorRequirement, ...] = ()
    optional_connector_capabilities: tuple[ConnectorRequirement, ...] = ()
    allowed_confirmed_write_tools: tuple[str, ...] = ()
    blocked_external_write: bool = False
    owner_group: str | None = None
    confirmation_required: bool = False
    partial_completion_rule: str = Field(min_length=1, max_length=255)
    timeout_rule: str = Field(min_length=1, max_length=255)
    cancellation_rule: str = Field(min_length=1, max_length=255)
    human_exit_conditions: tuple[str, ...]


class FlowAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    flow_key: str
    status: FlowStatus
    reason_code: FlowReason
    available_tools: tuple[str, ...] = ()
    unavailable_tools: tuple[str, ...] = ()
    optional_unavailable_tools: tuple[str, ...] = ()
    unavailable_connector_capabilities: tuple[str, ...] = ()
    optional_unavailable_connector_capabilities: tuple[str, ...] = ()
    owner_group_configured: bool


STANDARD_FLOW_TEMPLATES: tuple[FlowTemplate, ...] = (
    FlowTemplate(
        key="order_status",
        title="订单进度查询",
        business_lines=("component_procurement", "pcb", "pcba", "supply_chain"),
        intent_codes=("order.status", "order.delivery_status"),
        required_fields=(
            FlowField(name="order_id", source="customer"),
            FlowField(name="customer_account_ref", source="verified_business_record"),
        ),
        optional_fields=(
            FlowField(name="shipment_id", source="verified_business_record", required=False),
        ),
        required_read_tools=("order.get_status",),
        optional_read_tools=("shipment.track",),
        required_connector_capabilities=(
            ConnectorRequirement(tool_name="order.get_status", connector_capability="orders_read"),
        ),
        optional_connector_capabilities=(
            ConnectorRequirement(tool_name="shipment.track", connector_capability="shipments_read"),
        ),
        partial_completion_rule="已核实订单状态可先展示；缺少物流数据时不得推断发货或签收。",
        timeout_rule="外部查询超时标记 unknown 并转人工核查，不复用陈旧缓存冒充当前状态。",
        cancellation_rule="取消未提交的后续只读查询；已返回的事实保留来源和读取时间。",
        human_exit_conditions=("客户归属不匹配", "订单号歧义", "来源数据过期", "连接器不可用"),
    ),
    FlowTemplate(
        key="repair_quality_intake",
        title="PCB/PCBA 报修与质量问题受理",
        business_lines=("pcb", "pcba", "component"),
        intent_codes=("quality.repair_request", "quality.defect_report"),
        required_fields=(
            FlowField(name="customer_account_ref", source="verified_business_record"),
            FlowField(name="product_ref", source="verified_business_record"),
            FlowField(name="issue_summary", source="customer"),
        ),
        optional_fields=(
            FlowField(name="lot_or_work_order_ref", source="customer", required=False),
            FlowField(name="expected_vs_actual", source="customer", required=False),
        ),
        allowed_confirmed_write_tools=("case.create",),
        owner_group="quality",
        confirmation_required=True,
        partial_completion_rule="只确认内部工单已创建；不得声称已维修、换货或完成质量判定。",
        timeout_rule="工单创建结果不确定时按幂等键对账，禁止盲目重复创建。",
        cancellation_rule="确认前可取消草稿；工单创建后用补偿/关闭流程，不删除审计记录。",
        human_exit_conditions=("无法验证客户或产品归属", "缺陷影响生产安全", "需要质量判定"),
    ),
    FlowTemplate(
        key="invoice_application",
        title="发票申请与问题登记",
        business_lines=("component_procurement", "pcb", "pcba"),
        intent_codes=("invoice.apply", "invoice.change_request"),
        required_fields=(
            FlowField(name="customer_account_ref", source="verified_business_record"),
            FlowField(name="order_id", source="customer"),
            FlowField(name="invoice_type", source="customer"),
        ),
        optional_fields=(
            FlowField(name="tax_id", source="customer", sensitive=True, required=False),
        ),
        optional_read_tools=("billing.get_invoice",),
        optional_connector_capabilities=(
            ConnectorRequirement(
                tool_name="billing.get_invoice", connector_capability="invoices_read"
            ),
        ),
        allowed_confirmed_write_tools=("case.create",),
        blocked_external_write=True,
        owner_group="finance",
        confirmation_required=True,
        partial_completion_rule="当前仅可核对已有发票或登记内部申请；没有发票系统写能力时不得声称已开票。",
        timeout_rule="查询超时标记 unknown；申请登记失败转财务人工，不推断开票结果。",
        cancellation_rule="开票前允许取消内部申请；任何已发生的外部动作须由财务对账。",
        human_exit_conditions=(
            "缺少财务负责人配置",
            "需要开具/红冲/变更发票",
            "税务信息需人工核验",
        ),
    ),
    FlowTemplate(
        key="technical_escalation",
        title="制造与技术问题升级",
        business_lines=("pcb", "pcba", "component"),
        intent_codes=("technical.process_question", "technical.escalation"),
        required_fields=(
            FlowField(name="customer_account_ref", source="verified_business_record"),
            FlowField(name="product_ref", source="verified_business_record"),
            FlowField(name="question_or_symptom", source="customer"),
        ),
        optional_fields=(
            FlowField(name="revision", source="customer", required=False),
            FlowField(name="work_order_ref", source="verified_business_record", required=False),
        ),
        allowed_confirmed_write_tools=("case.create",),
        owner_group="engineering",
        confirmation_required=True,
        partial_completion_rule="只报告资料已转交/内部工单状态；不生成未核验的工艺参数或可制造性承诺。",
        timeout_rule="工程队列不可用时保留待人工任务，不把超时表示为已升级。",
        cancellation_rule="转交前可取消；已接单后由工程负责人记录撤回或关闭原因。",
        human_exit_conditions=("需要工程判断", "涉及安全/法规风险", "关键产品版本无法确认"),
    ),
)


def resolve_flow_availability(
    template: FlowTemplate,
    *,
    capabilities: dict[str, CapabilityView],
    configured_owner_groups: frozenset[str],
    active_connector_capabilities: frozenset[str] = frozenset(),
) -> FlowAvailability:
    """Check policy-filtered tools, active connectors, and staffed owner groups."""
    required_tools = set(template.required_read_tools) | set(template.allowed_confirmed_write_tools)
    optional_tools = set(template.optional_read_tools)
    required_connectors = {
        requirement.tool_name: requirement.connector_capability
        for requirement in template.required_connector_capabilities
    }
    optional_connectors = {
        requirement.tool_name: requirement.connector_capability
        for requirement in template.optional_connector_capabilities
    }
    available: list[str] = []
    unavailable: list[str] = []
    optional_unavailable: list[str] = []
    unavailable_connectors: set[str] = set()
    optional_unavailable_connectors: set[str] = set()
    risk_mismatch = False
    for name in sorted(required_tools):
        cap = capabilities.get(name)
        expected = "read" if name in template.required_read_tools else "confirmed_write"
        connector_missing = (
            name in required_connectors
            and required_connectors[name] not in active_connector_capabilities
        )
        if connector_missing:
            unavailable_connectors.add(required_connectors[name])
        if cap is None:
            unavailable.append(name)
        elif cap.risk_class != expected:
            unavailable.append(name)
            risk_mismatch = True
        elif connector_missing:
            unavailable.append(name)
        else:
            available.append(name)

    for name in sorted(optional_tools):
        cap = capabilities.get(name)
        connector_missing = (
            name in optional_connectors
            and optional_connectors[name] not in active_connector_capabilities
        )
        if connector_missing:
            optional_unavailable_connectors.add(optional_connectors[name])
        if cap is None:
            optional_unavailable.append(name)
        elif cap.risk_class != "read":
            optional_unavailable.append(name)
        elif connector_missing:
            optional_unavailable.append(name)
        else:
            available.append(name)

    owner_configured = (
        template.owner_group is None or template.owner_group in configured_owner_groups
    )
    status: FlowStatus
    reason: FlowReason
    if risk_mismatch:
        status, reason = "needs_human", "FLOW_CAPABILITY_RISK_MISMATCH"
    elif unavailable:
        status = "needs_human"
        reason = (
            "FLOW_CONNECTOR_CAPABILITY_MISSING"
            if unavailable_connectors
            else "FLOW_CAPABILITY_MISSING"
        )
    elif template.blocked_external_write:
        status, reason = "needs_human", "FLOW_EXTERNAL_WRITE_UNAVAILABLE"
    elif not owner_configured:
        status, reason = "needs_human", "FLOW_OWNER_UNASSIGNED"
    else:
        status, reason = "available", "FLOW_READY"
    return FlowAvailability(
        flow_key=template.key,
        status=status,
        reason_code=reason,
        available_tools=tuple(available),
        unavailable_tools=tuple(unavailable),
        optional_unavailable_tools=tuple(optional_unavailable),
        unavailable_connector_capabilities=tuple(sorted(unavailable_connectors)),
        optional_unavailable_connector_capabilities=tuple(sorted(optional_unavailable_connectors)),
        owner_group_configured=owner_configured,
    )


def get_standard_flow(flow_key: str) -> FlowTemplate | None:
    """Look up one versioned-in-code template without fuzzy name matching."""
    return next((flow for flow in STANDARD_FLOW_TEMPLATES if flow.key == flow_key), None)


__all__ = [
    "FLAG_STANDARD_FLOW_INSTANCES",
    "FLOW_EXECUTOR_UNAVAILABLE",
    "FLOW_INTERNAL_CASE_ONLY",
    "STANDARD_FLOW_TEMPLATES",
    "ConnectorRequirement",
    "FlowAvailability",
    "FlowField",
    "FlowTemplate",
    "get_standard_flow",
    "resolve_flow_availability",
]
