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


class CacheEntry[T]:
    """TTL cache slot.

    Generic in the cached value so `get_account` can return
    `CrmAccountSummary | None` without a cast: the cache is shared by
    several lookup methods with different value types, and an `Any` slot
    erases the type at exactly the boundary where it matters.
    """

    __slots__ = ("value", "expires_at", "fetched_at")

    def __init__(self, value: T, ttl_seconds: int) -> None:
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
    # Annotated so a write-capable subclass can extend the tuple; without it
    # the literal narrows to a 3-tuple and the override is a type error.
    capabilities: tuple[str, ...] = ("read_account", "read_contact", "read_entitlements")

    def __init__(self, context: ConnectorContext, *, cache_ttl_seconds: int = 300) -> None:
        super().__init__(context)
        # Only account summaries are cached today; the entry is generic so a
        # second cached projection keeps its type instead of decaying to Any.
        self._cache: dict[str, CacheEntry[CrmAccountSummary]] = {}
        self._ttl = cache_ttl_seconds

    def _auth_headers(self) -> dict[str, str]:
        """Bearer header from the server-resolved credential.

        Credentials arrive on the context (resolved by the platform from
        `credential_ref`), never from a caller; this only shapes them for
        the wire.
        """
        return {"Authorization": f"Bearer {self.context.credentials.get('api_token', '')}"}

    def _account_url(self, external_ref: str) -> str:
        base = self.context.configuration.get("base_url", "")
        path_tpl = self.context.configuration.get("accounts_path", "/accounts/{id}")
        return f"{base}{path_tpl.format(id=external_ref)}"

    async def health_check(self) -> bool:
        base = self.context.configuration.get("base_url", "")
        if not base:
            return False
        result = await self.http_request("GET", f"{base}/health", max_retries=0)
        return result.ok

    async def fetch(self, resource: str, cursor: str | None = None) -> tuple[list[Any], str | None]:
        raise NotImplementedError("CRM pilot is lookup-based, not sync-based")

    async def get_account(self, external_ref: str) -> CrmAccountSummary | None:
        cached = self._cache.get(f"account:{external_ref}")
        if cached and cached.fresh():
            return cached.value

        result = await self.http_request(
            "GET", self._account_url(external_ref), headers=self._auth_headers()
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
            headers=self._auth_headers(),
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


class CrmWriteAdapter(CrmReadAdapter):
    """Write-capable CRM adapter (Tool Gateway tool `crm.update_account`).

    Extends the read adapter so the postcondition check can reuse the same
    canonical projection: after a PATCH we re-read the account and compare the
    fields we asked to change. Verifying against the PATCH *response* would
    only prove the CRM echoed our request, not that it persisted it.

    Capability gate: the tenant's connector must claim `update_account`; the
    registry refuses to build an executor otherwise, so connecting a CRM for
    lookups never silently authorizes a write.
    """

    provider = "crm"
    capabilities = (*CrmReadAdapter.capabilities, "update_account")

    # The fields a support agent may change through the gateway. Deliberately
    # a closed set: an open "patch any field" tool would let a prompt
    # injection rewrite billing state.
    WRITABLE_FIELDS = ("tier", "contract_status")

    async def update_account(
        self,
        account_ref: str,
        *,
        tier: str | None = None,
        contract_status: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if tier is not None:
            body["tier"] = tier
        if contract_status is not None:
            body["contract_status"] = contract_status
        if not account_ref or not body:
            return {"ok": False, "error_code": "TOOL_ARGS_INVALID"}

        result = await self.http_request(
            "PATCH",
            self._account_url(account_ref),
            headers=self._auth_headers(),
            json_body=body,
        )
        if not result.ok:
            return {
                "ok": False,
                "error_code": result.error_code,
                "ambiguous": result.ambiguous,
            }

        # The read cache would otherwise serve the pre-update projection and
        # make the postcondition check report a false failure.
        self._cache.pop(f"account:{account_ref}", None)
        return {"ok": True, "account_ref": account_ref, "updated": body}

    # --- ToolExecutor protocol (registered as crm.update_account) ---

    async def execute(
        self, tool_name: str, parameters: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        if tool_name != "crm.update_account":
            return None
        unknown = set(parameters) - {"account_ref", *self.WRITABLE_FIELDS}
        if unknown:
            return {
                "ok": False,
                "error_code": "TOOL_ARGS_INVALID",
                "unknown": sorted(unknown),
            }
        return await self.update_account(
            str(parameters.get("account_ref", "")),
            tier=parameters.get("tier"),
            contract_status=parameters.get("contract_status"),
        )

    async def verify_postcondition(
        self, tool_name: str, parameters: dict[str, Any], output: dict[str, Any] | None
    ) -> bool | None:
        """Confirm the change landed by re-reading the account.

        None when the outcome cannot be determined (no output, or the read
        that would confirm it is unavailable): the gateway records UNKNOWN
        rather than pretending success or failure.
        """
        if not output:
            return None
        if output.get("ok") is not True:
            # An ambiguous transport outcome means the write may or may not
            # have landed: "cannot verify" is honest, "failed" is not.
            return None if output.get("ambiguous") else False
        account_ref = output.get("account_ref")
        if not account_ref:
            return None

        summary = await self.get_account(str(account_ref))
        if summary is None:
            return None

        updated = output.get("updated") or {}
        for field, expected in updated.items():
            actual = summary.tier if field == "tier" else summary.contract_status
            if actual != expected:
                return False
        return True
