"""Process-local synthetic CRM opportunity writer for local/test Tool Gateway demos.

This adapter is deliberately not an HTTP client and does not read or write a
CRM database. It keeps a small in-memory receipt store so the existing Tool
Gateway can exercise confirmation, idempotency and readback verification
without claiming that an external opportunity was created. Its state is
synthetic and volatile by design.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from typing import Any

from platform_core.config import get_settings
from platform_core.integrations.sdk import ConnectorContext

DEMO_CRM_ACCOUNT_REF = "acme"
DEMO_CRM_PRODUCTS = frozenset({"PCB-DEMO-100", "PCBA-DEMO-200"})
DEMO_CRM_SOURCE_VERSION = "demo-crm-fixture-v1"

_LOCK = threading.RLock()
_RECORDS: dict[tuple[str, str], dict[str, Any]] = {}
_IDEMPOTENCY: dict[tuple[str, str], tuple[str, str]] = {}


class DemoCrmOpportunityExecutor:
    """Synthetic `crm.create_opportunity` ToolExecutor, gated to local/test."""

    provider = "demo_crm"
    capabilities = ("opportunity_create",)

    def __init__(self, context: ConnectorContext) -> None:
        settings = get_settings()
        if (
            settings.environment not in ("local", "test")
            or settings.business_api_adapter.strip().lower() != "demo"
            or context.configuration.get("mode") != "synthetic"
        ):
            raise RuntimeError("DEMO_CRM_OPPORTUNITY_DISABLED")
        self.context = context

    async def health_check(self) -> bool:
        return True

    async def fetch(self, resource: str, cursor: str | None = None) -> tuple[list[Any], str | None]:
        """The mock has no sync API; return no records rather than fake a sync."""
        del resource, cursor
        return [], None

    async def execute(
        self, tool_name: str, parameters: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        if tool_name != "crm.create_opportunity":
            return None

        account_ref = str(parameters.get("account_ref", ""))
        product_ref = str(parameters.get("product_ref", ""))
        if account_ref != DEMO_CRM_ACCOUNT_REF or product_ref not in DEMO_CRM_PRODUCTS:
            return {"ok": False, "error_code": "BUSINESS_OWNERSHIP_MISMATCH"}

        tenant_id = self.context.tenant_id
        operation_key = (tenant_id, idempotency_key)
        request_digest = hashlib.sha256(
            json.dumps(
                {"account_ref": account_ref, "product_ref": product_ref},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with _LOCK:
            previous = _IDEMPOTENCY.get(operation_key)
            if previous is not None:
                previous_digest, previous_ref = previous
                if previous_digest != request_digest:
                    return {"ok": False, "error_code": "IDEMPOTENCY_CONFLICT"}
                record = _RECORDS.get((tenant_id, previous_ref))
                if record is None:
                    return {"ok": False, "error_code": "DEMO_CRM_READBACK_UNAVAILABLE"}
                return _receipt(record)

            opportunity_ref = (
                "DEMO-OPP-"
                + uuid.uuid5(uuid.NAMESPACE_URL, f"{tenant_id}:{idempotency_key}").hex[:12].upper()
            )
            record = {
                "opportunity_ref": opportunity_ref,
                "account_ref": account_ref,
                "product_ref": product_ref,
                "stage": "qualified",
                "source": "demo",
                "source_version": DEMO_CRM_SOURCE_VERSION,
                "synthetic": True,
                "customer_contacted": False,
            }
            _RECORDS[(tenant_id, opportunity_ref)] = record
            _IDEMPOTENCY[operation_key] = (request_digest, opportunity_ref)
        return _receipt(record)

    async def verify_postcondition(
        self,
        tool_name: str,
        parameters: dict[str, Any],
        output: dict[str, Any] | None,
    ) -> bool | None:
        """Read the simulated CRM record back before the Gateway can succeed."""
        if tool_name != "crm.create_opportunity":
            return None
        if not output:
            return None
        if output.get("ok") is not True:
            return False
        opportunity_ref = output.get("opportunity_ref")
        if not isinstance(opportunity_ref, str):
            return None
        with _LOCK:
            record = _RECORDS.get((self.context.tenant_id, opportunity_ref))
            if record is None:
                return None
            return (
                record.get("account_ref") == parameters.get("account_ref")
                and record.get("product_ref") == parameters.get("product_ref")
                and record.get("source") == "demo"
                and record.get("synthetic") is True
                and output == _receipt(record)
            )


def _receipt(record: dict[str, Any]) -> dict[str, Any]:
    """Small allowlisted Tool Gateway receipt; never includes a quote or PII."""
    return {"ok": True, **record}


def _reset_demo_crm_store_for_tests() -> None:
    with _LOCK:
        _RECORDS.clear()
        _IDEMPOTENCY.clear()


__all__ = [
    "DEMO_CRM_ACCOUNT_REF",
    "DEMO_CRM_PRODUCTS",
    "DEMO_CRM_SOURCE_VERSION",
    "DemoCrmOpportunityExecutor",
]
