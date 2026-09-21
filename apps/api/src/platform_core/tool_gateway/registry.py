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
from platform_core.integrations.business_read import READ_TOOL_SCHEMAS
from platform_core.integrations.credentials import resolve_credentials
from platform_core.integrations.models import Connector, ConnectorStatus
from platform_core.integrations.sdk import AUTH_FAILURE_CODES, ConnectorContext
from platform_core.tool_gateway.gateway import ToolExecutor
from platform_core.tool_gateway.models import ToolRisk
from platform_policy import Action

# Risk class -> the policy action a caller must hold to propose a tool of that
# class. Defined once, here, because two callers need it: the HTTP proposal
# endpoint and the agent's own write path. Two copies would let one of them
# become a way around the other.
RISK_ACTION: dict[str, str] = {
    ToolRisk.READ.value: Action.TOOL_READ.value,
    ToolRisk.LOW_WRITE.value: Action.TOOL_WRITE_LOW.value,
    ToolRisk.CONFIRMED_WRITE.value: Action.TOOL_WRITE_CONFIRMED.value,
    ToolRisk.HUMAN_APPROVAL.value: Action.TOOL_HUMAN_APPROVAL.value,
}

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
    # Linear is the other half of Phase 3's "Jira or Linear" priority. A tenant
    # on Linear had no ticket path at all before this entry existed.
    "linear.create_issue": "create_issue",
    "im.send_notification": "send_notification",
    # Read tools (iteration plan 3.2): served by the tenant's business_api
    # connector. case.read is deliberately absent - it reads the platform's
    # own Case table and never touches a connector.
    "order.get_status": "orders_read",
    "shipment.track": "shipments_read",
    "billing.get_invoice": "invoices_read",
    "inventory.check_stock": "inventory_read",
}

# Tools the platform serves from its own tables rather than through a
# connector, and which therefore have no provider to resolve.
#
# They need their own path because `_build` requires a connector for every
# other tool: `case.read` is in the catalog, the orchestrator can run it
# (that path injected the executor by hand), and yet
# `POST /v1/tool-proposals/{id}/execute` answered `TOOL_EXECUTOR_MISSING` for
# it - a tool the catalog advertises that one surface can run and another
# cannot. The executors are session-bound, so they read and write under the
# caller's RLS-bound transaction and another tenant's row is invisible rather
# than filtered afterwards.
PLATFORM_TOOLS: frozenset[str] = frozenset({"case.read", "case.eq_confirm", "case.create"})

# Arguments a write tool requires that the customer never supplies, and the
# connector-configuration key each one is read from.
#
# "Create a Jira issue" needs a project key. The customer does not know it and
# must not be asked for it: it is the tenant's configuration, not the
# conversation's content. Reading it from the connector means it is set once by
# whoever connected the system, and an unset key produces a handoff rather than
# a guessed key - a write that lands in the wrong project is worse than a write
# that does not happen.
WRITE_ARG_DEFAULTS: dict[str, tuple[tuple[str, str], ...]] = {
    "jira.create_issue": (("project", "default_project"),),
    "linear.create_issue": (("team", "default_team"),),
    "im.send_notification": (("channel", "default_channel"),),
}

