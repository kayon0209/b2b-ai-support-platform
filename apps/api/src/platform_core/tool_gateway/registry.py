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

from platform_core.identity.tenant_context import TenantContext
from platform_core.integrations import dead_letter
from platform_core.integrations import health as connector_health
from platform_core.integrations.credentials import resolve_credentials
from platform_core.integrations.models import Connector, ConnectorStatus
from platform_core.integrations.sdk import AUTH_FAILURE_CODES, ConnectorContext
from platform_core.tool_gateway.gateway import ToolExecutor

# A credential reference -> credentials mapping. Injected rather than read
# here, so this module never touches a secret itself.
CredentialResolver = Callable[[str | None], dict[str, str]]

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


class ConnectorOutcomeExecutor:
    """Wraps a `ToolExecutor` so a failed connector outcome is recorded.

    Adapters report failure by *returning* it in a dict rather than raising,
    so nothing downstream distinguishes "the adapter declined" from "the
    adapter did the work". Two consequences needed handling, and both are
    invisible at the call site:

    1. A rejected credential (`CONNECTOR_AUTH_EXPIRED`) parks the connector in
       `NEEDS_REAUTH`. Previously the connector stayed `active` and every
       subsequent call failed the same way with no state anywhere saying why.
    2. Any other failure becomes a dead-letter record. Retry-exhausted and
       transport-ambiguous outcomes are exactly the ones a human must look at,
       and they previously left nothing behind but a failed `ToolExecution`
       row that nothing listed.

    The wrapper is applied at build time - it needs the `Connector` row, which
    only the resolver has - and it delegates `verify_postcondition` unchanged,
    so the gateway still decides the execution's final status.

    It does not swallow or rewrite the adapter's output: the caller sees byte
    for byte what the adapter returned.
    """

    def __init__(
        self,
        inner: ToolExecutor,
        *,
        session: AsyncSession,
        connector: Connector,
        ctx: TenantContext | None,
        trace_id: str | None = None,
    ) -> None:
        self._inner = inner
        self._session = session
        self._connector = connector
        self._ctx = ctx
        self._trace_id = trace_id

    def _context(self) -> TenantContext:
        return self._ctx or TenantContext(
            tenant_id=self._connector.tenant_id, actor_id=None, actor_kind="service"
        )

    async def execute(
        self, tool_name: str, parameters: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        output = await self._inner.execute(tool_name, parameters, idempotency_key)
        if not isinstance(output, dict) or output.get("ok") is not False:
            return output

        error_code = str(output.get("error_code") or "")
        ambiguous = bool(output.get("ambiguous"))
        ctx = self._context()

        if error_code in AUTH_FAILURE_CODES:
            await connector_health.record_auth_failure(
                self._session,
                self._connector,
                error_code=error_code,
                ctx=ctx,
                trace_id=self._trace_id,
            )
            return output

        if dead_letter.should_record(error_code=error_code, ambiguous=ambiguous):
            await dead_letter.record(
                self._session,
                tenant_id=self._connector.tenant_id,
                connector_id=self._connector.id,
                resource_type="tool_execution",
                tool_name=tool_name,
                parameters=parameters,
                error_code=error_code or "CONNECTOR_UNKNOWN",
                error_detail=str(output.get("detail") or ""),
                attempts=int(output.get("attempts") or 0),
                ambiguous=ambiguous,
            )
        return output

    async def verify_postcondition(
        self, tool_name: str, parameters: dict[str, Any], output: dict[str, Any] | None
    ) -> bool | None:
        return await self._inner.verify_postcondition(tool_name, parameters, output)


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
        credential_resolver: CredentialResolver | None = None,
        ctx: TenantContext | None = None,
        trace_id: str | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._factories = factories if factories is not None else default_factories()
        # Injected so the registry itself never reads a secret. The default
        # resolves the pilot `env://` scheme; an unsupported reference yields
        # no credentials, and the adapter then fails closed at call time.
        self._credential_resolver = credential_resolver or resolve_credentials
        # Audit context for a connector status change triggered by a rejected
        # credential. Optional so a caller that only resolves executors (a
        # test, a script) does not have to fabricate an actor; the wrapper
        # then attributes the change to the service.
        self._ctx = ctx
        self._trace_id = trace_id
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
            # Resolved through the injected resolver, so this module never
            # reads a secret itself. Without this every adapter received `{}`
            # and no external call could authenticate.
            credentials=self._credential_resolver(connector.credential_ref),
            configuration=dict(connector.configuration or {}),
        )
        try:
            executor = factory.build(context)
        except Exception:
            # A factory that cannot build from this configuration degrades
            # to "no executor" rather than failing the whole request.
            return None
        # Wrapped here rather than inside each adapter: the connector row and
        # the session are only available at this seam, and a per-adapter
        # implementation would be one more thing a new adapter can forget.
        executor = ConnectorOutcomeExecutor(
            executor,
            session=self._session,
            connector=connector,
            ctx=self._ctx,
            trace_id=self._trace_id,
        )
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


