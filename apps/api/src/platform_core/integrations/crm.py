"""CRM read adapter (ticket 28, docs/integrations.md CRM section).

Pilot capabilities: lookup account/contact, entitlement summary, link
Chatwoot contact to enterprise account. No bulk bidirectional sync.

Canonical projections (CrmAccountSummary etc.) are what core modules see;
provider payloads stop at this adapter boundary. Cached fields carry TTL
and source timestamps; live-sensitive facts state their freshness.
"""

import time
from dataclasses import dataclass
from typing import Any

from platform_core.integrations.sdk import (
    ConnectorAdapter,
    ConnectorContext,
)


@dataclass(frozen=True)
class CrmAccountSummary:
    """Canonical CRM account projection. Only support-relevant fields."""

    external_ref: str
    name: str
    tier: str | None
    contract_status: str | None
    entitlements: tuple[str, ...]
    fetched_at: int


@dataclass(frozen=True)
class CrmContactSummary:
    external_ref: str
    name: str
    email_domain: str | None  # never the full email (PII minimization)
    account_external_ref: str | None
    fetched_at: int


class CacheEntry:
    __slots__ = ("value", "expires_at", "fetched_at")

    def __init__(self, value: Any, ttl_seconds: int) -> None:
        self.value = value
        self.fetched_at = int(time.time())
        self.expires_at = self.fetched_at + ttl_seconds

    def fresh(self) -> bool:
        return time.time() < self.expires_at


class CrmReadAdapter(ConnectorAdapter):
    """Generic REST CRM read adapter.

    configuration keys:
      base_url: CRM API root
      accounts_path: e.g. /api/v2/accounts/{id}
      contacts_path: e.g. /api/v2/contacts/{id}
    credentials: {"api_token": ...} injected server-side.
    """

    provider = "crm"
    capabilities = ("read_account", "read_contact", "read_entitlements")

    def __init__(self, context: ConnectorContext, *, cache_ttl_seconds: int = 300) -> None:
        super().__init__(context)
        self._cache: dict[str, CacheEntry] = {}
        self._ttl = cache_ttl_seconds

    async def health_check(self) -> bool:
        base = self.context.configuration.get("base_url", "")
        if not base:
            return False
        result = await self.http_request("GET", f"{base}/health", max_retries=0)
        return result.ok

    async def fetch(
        self, resource: str, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        raise NotImplementedError("CRM pilot is lookup-based, not sync-based")

    async def get_account(self, external_ref: str) -> CrmAccountSummary | None:
        cached = self._cache.get(f"account:{external_ref}")
        if cached and cached.fresh():
            return cached.value  # type: ignore[return-value]

        base = self.context.configuration.get("base_url", "")
        path_tpl = self.context.configuration.get("accounts_path", "/accounts/{id}")
        result = await self.http_request(
            "GET",
            f"{base}{path_tpl.format(id=external_ref)}",
            headers={"Authorization": f"Bearer {self.context.credentials.get('api_token', '')}"},
        )
        if not result.ok or not result.data:
            return None

        summary = self._project_account(external_ref, result.data)
        self._cache[f"account:{external_ref}"] = CacheEntry(summary, self._ttl)
        return summary

    async def get_contact(self, external_ref: str) -> CrmContactSummary | None:
        base = self.context.configuration.get("base_url", "")
        path_tpl = self.context.configuration.get("contacts_path", "/contacts/{id}")
        result = await self.http_request(
            "GET",
            f"{base}{path_tpl.format(id=external_ref)}",
            headers={"Authorization": f"Bearer {self.context.credentials.get('api_token', '')}"},
        )
        if not result.ok or not result.data:
            return None
        return self._project_contact(external_ref, result.data)

    # --- Canonical projection: provider fields -> support fields ---

    def _project_account(self, external_ref: str, data: dict[str, Any]) -> CrmAccountSummary:
        entitlements = data.get("entitlements") or data.get("plans") or []
        if isinstance(entitlements, list):
            names = tuple(
                str(e.get("name")) if isinstance(e, dict) else str(e) for e in entitlements
            )
        else:
            names = ()
        return CrmAccountSummary(
            external_ref=external_ref,
            name=str(data.get("name") or data.get("account_name") or ""),
            tier=data.get("tier") or data.get("plan_tier"),
            contract_status=data.get("contract_status") or data.get("status"),
            entitlements=names,
            fetched_at=int(time.time()),
        )

    def _project_contact(self, external_ref: str, data: dict[str, Any]) -> CrmContactSummary:
        email = data.get("email") or ""
        domain = email.split("@", 1)[1] if "@" in email else None
        return CrmContactSummary(
            external_ref=external_ref,
            name=str(data.get("name") or ""),
            email_domain=domain,  # full email deliberately dropped
            account_external_ref=(str(data["account_id"]) if data.get("account_id") else None),
            fetched_at=int(time.time()),
        )