# tool name -> the connectors that can serve it, in preference order.
#
# A tuple rather than one provider because a tool's *meaning* is independent of
# which vendor implements it: `im.send_notification` is "notify the on-call
# channel", and a tenant on Feishu must be able to call it. Mapping it to
# `im_webhook` alone would leave the Feishu and Teams adapters registered but
# unreachable - dead code, which is the defect this repository keeps finding.
# The first provider with an active connector that claims the capability wins.
TOOL_PROVIDERS: dict[str, tuple[str, ...]] = {
    "jira.create_issue": ("jira",),
    "crm.update_account": ("crm",),
    "linear.create_issue": ("linear",),
    "im.send_notification": ("im_webhook", "feishu", "teams"),
    "order.get_status": ("business_api",),
    "shipment.track": ("business_api",),
    "billing.get_invoice": ("business_api",),
    "inventory.check_stock": ("business_api",),
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

    async def default_write_arguments(self, tool_name: str) -> dict[str, str] | None:
        """Configuration-supplied arguments for a write tool.

        Returns `None` when the tool needs a default that no active connector
        supplies. `None` is deliberately not an empty dict: a caller must hand
        off rather than propose a write with a missing required argument. The
        gateway would reject it as `TOOL_ARGS_INVALID`, but only after the
        proposal row existed, leaving an operator to explain a proposal that
        could never have run.
        """
        wanted = WRITE_ARG_DEFAULTS.get(tool_name)
        if not wanted:
            return {}
        resolved = self.resolve_connector(tool_name, await self._available_connectors())
        if resolved is None:
            return None
        configuration = dict(resolved[1].configuration or {})
        values: dict[str, str] = {}
        for argument, key in wanted:
            value = configuration.get(key)
            if not isinstance(value, str) or not value.strip():
                return None
            values[argument] = value
        return values

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

    @staticmethod
    def resolve_connector(
        tool_name: str, available: dict[str, Connector]
    ) -> tuple[str, Connector] | None:
        """The (provider, connector) that can serve a tool, or None.

        Extracted from `_build` because the answer is needed twice: to build
        an executor, and to read the connector's own configuration for the
        arguments a customer never supplies (see `default_write_arguments`).
        One implementation means those two can never disagree about which
        connector a tool belongs to.
        """
        providers = TOOL_PROVIDERS.get(tool_name)
        capability = TOOL_CAPABILITY.get(tool_name)
        if not providers or capability is None:
            return None

        for candidate in providers:
            found = available.get(candidate)
            if found is None:
                continue
            # The connector must claim the capability the tool needs. A
            # connector that only reads must not become a write path, and a
            # candidate that cannot serve the tool must not shadow a later one
            # that can.
            if capability not in (found.capabilities or []):
                continue
            return candidate, found
        return None

    def _platform_executor(self, tool_name: str) -> ToolExecutor | None:
        """Build an executor for a tool the platform serves itself.

        `case.create` is built with the resolver's `tenant_id`, which the
        request path resolved server-side (never a client payload). The other
        platform tools need no tenant: `case.read` and `case.eq_confirm` work
        off rows that already exist, so RLS supplies it, while a create has no
        row yet and must name the tenant it is writing into.
        """
        if self._cache is None:
            self._cache = {}
        cached = self._cache.get(tool_name)
        if cached is not None:
            return cached

        from platform_core.tool_gateway.case_create import CaseCreateExecutor
        from platform_core.tool_gateway.case_eq_confirm import CaseEqConfirmExecutor
        from platform_core.tool_gateway.case_read import CaseReadExecutor

        builders: dict[str, Callable[[AsyncSession], ToolExecutor]] = {
            "case.read": CaseReadExecutor,
            "case.eq_confirm": CaseEqConfirmExecutor,
        }
        builder = builders.get(tool_name)
        if builder is not None:
            executor = builder(self._session)
            self._cache[tool_name] = executor
            return executor

        if tool_name == "case.create":
            executor = CaseCreateExecutor(
                self._session,
                tenant_id=self._tenant_id,
                actor_id=self._ctx.actor_id if self._ctx is not None else None,
            )
            self._cache[tool_name] = executor
            return executor

        return None

    def _build(self, tool_name: str, available: dict[str, Connector]) -> ToolExecutor | None:
        if tool_name in PLATFORM_TOOLS:
            return self._platform_executor(tool_name)
        resolved = self.resolve_connector(tool_name, available)
        if resolved is None:
            return None
        provider, connector = resolved

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

    def _build_linear(context: ConnectorContext) -> ToolExecutor:
        from platform_core.integrations.linear import LinearAdapter

        return LinearAdapter(context)

    # Three IM providers, not one: Feishu and Teams reject the Slack body, so
    # folding them into `im_webhook` would produce notifications the provider
    # discards while the platform reports success. See `integrations/im.py`.
    def _build_im(context: ConnectorContext) -> ToolExecutor:
        from platform_core.integrations.im import ImNotificationAdapter

        return ImNotificationAdapter(context)

    def _build_feishu(context: ConnectorContext) -> ToolExecutor:
        from platform_core.integrations.im import FeishuNotificationAdapter

        return FeishuNotificationAdapter(context)

    def _build_teams(context: ConnectorContext) -> ToolExecutor:
        from platform_core.integrations.im import TeamsNotificationAdapter

        return TeamsNotificationAdapter(context)

    def _build_business_read(context: ConnectorContext) -> ToolExecutor:
        from platform_core.config import get_settings

        # A deployment with no ERP to call can still run the whole read path
        # against sample data. Selected by configuration, and the sample
        # records say `source: "demo"` so they cannot be mistaken for a real
        # system's answer.
        if str(get_settings().business_api_adapter or "").strip().lower() == "demo":
            from platform_core.integrations.demo_erp import DemoBusinessToolExecutor

            return DemoBusinessToolExecutor(context)

        from platform_core.integrations.business_read import BusinessReadToolExecutor

        return BusinessReadToolExecutor(context)

    return {
        "jira": AdapterFactory(provider="jira", build=_build_jira),
        "crm": AdapterFactory(provider="crm", build=_build_crm),
        "linear": AdapterFactory(provider="linear", build=_build_linear),
        "im_webhook": AdapterFactory(provider="im_webhook", build=_build_im),
        "feishu": AdapterFactory(provider="feishu", build=_build_feishu),
        "teams": AdapterFactory(provider="teams", build=_build_teams),
        "business_api": AdapterFactory(provider="business_api", build=_build_business_read),
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
    "TOOL_PROVIDERS",
    "WRITE_ARG_DEFAULTS",
    "default_factories",
    "build_adapter",
    "probe_connector",
    "resolve_executors",
]


# The platform catalog every tenant gets: name -> (risk, input_schema,
# required permission actions, requires_confirmation). Seeded idempotently -
# the registry is deny-by-default, so a tool that exists only in
# TOOL_CAPABILITY but has no definition row would propose as
# TOOL_NOT_REGISTERED, which is the failure mode this seeding removes.
TOOL_CATALOG: dict[str, tuple[str, dict[str, Any], list[str], bool]] = {
    "jira.create_issue": (
        "confirmed_write",
        {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "summary": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["project", "summary"],
            "additionalProperties": False,
        },
        ["tool.write.confirmed"],
        True,
    ),
    "linear.create_issue": (
        "confirmed_write",
        {
            "type": "object",
            "properties": {
                "team": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["team", "title"],
            "additionalProperties": False,
        },
        ["tool.write.confirmed"],
        True,
    ),
    "crm.update_account": (
        "confirmed_write",
        {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "fields": {"type": "object"},
            },
            "required": ["account_id", "fields"],
            "additionalProperties": False,
        },
        ["tool.write.confirmed"],
        True,
    ),
    "im.send_notification": (
        "low_write",
        {
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["channel", "text"],
            "additionalProperties": False,
        },
        ["tool.write.low"],
        False,
    ),
    "order.get_status": ("read", READ_TOOL_SCHEMAS["order.get_status"], ["tool.read"], False),
    "shipment.track": ("read", READ_TOOL_SCHEMAS["shipment.track"], ["tool.read"], False),
    "billing.get_invoice": (
        "read",
        READ_TOOL_SCHEMAS["billing.get_invoice"],
        ["tool.read"],
        False,
    ),
    "case.read": ("read", READ_TOOL_SCHEMAS["case.read"], ["tool.read"], False),
    # The EQ confirmation (stage 2b). `human_approval`, which is what the
    # research report asks for in five places ("EQ 放行…必须人工"; "AI 只能转述 +
    # 收集确认，不能代替放行") and what the policy engine reserves for
    # tenant_owner.
    #
    # It was briefly `confirmed_write`, on the argument that `human_approval`
    # makes the tool unreachable *by the agent* and the write path built in 2a
    # could then never propose it. That argument answers the wrong question:
    # being unreachable by the agent is the point, not the problem.
    # `packages/policy/engine.py` says the class "must be unreachable by the
    # agent at every stage, propose included", and the reason is in the flow -
    # the case status is what the factory reads, so recording a confirmation is
    # one step from releasing production, and the customer's word in a
    # conversation is not a release. The AI relays and collects; a person
    # records it.
    "case.eq_confirm": (
        "human_approval",
        {
            "type": "object",
            "properties": {"case_ref": {"type": "string"}},
            "required": ["case_ref"],
            "additionalProperties": False,
        },
        ["tool.human_approval"],
        True,
    ),
    "inventory.check_stock": (
        "read",
        READ_TOOL_SCHEMAS["inventory.check_stock"],
        ["tool.read"],
        False,
    ),
    # Opening a case (stage 3). `confirmed_write`, not `human_approval`: the
    # risk inventory in the research report lists only "EQ 放行、赔付、退款" as
    # `HUMAN_APPROVAL`, and stage 3 says the *adjudication* of a complaint is
    # human. Creating a case moves nothing in the outside world; it writes a
    # row. `support_agent` already holds `CASE_CREATE`, so this matches the
    # existing policy table rather than tightening it. See
    # `docs/adr/0008-case-create-risk-class.md`.
    #
    # `enterprise_account_id` is in `required` on purpose. It selects the SLA
    # tier and both deadlines, and the tier is snapshotted onto the row, so a
    # wrong account starts a wrong clock that never self-corrects - and
    # `create_case` *succeeds* without one, leaving a ticket that can never
    # escalate. Requiring it here means a proposal missing it is rejected as
    # `TOOL_ARGS_INVALID` before the proposal row exists.
    #
    # `tenant_id` is deliberately absent from the schema: it is resolved
    # server-side by the executor's resolver, and accepting it as an argument
    # is the shortcut AGENTS.md forbids.
    "case.create": (
        "confirmed_write",
        {
            "type": "object",
            "properties": {
                "enterprise_account_id": {"type": "string", "minLength": 1},
                "subject": {"type": "string", "minLength": 1},
                "description": {"type": "string"},
                "priority": {"type": "string"},
                "category": {"type": "string"},
                "conversation_ref_id": {"type": "string"},
            },
            "required": ["enterprise_account_id", "subject"],
            "additionalProperties": False,
        },
        ["tool.write.confirmed"],
        True,
    ),
}


async def risk_action_for(
    session: AsyncSession, *, tenant_id: Any, tool_name: str
) -> Action | None:
    """The policy action a tool of this name requires, or None if unregistered.

    Deny-by-default: a name with no definition row has no risk class and is
    therefore not proposable - the same conclusion the gateway reaches with
    `TOOL_NOT_REGISTERED`, reached earlier so the caller can hand off instead
    of writing a proposal row that could never run.
    """
    from platform_core.tool_gateway.models import ToolDefinition

    risk = (
        await session.execute(
            select(ToolDefinition.risk)
            .where(
                (ToolDefinition.tenant_id == tenant_id) | ToolDefinition.tenant_id.is_(None),
                ToolDefinition.name == tool_name,
            )
            .order_by(ToolDefinition.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if risk is None:
        return None
    value = RISK_ACTION.get(str(risk))
    return Action(value) if value is not None else None


async def ensure_tool_definitions(session: AsyncSession, *, tenant_id: Any) -> int:
    """Register missing catalog definitions for a tenant. Idempotent.

    Returns the number of definitions created. Existing rows are never
    touched: a tenant that deliberately disabled a tool by editing its
    definition must not be silently repaired over.
    """
    import uuid as _uuid

    from platform_core.tool_gateway.models import ToolDefinition

    created = 0
    for name, (risk, schema, permissions, requires_confirmation) in TOOL_CATALOG.items():
        existing = (
            await session.execute(
                select(ToolDefinition).where(
                    ToolDefinition.tenant_id == tenant_id,
                    ToolDefinition.name == name,
                    ToolDefinition.version == 1,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            ToolDefinition(
                tenant_id=tenant_id,
                name=name,
                version=1,
                risk=risk,
                input_schema=schema,
                output_schema={},
                required_permissions=permissions,
                requires_confirmation=requires_confirmation,
            )
        )
        created += 1
    await session.flush()
    _ = _uuid
    return created