def build_adapter(
    connector: Connector,
    *,
    credential_resolver: CredentialResolver | None = None,
    factories: dict[str, AdapterFactory] | None = None,
) -> Any | None:
    """Build the adapter for a connector's provider, or None if none ships.

    Returns None rather than raising for both "this deployment has no adapter
    for the provider" and "the factory refused this configuration": neither is
    an outage, and callers need to tell that apart from one.

    Unlike `_build`, no write capability is required - health and sync are
    read-side questions, and a read-only connector still has to be
    diagnosable and syncable.
    """
    resolver = credential_resolver or resolve_credentials
    registry = factories if factories is not None else default_factories()
    factory = registry.get(connector.provider)
    if factory is None:
        return None

    context = ConnectorContext(
        tenant_id=str(connector.tenant_id),
        connector_id=str(connector.id),
        credentials=resolver(connector.credential_ref),
        configuration=dict(connector.configuration or {}),
    )
    try:
        return factory.build(context)
    except Exception:
        return None


async def probe_connector(
    connector: Connector,
    *,
    credential_resolver: CredentialResolver | None = None,
    factories: dict[str, AdapterFactory] | None = None,
) -> bool | None:
    """Call the adapter's `health_check` for a connector.

    Returns the reachability result, or `None` when this deployment ships no
    adapter for the connector's provider. `None` is not `False`: "we have no
    way to probe this provider" and "the provider did not answer" call for
    different operator responses, and collapsing them would report a
    capability gap as an outage.
    """
    adapter = build_adapter(connector, credential_resolver=credential_resolver, factories=factories)
    if adapter is None:
        return None

    probe = getattr(adapter, "health_check", None)
    if probe is None:  # pragma: no cover - factories only build ConnectorAdapters
        return None
    try:
        result = await probe()
    except Exception:
        return False
    return bool(result)


async def resolve_executors(
    session: AsyncSession,
    *,
    tenant_id: Any,
    tool_names: list[str],
    factories: dict[str, AdapterFactory] | None = None,
    credential_resolver: CredentialResolver | None = None,
    ctx: TenantContext | None = None,
    trace_id: str | None = None,
) -> dict[str, ToolExecutor]:
    """Convenience entry point for routers.

    Returns only the executors this tenant can legitimately use; callers
    pass the result straight to `ToolGateway`.
    """
    resolver = ConnectorExecutorResolver(
        session,
        tenant_id=tenant_id,
        factories=factories,
        credential_resolver=credential_resolver,
        ctx=ctx,
        trace_id=trace_id,
    )
    return await resolver.executors_for(tool_names)


__all__ = [
    "AdapterFactory",
    "ConnectorExecutorResolver",
    "ConnectorOutcomeExecutor",
    "TOOL_CAPABILITY",
    "TOOL_PROVIDER",
    "default_factories",
    "build_adapter",
    "probe_connector",
    "resolve_executors",
]
