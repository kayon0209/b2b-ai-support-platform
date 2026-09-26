"""Unit tests: connector-scoped tool executor resolution.

The resolver is the only place where a tool name becomes a live adapter.
That makes it a security boundary, and these tests pin the boundary itself
rather than any individual adapter:

- a tenant's executor is built from that tenant's connector;
- a connector that does not claim the capability never becomes a write path;
- a non-active connector is not usable;
- an unresolvable tool yields nothing, which the gateway turns into
  `TOOL_EXECUTOR_MISSING` instead of a different, confusing failure.
"""

from typing import Any

import pytest

from platform_core.integrations.models import Connector, ConnectorStatus
from platform_core.integrations.sdk import ConnectorContext
from platform_core.tool_gateway.gateway import ToolExecutor
from platform_core.tool_gateway.registry import (
    PLATFORM_TOOLS,
    TOOL_CAPABILITY,
    TOOL_CATALOG,
    TOOL_PROVIDERS,
    AdapterFactory,
    ConnectorExecutorResolver,
)

TENANT = "01900000-0000-7000-8000-0000000000e1"


class _FakeExecutor:
    """Records that it was built, and from which connector."""

    def __init__(self, context: ConnectorContext) -> None:
        self.context = context

    async def execute(
        self, tool_name: str, parameters: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        return {"ok": True}

    async def verify_postcondition(
        self, tool_name: str, parameters: dict[str, Any], output: dict[str, Any] | None
    ) -> bool | None:
        return True


def _connector(provider: str, capabilities: list[str], status: str) -> Connector:
    connector = Connector(
        tenant_id=TENANT,
        provider=provider,
        name=f"{provider}-primary",
        status=status,
        capabilities=capabilities,
        configuration={"base_url": f"https://{provider}.example"},
        credential_ref=f"vault://kv/{provider}",
    )
    # The PK is normally server-generated; assign one so the resolver can
    # read `connector.id` without a database.
    connector.id = TENANT.replace("e1", "f1")  # type: ignore[assignment]
    return connector


class _FakeResult:
    def __init__(self, rows: list[Connector]) -> None:
        self._rows = rows

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[Connector]:
        return self._rows


class _FakeSession:
    """Minimal stand-in that honors the status filter the resolver applies."""

    def __init__(self, rows: list[Connector]) -> None:
        self._rows = rows

    async def execute(self, stmt: Any) -> _FakeResult:
        # The resolver filters to ACTIVE in SQL. Emulating that here keeps
        # the test honest: a disabled connector must not reach the factory.
        active = [r for r in self._rows if r.status == ConnectorStatus.ACTIVE.value]
        return _FakeResult(active)


def _factories(built: list[ConnectorContext]) -> dict[str, AdapterFactory]:
    def make(provider: str) -> AdapterFactory:
        def build(context: ConnectorContext) -> ToolExecutor:
            built.append(context)
            return _FakeExecutor(context)

        return AdapterFactory(provider=provider, build=build)

    # Only providers that appear in TOOL_PROVIDERS need a factory; adding a
    # factory for a provider with no tools is what the wiring test guards
    # against in the other direction.
    return {p: make(p) for p in sorted({p for ps in TOOL_PROVIDERS.values() for p in ps})}


@pytest.mark.asyncio
async def test_resolves_executor_from_tenant_connector() -> None:
    built: list[ConnectorContext] = []
    session = _FakeSession([_connector("jira", ["create_issue"], ConnectorStatus.ACTIVE.value)])
    resolver = ConnectorExecutorResolver(session, tenant_id=TENANT, factories=_factories(built))

    executors = await resolver.executors_for(["jira.create_issue"])

    assert set(executors) == {"jira.create_issue"}
    # The adapter was built from the tenant's own connector, not a global.
    assert len(built) == 1
    assert built[0].tenant_id == TENANT
    assert built[0].configuration["base_url"] == "https://jira.example"


@pytest.mark.asyncio
async def test_no_connector_means_no_executor() -> None:
    """A tenant with nothing connected gets nothing, not a shared adapter."""
    session = _FakeSession([])
    resolver = ConnectorExecutorResolver(session, tenant_id=TENANT, factories=_factories([]))

    assert await resolver.executors_for(["jira.create_issue"]) == {}


@pytest.mark.asyncio
async def test_missing_capability_is_not_a_write_path() -> None:
    """A read-only connector must not become an executor for a write tool.

    This is the important one: connecting Jira for reads must never
    silently authorize creating issues.
    """
    built: list[ConnectorContext] = []
    session = _FakeSession([_connector("jira", ["read_issue"], ConnectorStatus.ACTIVE.value)])
    resolver = ConnectorExecutorResolver(session, tenant_id=TENANT, factories=_factories(built))

    assert await resolver.executors_for(["jira.create_issue"]) == {}
    assert built == []  # the factory was never even called


@pytest.mark.asyncio
async def test_disabled_connector_is_not_usable() -> None:
    built: list[ConnectorContext] = []
    session = _FakeSession([_connector("jira", ["create_issue"], ConnectorStatus.DISABLED.value)])
    resolver = ConnectorExecutorResolver(session, tenant_id=TENANT, factories=_factories(built))

    assert await resolver.executors_for(["jira.create_issue"]) == {}
    assert built == []


@pytest.mark.asyncio
async def test_unknown_tool_name_resolves_to_nothing() -> None:
    """A tool with no declared provider/capability is never executable."""
    built: list[ConnectorContext] = []
    session = _FakeSession([_connector("jira", ["create_issue"], ConnectorStatus.ACTIVE.value)])
    resolver = ConnectorExecutorResolver(session, tenant_id=TENANT, factories=_factories(built))

    assert await resolver.executors_for(["rm_rf_production"]) == {}


@pytest.mark.asyncio
async def test_factory_failure_degrades_instead_of_raising() -> None:
    """A misconfigured adapter must not turn into a 500."""

    def boom(context: ConnectorContext) -> ToolExecutor:
        raise RuntimeError("bad configuration")

    factories = {"jira": AdapterFactory(provider="jira", build=boom)}
    session = _FakeSession([_connector("jira", ["create_issue"], ConnectorStatus.ACTIVE.value)])
    resolver = ConnectorExecutorResolver(session, tenant_id=TENANT, factories=factories)

    assert await resolver.executors_for(["jira.create_issue"]) == {}


@pytest.mark.asyncio
async def test_executor_is_cached_within_a_resolver() -> None:
    """Two lookups of the same tool in one request reuse the adapter."""
    built: list[ConnectorContext] = []
    session = _FakeSession([_connector("jira", ["create_issue"], ConnectorStatus.ACTIVE.value)])
    resolver = ConnectorExecutorResolver(session, tenant_id=TENANT, factories=_factories(built))

    await resolver.executors_for(["jira.create_issue"])
    await resolver.executors_for(["jira.create_issue"])

    assert len(built) == 1


@pytest.mark.asyncio
async def test_two_resolvers_for_one_tenant_do_not_share_adapters() -> None:
    """Adapter instances are request-scoped, never process-global.

    Adapters hold per-tenant credentials and an instance-level response
    cache (`CrmReadAdapter._cache` keys on the external ref alone, not on
    the tenant). Sharing one instance between tenants would let tenant B
    read tenant A's cached account summary. Two resolvers for the same
    tenant must therefore still build their own adapters.
    """
    built: list[ConnectorContext] = []
    session = _FakeSession([_connector("jira", ["create_issue"], ConnectorStatus.ACTIVE.value)])
    factories = _factories(built)

    first = ConnectorExecutorResolver(session, tenant_id=TENANT, factories=factories)
    second = ConnectorExecutorResolver(session, tenant_id=TENANT, factories=factories)
    await first.executors_for(["jira.create_issue"])
    await second.executors_for(["jira.create_issue"])

    assert len(built) == 2, "each resolver must get its own adapter instance"


@pytest.mark.asyncio
async def test_resolver_query_is_tenant_scoped_in_sql() -> None:
    """The connector lookup must filter by tenant in the query itself.

    Filtering the rows after loading them would still pull another
    tenant's credential reference into this process, which is the thing
    the tenant boundary exists to prevent. `_FakeSession` ignores the
    WHERE clause, so this test inspects the compiled statement instead of
    trusting the fake.
    """
    captured: list[Any] = []

    class _CapturingSession:
        async def execute(self, stmt: Any) -> _FakeResult:
            captured.append(stmt)
            return _FakeResult([])

    resolver = ConnectorExecutorResolver(
        _CapturingSession(),
        tenant_id=TENANT,
        factories=_factories([]),  # type: ignore[arg-type]
    )
    await resolver.executors_for(["jira.create_issue"])

    assert captured, "the resolver must query for connectors"
    compiled = captured[0].compile()
    sql = str(compiled)
    assert "connectors.tenant_id" in sql, sql
    # The bound parameter carries this tenant's id, so the filter is real
    # rather than a placeholder that a caller forgot to populate.
    assert any(str(TENANT) in str(v) for v in compiled.params.values()), compiled.params


def test_every_platform_tool_is_in_the_catalog() -> None:
    """A tool the registry builds but the catalog does not define is
    unproposable: the gateway refuses `TOOL_NOT_REGISTERED` before the executor
    is ever reached, so the tool exists and cannot be called."""
    assert PLATFORM_TOOLS <= set(TOOL_CATALOG)


def test_platform_tools_are_not_connector_backed() -> None:
    """They read and write the platform's own tables, so a provider entry
    would be a claim that some external system serves them."""
    assert not (PLATFORM_TOOLS & set(TOOL_PROVIDERS))
    assert not (PLATFORM_TOOLS & set(TOOL_CAPABILITY))


@pytest.mark.asyncio
async def test_a_platform_tool_resolves_with_no_connectors_at_all() -> None:
    """The point of the platform path: no connector is required, which is what
    lets the HTTP surface execute these tools and not only the orchestrator.

    `case.read` used to be injected by hand inside the read path, so it was
    executable from exactly one caller and answered `TOOL_EXECUTOR_MISSING`
    through `POST /v1/tool-proposals/{id}/execute`.
    """
    resolver = ConnectorExecutorResolver(
        _FakeSession([]), tenant_id=TENANT, factories=_factories([])
    )

    executors = await resolver.executors_for(["case.read", "case.eq_confirm"])

    assert set(executors) == {"case.read", "case.eq_confirm"}


def test_tool_vocabulary_is_fully_declared() -> None:
    """Every mapped provider has a capability and vice versa.

    A tool that appears in one map but not the other would silently resolve
    to nothing, which is hard to diagnose from a request log.
    """
    assert set(TOOL_PROVIDERS) == set(TOOL_CAPABILITY)


def test_every_declared_tool_has_a_registered_adapter() -> None:
    """A tool in the vocabulary with no adapter is a wiring bug.

    `CrmReadAdapter` was registered here once and does not satisfy the
    `ToolExecutor` protocol (its inherited `execute` takes an
    `ExecutionResult`), so a CRM tool would have raised `TypeError` the
    first time it executed. This test keeps the vocabulary and the
    factories in agreement.
    """
    from platform_core.tool_gateway.registry import default_factories

    factories = default_factories()
    # Every provider in the tuple, not just the preferred one: a tool that can
    # only be served by the first entry leaves the alternatives registered but
    # unreachable, which is indistinguishable from not shipping them.
    for tool_name, providers in TOOL_PROVIDERS.items():
        for provider in providers:
            assert provider in factories, f"{tool_name} maps to unregistered provider {provider}"


@pytest.mark.asyncio
async def test_crm_write_tool_resolves_for_a_write_capable_connector() -> None:
    """A CRM connector that claims `update_account` becomes an executor."""
    built: list[ConnectorContext] = []
    session = _FakeSession(
        [_connector("crm", ["read_account", "update_account"], ConnectorStatus.ACTIVE.value)]
    )
    resolver = ConnectorExecutorResolver(session, tenant_id=TENANT, factories=_factories(built))

    executors = await resolver.executors_for(["crm.update_account"])

    assert set(executors) == {"crm.update_account"}
    assert built[0].configuration["base_url"] == "https://crm.example"


@pytest.mark.asyncio
async def test_lookup_only_crm_is_not_a_write_path() -> None:
    """Connecting a CRM for lookups must not authorize crm.update_account."""
    built: list[ConnectorContext] = []
    session = _FakeSession(
        [_connector("crm", ["read_account", "read_contact"], ConnectorStatus.ACTIVE.value)]
    )
    resolver = ConnectorExecutorResolver(session, tenant_id=TENANT, factories=_factories(built))

    assert await resolver.executors_for(["crm.update_account"]) == {}
    assert built == []


@pytest.mark.asyncio
async def test_credentials_are_resolved_through_the_injected_resolver() -> None:
    """The adapter must receive real credentials, not an empty mapping.

    Nothing dereferenced `credential_ref`, so every adapter got `{}` and no
    external call could authenticate. The registry still must not read a
    secret itself, so resolution is injected.
    """
    built: list[ConnectorContext] = []
    session = _FakeSession([_connector("jira", ["create_issue"], ConnectorStatus.ACTIVE.value)])
    resolver = ConnectorExecutorResolver(
        session,
        tenant_id=TENANT,
        factories=_factories(built),
        credential_resolver=lambda ref: {"api_token": f"tok:{ref}"},
    )

    await resolver.executors_for(["jira.create_issue"])

    assert built[0].credentials == {"api_token": "tok:vault://kv/jira"}


@pytest.mark.asyncio
async def test_unsupported_credential_reference_yields_no_credentials() -> None:
    """Fail closed: an unknown scheme must not produce a placeholder token."""
    built: list[ConnectorContext] = []
    session = _FakeSession([_connector("jira", ["create_issue"], ConnectorStatus.ACTIVE.value)])
    resolver = ConnectorExecutorResolver(session, tenant_id=TENANT, factories=_factories(built))

    await resolver.executors_for(["jira.create_issue"])

    # `_connector` uses vault://, which the default resolver does not support.
    assert built[0].credentials == {}


@pytest.mark.asyncio
async def test_the_im_tool_resolves_for_a_feishu_connector() -> None:
    """`im.send_notification` is "notify the on-call channel", not "post to a
    Slack-shaped webhook". Mapping the tool to `im_webhook` alone left the
    Feishu and Teams adapters registered but unreachable - dead code, and a
    tenant on Lark could not be paged at all.
    """
    built: list[ConnectorContext] = []
    session = _FakeSession(
        [_connector("feishu", ["send_notification"], ConnectorStatus.ACTIVE.value)]
    )
    resolver = ConnectorExecutorResolver(session, tenant_id=TENANT, factories=_factories(built))

    executors = await resolver.executors_for(["im.send_notification"])

    assert set(executors) == {"im.send_notification"}
    assert built[0].configuration["base_url"] == "https://feishu.example"


@pytest.mark.asyncio
async def test_a_preferred_provider_without_the_capability_does_not_shadow_another() -> None:
    """A Slack connector that only reads must not block a Feishu connector
    that can send: the first candidate is a preference, not a veto."""
    built: list[ConnectorContext] = []
    session = _FakeSession(
        [
            _connector("im_webhook", ["read_notification"], ConnectorStatus.ACTIVE.value),
            _connector("feishu", ["send_notification"], ConnectorStatus.ACTIVE.value),
        ]
    )
    resolver = ConnectorExecutorResolver(session, tenant_id=TENANT, factories=_factories(built))

    executors = await resolver.executors_for(["im.send_notification"])

    assert set(executors) == {"im.send_notification"}
    assert built[0].configuration["base_url"] == "https://feishu.example"
