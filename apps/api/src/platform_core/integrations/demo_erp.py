"""A local stand-in for the tenant's ERP, for a deployment with no real one.

This project has no external system to call, which left the read path - the
part that answers "where is my order" - exercised only by tests with fake
providers. This adapter is a real `business_api` provider backed by sample
data, so the whole path runs: question -> tool -> receipt -> data card.

**The data is invented, and it says so.** Every record carries
`source: "demo"` precisely so a figure from here can never be mistaken for one
from a real system. That is the same rule as the public price table: a number
is only as good as its provenance, and a plausible number with no provenance is
the kind that gets believed.

It is selected with `business_api_adapter=demo`; the default stays the real
HTTP adapter. Nothing else in the platform knows the difference, which is the
point - the seam is the provider, not the call sites.
"""

from __future__ import annotations

from typing import Any

from platform_core.integrations.business_read import READ_TOOL_RESOURCES

# Deliberately small and obviously fictional: a handful of orders is enough to
# exercise the timeline card, and a realistic-looking dataset would invite
# someone to mistake it for production data.
_ORDERS: dict[str, dict[str, Any]] = {
    "SO-9001": {
        "order_id": "SO-9001",
        "status": "in_production",
        "nodes": [
            {"label": "下单", "status": "done", "at": "2026-09-14T10:00:00Z"},
            {"label": "工程确认", "status": "done", "at": "2026-09-15T09:20:00Z"},
            {"label": "生产", "status": "active", "at": "2026-09-17T08:00:00Z"},
            {"label": "出货", "status": "pending", "at": None},
        ],
        "eta": "2026-09-26T00:00:00Z",
        "quantity": 500,
    },
    "SO-9002": {
        "order_id": "SO-9002",
        "status": "shipped",
        "nodes": [
            {"label": "下单", "status": "done", "at": "2026-09-08T11:00:00Z"},
            {"label": "工程确认", "status": "done", "at": "2026-09-09T10:00:00Z"},
            {"label": "生产", "status": "done", "at": "2026-09-12T08:00:00Z"},
            {"label": "出货", "status": "done", "at": "2026-09-16T15:30:00Z"},
        ],
        "eta": "2026-09-16T00:00:00Z",
        "quantity": 100,
    },
}

_SHIPMENTS: dict[str, dict[str, Any]] = {
    "SH-7001": {
        "shipment_id": "SH-7001",
        "carrier": "SF Express",
        "tracking_no": "SF1234567890",
        "status": "in_transit",
    },
}

_RESOURCES: dict[str, dict[str, dict[str, Any]]] = {
    "orders": _ORDERS,
    "shipments": _SHIPMENTS,
    "invoices": {},
    "inventory": {},
}


class DemoBusinessAdapter:
    """Reads one record from the sample dataset.

    The shape returned is the same the real adapter returns, including
    `fetched_at`, because the data card's freshness label depends on it - a
    demo that omitted it would hide the case where it is missing.
    """

    provider = "business_api"
    capabilities = ("orders_read", "shipments_read", "invoices_read", "inventory_read")

    def __init__(self, context: object) -> None:
        self.context = context

    async def health_check(self) -> bool:
        return True

    async def fetch(
        self, resource: str, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        return list(_RESOURCES.get(resource, {}).values()), None

    async def read_one(self, resource: str, record_id: str) -> dict[str, Any] | None:
        record = _RESOURCES.get(resource, {}).get(record_id)
        if record is None:
            return None
        return {
            **record,
            "found": True,
            "resource": resource,
            # Provenance, so this can never be read as a real system's answer.
            "source": "demo",
            "fetched_at": "2026-09-21T00:00:00Z",
        }


class DemoBusinessToolExecutor:
    """The ToolExecutor surface over `DemoBusinessAdapter`.

    The registry expects an executor, not an adapter - the real provider
    returns `BusinessReadToolExecutor`, so the demo has to look the same or the
    gateway reports TOOL_EXECUTION_FAILED for a tool the catalog advertises.
    The postcondition is verified honestly: a receipt that is present and
    carries a fetch time is one the platform may repeat.
    """

    def __init__(self, context: object) -> None:
        self._adapter = DemoBusinessAdapter(context)

    async def execute(
        self, tool_name: str, parameters: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        resource, param = READ_TOOL_RESOURCES[tool_name]
        record_id = str(parameters.get(param) or "").strip()
        if not record_id:
            raise ValueError(f"{tool_name} requires {param}")
        record = await self._adapter.read_one(resource, record_id)
        if record is None:
            return {"found": False, "resource": resource, "id": record_id}
        return record

    async def verify_postcondition(
        self, tool_name: str, parameters: dict[str, Any], output: object
    ) -> bool:
        if not isinstance(output, dict) or not output.get("found"):
            # An honest no: the record is not in the sample data.
            return False
        return bool(output.get("fetched_at"))
