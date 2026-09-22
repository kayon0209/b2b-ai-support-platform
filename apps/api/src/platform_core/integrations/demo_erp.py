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
#
# `account` exists for feature list 2.5: the read tools must be able to say
# whose data a record is, or an anonymous visitor on the support surface is
# served *somebody's* order by asking for its number. The account rides on the
# receipt, and the orchestrator refuses to publish a receipt whose account is
# not the one the visitor proved ownership of (see support_router.verify).

# The proof of ownership, deliberately OUTSIDE the records: a phone tail on a
# published receipt would leak the very credential the verification checks.
# `verify_ownership` is the provider's own answer to "does this proof match
# this record" - a real ERP would ask its system the same question.
_ORDERS: dict[str, dict[str, Any]] = {
    "SO-9001": {
        "order_id": "SO-9001",
        "status": "in_production",
        "account": "acme",
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
        "account": "other-co",
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
        # The shipment belongs to the account that ordered SO-9001, so the
        # ownership gate can cover tracking without a second verification.
        "account": "acme",
    },
}

# Last four digits of the contact phone on file - the weakest
# demonstration-grade proof, and labelled as such by the verify endpoint.
_CONTACT_TAILS: dict[str, str] = {"SO-9001": "8888", "SO-9002": "7777"}

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

    async def verify_ownership(self, tool_name: str, record_id: str, proof: str) -> str | None:
        """Feature list 2.2/2.5: whose record is this, if the proof matches.

        Returns the owning account, or None when the record is unknown, the
        proof is wrong, or the tool's records have no owner. None is also what
        a provider that cannot prove ownership returns, and the caller must
        treat it as a refusal - a silent yes here would put somebody's order on
        a stranger's screen.
        """
        if tool_name != "order.get_status":
            # Only orders carry an owner in the sample data; tracking and
            # invoices inherit the owner of the order they belong to.
            return None
        record = _ORDERS.get(record_id)
        if record is None or _CONTACT_TAILS.get(record_id) != proof.strip():
            return None
        return str(record.get("account") or "") or None
