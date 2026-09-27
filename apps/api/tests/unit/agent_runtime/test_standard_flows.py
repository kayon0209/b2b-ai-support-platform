"""R2-02 standard-flow templates remain bounded by actual capabilities."""

from platform_core.agent_runtime.semantic.validator import CapabilityView
from platform_core.agent_runtime.tasks.standard_flows import (
    STANDARD_FLOW_TEMPLATES,
    get_standard_flow,
    resolve_flow_availability,
)

REGISTERED_TOOL_NAMES = {
    "order.get_status",
    "shipment.track",
    "billing.get_invoice",
    "case.create",
}
REGISTERED_CONNECTOR_CAPABILITIES = {
    "orders_read",
    "shipments_read",
    "invoices_read",
}


def _cap(tool_name: str, risk: str) -> CapabilityView:
    return CapabilityView(
        tool_name=tool_name,
        risk_class=risk,
        allowed_task_kinds=frozenset({"read" if risk == "read" else "write"}),
    )


def test_catalog_declares_four_bounded_flows_and_no_unknown_tools() -> None:
    assert {item.key for item in STANDARD_FLOW_TEMPLATES} == {
        "order_status",
        "repair_quality_intake",
        "invoice_application",
        "technical_escalation",
    }
    for flow in STANDARD_FLOW_TEMPLATES:
        assert set(flow.required_read_tools + flow.optional_read_tools).issubset(
            REGISTERED_TOOL_NAMES
        )
        assert set(flow.allowed_confirmed_write_tools).issubset(REGISTERED_TOOL_NAMES)
        connector_requirements = (
            flow.required_connector_capabilities + flow.optional_connector_capabilities
        )
        assert {item.connector_capability for item in connector_requirements}.issubset(
            REGISTERED_CONNECTOR_CAPABILITIES
        )
        assert flow.partial_completion_rule
        assert flow.timeout_rule
        assert flow.cancellation_rule
        assert flow.human_exit_conditions


def test_order_status_can_run_read_only_and_treats_shipping_as_optional() -> None:
    flow = get_standard_flow("order_status")
    assert flow is not None
    availability = resolve_flow_availability(
        flow,
        capabilities={"order.get_status": _cap("order.get_status", "read")},
        configured_owner_groups=frozenset(),
        active_connector_capabilities=frozenset({"orders_read"}),
    )

    assert availability.status == "available"
    assert availability.available_tools == ("order.get_status",)
    assert availability.optional_unavailable_tools == ("shipment.track",)
    assert availability.optional_unavailable_connector_capabilities == ("shipments_read",)


def test_declared_read_tool_without_an_active_connector_is_not_available() -> None:
    flow = get_standard_flow("order_status")
    assert flow is not None
    availability = resolve_flow_availability(
        flow,
        capabilities={"order.get_status": _cap("order.get_status", "read")},
        configured_owner_groups=frozenset(),
    )

    assert availability.status == "needs_human"
    assert availability.reason_code == "FLOW_CONNECTOR_CAPABILITY_MISSING"
    assert availability.unavailable_tools == ("order.get_status",)
    assert availability.unavailable_connector_capabilities == ("orders_read",)


def test_capability_risk_mismatch_is_never_promoted_to_available() -> None:
    flow = get_standard_flow("order_status")
    assert flow is not None
    availability = resolve_flow_availability(
        flow,
        capabilities={"order.get_status": _cap("order.get_status", "confirmed_write")},
        configured_owner_groups=frozenset(),
    )

    assert availability.status == "needs_human"
    assert availability.reason_code == "FLOW_CAPABILITY_RISK_MISMATCH"
    assert availability.unavailable_tools == ("order.get_status",)


def test_quality_intake_requires_real_case_write_and_a_configured_owner() -> None:
    flow = get_standard_flow("repair_quality_intake")
    assert flow is not None
    no_owner = resolve_flow_availability(
        flow,
        capabilities={"case.create": _cap("case.create", "confirmed_write")},
        configured_owner_groups=frozenset(),
    )
    ready = resolve_flow_availability(
        flow,
        capabilities={"case.create": _cap("case.create", "confirmed_write")},
        configured_owner_groups=frozenset({"quality"}),
    )

    assert no_owner.reason_code == "FLOW_OWNER_UNASSIGNED"
    assert ready.status == "available"
    assert ready.available_tools == ("case.create",)
    assert flow.confirmation_required is True


def test_invoice_application_stays_human_owned_without_an_invoice_write_tool() -> None:
    flow = get_standard_flow("invoice_application")
    assert flow is not None
    availability = resolve_flow_availability(
        flow,
        capabilities={
            "billing.get_invoice": _cap("billing.get_invoice", "read"),
            "case.create": _cap("case.create", "confirmed_write"),
        },
        configured_owner_groups=frozenset({"finance"}),
    )

    assert availability.status == "needs_human"
    assert availability.reason_code == "FLOW_EXTERNAL_WRITE_UNAVAILABLE"
    assert "billing.issue_invoice" not in flow.allowed_confirmed_write_tools
    tax_id = next(field for field in flow.optional_fields if field.name == "tax_id")
    assert tax_id.sensitive is True


def test_technical_escalation_never_claims_engineering_resolution() -> None:
    flow = get_standard_flow("technical_escalation")
    assert flow is not None
    assert flow.confirmation_required is True
    assert "不生成未核验" in flow.partial_completion_rule
