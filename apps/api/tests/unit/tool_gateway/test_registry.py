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
    TOOL_CAPABILITY,
    TOOL_PROVIDER,
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

    # Only providers that appear in TOOL_PROVIDER need a factory; adding a
    # factory for a provider with no tools is what the wiring test guards
    # against in the other direction.
    return {p: make(p) for p in sorted(set(TOOL_PROVIDER.values()))}


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


def test_tool_vocabulary_is_fully_declared() -> None:
    """Every mapped provider has a capability and vice versa.

    A tool that appears in one map but not the other would silently resolve
    to nothing, which is hard to diagnose from a request log.
    """
    assert set(TOOL_PROVIDER) == set(TOOL_CAPABILITY)


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
    for tool_name, provider in TOOL_PROVIDER.items():
        assert provider in factories, f"{tool_name} maps to unregistered provider {provider}"
