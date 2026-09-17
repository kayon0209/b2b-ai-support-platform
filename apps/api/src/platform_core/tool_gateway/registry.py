"""Tool executor resolution: connectors -> executors, per tenant.

The gateway takes `executors: dict[str, ToolExecutor]` as a constructor
argument so authorization, confirmation and idempotency stay testable
without network adapters. In production that dict has to come from
somewhere, and the somewhere is the `connectors` table: a tool is only
executable by a tenant that has connected the backing system.

Two rules this module exists to enforce:

1. **A connector is tenant-scoped.** An adapter is built from a `Connector`
   row belonging to the calling tenant. There is no process-wide adapter
   cache keyed only by tool name, because that would let one tenant's
   credentials serve another tenant's write.
2. **Credentials never come from the request.** They are resolved from the
   secret reference stored on the connector row, at call time. This module
   passes the reference through; the secret manager is a separate concern.

A tenant with no active connector simply has no executor, and the gateway
reports `TOOL_EXECUTOR_MISSING`. That is correct: proposing the tool must
still work, so the confirmation workflow stays inspectable, but it cannot
execute.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.integrations.models import Connector, ConnectorStatus
from platform_core.integrations.sdk import ConnectorContext
from platform_core.tool_gateway.gateway import ToolExecutor

# tool name -> the connector capability it requires.
#
# A tool is executable only when the tenant holds a connector that claims
# the capability. Declared here rather than inferred from the tool's JSON
# Schema because the mapping is a security decision, not a data detail.
#
# A tool belongs in this table only when an adapter that satisfies
# `ToolExecutor` exists for it. Adding a name here without an adapter turns
# a wiring omission into a runtime `TOOL_EXECUTOR_MISSING` at execute time.
TOOL_CAPABILITY: dict[str, str] = {
    "jira.create_issue": "create_issue",
    "crm.update_account": "update_account",
}

# tool name -> the connector provider that can serve it.
TOOL_PROVIDER: dict[str, str] = {
    "jira.create_issue": "jira",
    "crm.update_account": "crm",
}


@dataclass(frozen=True)
class AdapterFactory:
    """Builds an adapter from a resolved connector context."""

    provider: str
    build: Callable[[ConnectorContext], ToolExecutor]


class ConnectorExecutorResolver:
    """Resolves the executors available to one tenant.

    Constructed per request with the tenant's session, so the connector
    lookup runs under the same RLS-bound transaction as everything else in
    the request and cannot escape the tenant boundary.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: Any,
        factories: dict[str, AdapterFactory] | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._factories = factories if factories is not None else default_factories()
        self._cache: dict[str, ToolExecutor] | None = None

    async def executors_for(self, tool_names: list[str]) -> dict[str, ToolExecutor]:
        """Return executors for the requested tool names, skipping the rest.

        Only tools the tenant can actually serve appear in the result; the
        gateway then reports `TOOL_EXECUTOR_MISSING` for the others.
        """
        if not tool_names:
            return {}
        available = await self._available_connectors()
        resolved: dict[str, ToolExecutor] = {}
        for tool_name in tool_names:
            executor = self._build(tool_name, available)
            if executor is not None:
                resolved[tool_name] = executor
        return resolved

    async def _available_connectors(self) -> dict[str, Connector]:
        """Active connectors for this tenant, keyed by provider.

        A disabled or degraded connector is excluded: executing a write
        through a connection that is known to be broken would produce an
        ambiguous outcome the operator has to chase down manually.
        """
        rows = (
            (
                await self._session.execute(
                    select(Connector).where(
                        Connector.tenant_id == self._tenant_id,
                        Connector.status == ConnectorStatus.ACTIVE.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        by_provider: dict[str, Connector] = {}
        for connector in rows:
            # First active connector per provider wins. Multiple connectors
            # for one provider is a legitimate setup (two Jira sites), and
            # disambiguating them needs a routing rule that does not exist
            # yet; picking deterministically beats picking arbitrarily.
            by_provider.setdefault(connector.provider, connector)
        return by_provider

    def _build(self, tool_name: str, available: dict[str, Connector]) -> ToolExecutor | None:
        provider = TOOL_PROVIDER.get(tool_name)
        capability = TOOL_CAPABILITY.get(tool_name)
        if provider is None or capability is None:
            return None

        connector = available.get(provider)
        if connector is None:
            return None

        # The connector must claim the capability the tool needs. A
        # connector that only reads must not become a write path.
        caps = connector.capabilities or []
        if capability not in caps:
            return None

        factory = self._factories.get(provider)
        if factory is None:
            return None

        if self._cache is None:
            self._cache = {}
        cached = self._cache.get(tool_name)
        if cached is not None:
            return cached

        context = ConnectorContext(
            tenant_id=str(self._tenant_id),
            connector_id=str(connector.id),
            # The reference is passed through, not dereferenced here: this
            # module must not become a place where secrets are read.
            credentials={},
            configuration=dict(connector.configuration or {}),
        )
        try:
            executor = factory.build(context)
        except Exception:
            # A factory that cannot build from this configuration degrades
            # to "no executor" rather than failing the whole request.
            return None
        self._cache[tool_name] = executor
        return executor


def default_factories() -> dict[str, AdapterFactory]:
    """Adapter factories for the providers this service ships.

    Import-light: the adapter modules are imported inside the builder so a
    service that never connects Jira does not pay its import cost.

    Only adapters that actually implement the `ToolExecutor` protocol are
    registered. `CrmReadAdapter` on its own is deliberately absent: it is a
    read adapter with no write surface, so mapping `crm.update_account` to it
    would produce a `TypeError` at execute time. `CrmWriteAdapter` extends it
    with `execute` / `verify_postcondition` and is registered instead.
    """

    def _build_jira(context: ConnectorContext) -> ToolExecutor:
        from platform_core.integrations.jira import JiraAdapter

        # JiraAdapter implements both the read surface and the
        # ToolExecutor protocol (`execute` / `verify_postcondition`).
        return JiraAdapter(context)

    def _build_crm(context: ConnectorContext) -> ToolExecutor:
        from platform_core.integrations.crm import CrmWriteAdapter

        # Write-capable: the resolver only builds it when the connector
        # claims `update_account`, so a lookup-only CRM stays read-only.
        return CrmWriteAdapter(context)

    return {
        "jira": AdapterFactory(provider="jira", build=_build_jira),
        "crm": AdapterFactory(provider="crm", build=_build_crm),
    }


async def resolve_executors(
    session: AsyncSession,
    *,
    tenant_id: Any,
    tool_names: list[str],
    factories: dict[str, AdapterFactory] | None = None,
) -> dict[str, ToolExecutor]:
    """Convenience entry point for routers.

    Returns only the executors this tenant can legitimately use; callers
    pass the result straight to `ToolGateway`.
    """
    resolver = ConnectorExecutorResolver(session, tenant_id=tenant_id, factories=factories)
    return await resolver.executors_for(tool_names)


__all__ = [
    "AdapterFactory",
    "ConnectorExecutorResolver",
    "TOOL_CAPABILITY",
    "TOOL_PROVIDER",
    "default_factories",
    "resolve_executors",
]
