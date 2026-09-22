"""Business read tools: order status, shipment tracking, invoices (plan 3.2).

Read tools exist because live data must not enter RAG (ADR 0006): an order
status indexed on Tuesday answers wrongly on Wednesday, and the wrong answer
is cited with a hash. These adapters read from the tenant's `business_api`
connector - a plain REST surface whose configuration carries the base URL
and credential reference - so a tenant plugs in their order system once and
all three tools work.

`case.read` is deliberately NOT here: it reads the platform's own Case table
and needs a session-bound executor, which the registry wires separately.
"""

from datetime import UTC, datetime
from typing import Any

from platform_core.integrations.resilience import CircuitBreaker
from platform_core.integrations.sdk import ConnectorAdapter, ConnectorContext

# tool name -> (connector resource path, required parameter).
# One connector serves all three; the resource is the only difference.
READ_TOOL_RESOURCES: dict[str, tuple[str, str]] = {
    "order.get_status": ("orders", "order_id"),
    "shipment.track": ("shipments", "shipment_id"),
    "billing.get_invoice": ("invoices", "invoice_id"),
    # Component stock/lead time (huqiu research: pure real-time data — ADR 0006
    # forbids indexing it). Keyed by manufacturer part number.
    "inventory.check_stock": ("inventory", "part_number"),
}

# JSON Schema per tool, shared by the registry seeding and the contract tests.
READ_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "order.get_status": {
        "type": "object",
        "properties": {"order_id": {"type": "string", "minLength": 1}},
        "required": ["order_id"],
        "additionalProperties": False,
    },
    "shipment.track": {
        "type": "object",
        "properties": {"shipment_id": {"type": "string", "minLength": 1}},
        "required": ["shipment_id"],
        "additionalProperties": False,
    },
    "billing.get_invoice": {
        "type": "object",
        "properties": {"invoice_id": {"type": "string", "minLength": 1}},
        "required": ["invoice_id"],
        "additionalProperties": False,
    },
    "case.read": {
        "type": "object",
        "properties": {"case_ref": {"type": "string", "minLength": 1}},
        "required": ["case_ref"],
        "additionalProperties": False,
    },
    "inventory.check_stock": {
        "type": "object",
        "properties": {"part_number": {"type": "string", "minLength": 2}},
        "required": ["part_number"],
        "additionalProperties": False,
    },
}


class BusinessReadAdapter(ConnectorAdapter):
    """GET one record from the tenant's business API, canonically projected.

    The adapter is transport-only: the receipt it returns is the raw record
    minus credentials, and the Tool Gateway sanitizes and verifies it like
    any other execution. Unknown resource/tool combinations raise, because a
    silently wrong URL would read a different record and cite it.
    """

    provider = "business_api"
    capabilities = ("orders_read", "shipments_read", "invoices_read", "inventory_read")

    def __init__(self, context: ConnectorContext, breaker: CircuitBreaker | None = None) -> None:
        super().__init__(context, breaker)

    async def health_check(self) -> bool:
        result = await self.http_request("GET", self._base_url.rstrip("/") + "/health")
        return result.ok

    @property
    def _base_url(self) -> str:
        url = str(self.context.configuration.get("base_url") or "").rstrip("/")
        if not url:
            raise ValueError("business_api connector is missing base_url")
        return url

    async def fetch(self, resource: str, cursor: str | None = None) -> tuple[list[Any], str | None]:
        """Sync-surface conformance; read tools fetch single records."""
        params = {"cursor": cursor} if cursor else None
        result = await self.http_request(
            "GET", f"{self._base_url}/{resource.lstrip('/')}", params=params
        )
        if not result.ok:
            return [], None
        data = result.data or {}
        records = data.get("items") if isinstance(data, dict) else None
        return (records if isinstance(records, list) else [data]), None

    async def read_one(self, resource: str, record_id: str) -> dict[str, Any] | None:
        """The read-tool path: GET one record by id.

        Returns the record dict, or None when the provider says it does not
        exist. Transport failures raise via http_request's classified result
        and are translated by the executor below.
        """
        result = await self.http_request(
            "GET", f"{self._base_url}/{resource.lstrip('/')}/{record_id}"
        )
        if not result.ok:
            if result.error_code and result.error_code.startswith("CONNECTOR_REJECTED_404"):
                return None
            raise RuntimeError(result.error_code or "CONNECTOR_UNAVAILABLE")
        return result.data


class BusinessReadToolExecutor:
    """ToolExecutor over BusinessReadAdapter for the three read tools.

    Returned bare; the registry wraps it in ConnectorOutcomeExecutor so
    failures land in health/dead-letter bookkeeping like every connector.
    """

    def __init__(self, context: ConnectorContext) -> None:
        self._adapter = BusinessReadAdapter(context)

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
        # fetched_at rides on every receipt (huqiu research risk 1): a cached
        # progress or stock figure is an expired promise, so the answer the
        # model builds MUST be able to say when the provider stated it.
        #
        # ISO 8601 rather than the epoch integer this used to be, and the
        # reason is not readability. Turns are stored through
        # `evaluation.pii.redact_text`, which masks phone-shaped runs, so a
        # 10-digit epoch becomes `[PHONE]` - which makes the receipt invalid
        # JSON. `orchestrator._survives_redaction` then refuses to publish it,
        # so the card never reaches the customer. Measured, not inferred: with
        # this adapter the serialised payload failed to re-parse while the demo
        # adapter's ISO string was the only reason a card was ever published.
        # `case_create._iso` renders receipt times the same way, same reason.
        return {
            "found": True,
            "resource": resource,
            "record": record,
            "fetched_at": datetime.now(UTC).isoformat(),
        }

    async def verify_ownership(self, tool_name: str, record_id: str, proof: str) -> str | None:
        """Feature list 2.2/2.5: whose record is this, if the proof matches.

        The HTTP provider has no ownership endpoint in this deployment, so this
        returns None - which the caller must treat as "cannot prove", i.e. a
        refusal. A provider that can answer (the demo adapter does, from the
        contact phone on file) returns the owning account, and the visitor
        session is then bound to it. Returning a permissive default here would
        put somebody's order on a stranger's screen.
        """
        return None

    async def verify_postcondition(
        self, tool_name: str, parameters: dict[str, Any], output: dict[str, Any] | None
    ) -> bool | None:
        # A read's postcondition is "the response is the provider's":
        # found/not-found are both valid, verified outcomes. Ambiguity
        # comes only from transport failure, which surfaces as an
        # exception and is classified upstream.
        return isinstance(output, dict) and "found" in output
