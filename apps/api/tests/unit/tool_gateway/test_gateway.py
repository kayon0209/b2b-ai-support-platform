"""Unit tests: Tool Gateway (tickets 29-30).

Covers the docs/testing-and-evaluation.md safety invariants:
- args validated against the tool's JSON Schema (deny-by-default registry)
- prohibited tools never execute
- high-risk tools require confirmation bound to the same action hash
- duplicate idempotency keys execute exactly once
- ambiguous postcondition stays UNKNOWN, never success
- sanitized inputs never contain credentials or PII
"""

import time
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.tool_gateway.gateway import (
    ToolDenied,
    ToolGateway,
    ToolGatewayError,
    compute_action_hash,
    sanitize_arguments,
)
from platform_core.tool_gateway.models import ToolDefinition, ToolExecution


def _read_tool() -> ToolDefinition:
    return ToolDefinition(
        tenant_id=None,
        name="crm.get_account",
        version=1,
        risk="read",
        input_schema={
            "type": "object",
            "required": ["account_ref"],
            "properties": {"account_ref": {"type": "string"}},
        },
    )


def _confirmed_write_tool() -> ToolDefinition:
    return ToolDefinition(
        tenant_id=None,
        name="crm.add_note",
        version=1,
        risk="confirmed_write",
        input_schema={
            "type": "object",
            "required": ["account_ref", "note"],
            "properties": {"account_ref": {"type": "string"}, "note": {"type": "string"}},
        },
    )


def _prohibited_tool() -> ToolDefinition:
    return ToolDefinition(
        tenant_id=None,
        name="crm.delete_account",
        version=1,
        risk="prohibited",
        input_schema={"type": "object", "properties": {}},
    )


def _low_write_tool() -> ToolDefinition:
    return ToolDefinition(
        tenant_id=None,
        name="crm.tag_account",
        version=1,
        risk="low_write",
        input_schema={
            "type": "object",
            "required": ["account_ref"],
            "properties": {"account_ref": {"type": "string"}},
        },
    )


class FakeExecutor:
    def __init__(
        self, *, output: dict | None = None, postcondition: bool | None = True, fail: bool = False
    ) -> None:
        self.calls: list[tuple[str, dict, str]] = []
        self._output = output
        self._postcondition = postcondition
        self._fail = fail

    async def execute(self, tool_name, parameters, idempotency_key):
        self.calls.append((tool_name, parameters, idempotency_key))
        if self._fail:
            raise RuntimeError("adapter exploded")
        return self._output or {"done": True}

    async def verify_postcondition(self, tool_name, parameters, output):
        return self._postcondition


@pytest.fixture
def gateway_env():
    """Async in-memory-ish fixture returning a factory bound to a session."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("sqlite+aiosqlite://")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def make_gateway(executor: FakeExecutor, tools: list[ToolDefinition] | None = None):
        from sqlalchemy.dialects.postgresql import JSONB
        from sqlalchemy.ext.compiler import compiles

        try:

            @compiles(JSONB, "sqlite")
            def _jsonb_sqlite(type_, compiler, **kw):
                return "JSON"
        except Exception:
            pass

        async with engine.begin() as conn:
            from platform_core.orm_base import Base

            await conn.run_sync(Base.metadata.create_all)
        session = factory()
        gw = ToolGateway(
            session,
            {
                "crm.get_account": executor,
                "crm.add_note": executor,
                "crm.tag_account": executor,
                "crm.delete_account": executor,
            },
        )
        for tool in tools or [
            _read_tool(),
            _confirmed_write_tool(),
            _prohibited_tool(),
            _low_write_tool(),
        ]:
            session.add(tool)
        await session.commit()
        return gw, session, engine

    return make_gateway


TENANT = uuid.uuid4()
ACTOR = uuid.uuid4()


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_sanitize_strips_secrets_and_pii() -> None:
    cleaned = sanitize_arguments(
        {
            "account_ref": "a1",
            "api_key": "sk-123",
            "bankAccountNumber": "123456789",
            "password": "hunter2",
            "tokens": ["synthetic-secret-token"],
            "tokens_used": 37,
            "nested": {"token": "x", "note": "hello"},
            "contacts": [{"bank_account_number": "987654321", "name": "Synthetic Contact"}],
            "contact_emails": ["synthetic@example.test"],
        }
    )
    assert cleaned["api_key"] == "***"
    assert cleaned["bankAccountNumber"] == "***"
    assert cleaned["password"] == "***"
    assert cleaned["tokens"] == "***"
    assert cleaned["tokens_used"] == 37
    assert cleaned["nested"]["token"] == "***"
    assert cleaned["nested"]["note"] == "hello"
    assert cleaned["account_ref"] == "a1"
    assert cleaned["contacts"][0]["bank_account_number"] == "***"
    assert cleaned["contacts"][0]["name"] == "Synthetic Contact"
    assert cleaned["contact_emails"] == "***"


def test_action_hash_changes_with_arguments() -> None:
    h1 = compute_action_hash("t", 1, {"a": 1})
    h2 = compute_action_hash("t", 1, {"a": 2})
    h3 = compute_action_hash("t", 2, {"a": 1})
    assert h1 != h2 and h1 != h3


def test_unregistered_tool_denied(gateway_env) -> None:
    async def scenario() -> str:
        executor = FakeExecutor()
        gw, session, engine = await gateway_env(executor)
        try:
            await gw.propose(
                tenant_id=TENANT,
                actor_id=ACTOR,
                tool_name="unknown.tool",
                arguments={},
                role="support_agent",
                idempotency_key="k1",
                permission_allowed=True,
            )
            return "allowed"
        except ToolDenied as exc:
            return exc.code
        finally:
            await session.close()
            await engine.dispose()

    assert _run(scenario()) == "TOOL_NOT_REGISTERED"


@pytest.mark.zero_tolerance("unauthorized_writes")
def test_prohibited_tool_never_executes(gateway_env) -> None:
    async def scenario() -> str:
        executor = FakeExecutor()
        gw, session, engine = await gateway_env(executor)
        try:
            await gw.propose(
                tenant_id=TENANT,
                actor_id=ACTOR,
                tool_name="crm.delete_account",
                arguments={},
                role="tenant_owner",
                idempotency_key="k2",
                permission_allowed=True,
            )
            return "proposed"
        except ToolDenied as exc:
            return exc.code
        finally:
            await session.close()
            await engine.dispose()

    assert _run(scenario()) == "TOOL_PROHIBITED"


def test_invalid_schema_args_rejected(gateway_env) -> None:
    async def scenario() -> str:
        executor = FakeExecutor()
        gw, session, engine = await gateway_env(executor)
        try:
            await gw.propose(
                tenant_id=TENANT,
                actor_id=ACTOR,
                tool_name="crm.get_account",
                arguments={"wrong": "arg"},
                role="support_agent",
                idempotency_key="k3",
                permission_allowed=True,
            )
            return "accepted"
        except ToolGatewayError as exc:
            return exc.code
        finally:
            await session.close()
            await engine.dispose()

    assert _run(scenario()) == "TOOL_ARGS_INVALID"


@pytest.mark.zero_tolerance("unauthorized_writes")
def test_denied_permission_creates_rejected_proposal(gateway_env) -> None:
    async def scenario() -> str:
        executor = FakeExecutor()
        gw, session, engine = await gateway_env(executor)
        try:
            await gw.propose(
                tenant_id=TENANT,
                actor_id=ACTOR,
                tool_name="crm.get_account",
                arguments={"account_ref": "a1"},
                role="support_viewer",
                idempotency_key="k4",
                permission_allowed=False,
                permission_reason="ROLE_LACKS_ACTION",
            )
            return "allowed"
        except ToolDenied:
            return "rejected-by-gateway"
        finally:
            await session.close()
            await engine.dispose()

    assert _run(scenario()) == "rejected-by-gateway"


@pytest.mark.zero_tolerance("unauthorized_writes")
def test_high_risk_requires_confirmation_before_execute(gateway_env) -> None:
    async def scenario() -> tuple[str, int]:
        executor = FakeExecutor()
        gw, session, engine = await gateway_env(executor)
        proposal = await gw.propose(
            tenant_id=TENANT,
            actor_id=ACTOR,
            tool_name="crm.add_note",
            arguments={"account_ref": "a1", "note": "please cancel"},
            role="support_admin",
            idempotency_key="k5",
            permission_allowed=True,
        )
        # Execute WITHOUT confirmation: must be denied
        try:
            await gw.execute(tenant_id=TENANT, actor_id=ACTOR, proposal_id=proposal.id)
            no_confirm = "executed-anyway"
        except ToolDenied as exc:
            no_confirm = exc.code
        # Rollback here only discards the failed execute attempt's partial
        # writes; the proposal row from the flushed propose() stays in the
        # session because expire_on_commit=False and no commit happened.
        # commit so the proposal persists for the confirm+execute phase.
        await session.commit()

        # Confirm, then execute: adapter runs exactly once
        await gw.confirm(tenant_id=TENANT, proposal_id=proposal.id, actor_id=ACTOR)
        execution = await gw.execute(tenant_id=TENANT, actor_id=ACTOR, proposal_id=proposal.id)
        calls = len(executor.calls)
        status = execution.status
        await session.close()
        await engine.dispose()
        return no_confirm, calls, status

    no_confirm, calls, status = _run(scenario())
    assert no_confirm == "CONFIRMATION_REQUIRED"
    assert calls == 1
    assert status == "executed"


@pytest.mark.zero_tolerance("unauthorized_writes")
def test_confirmation_binds_to_action_hash(gateway_env) -> None:
    async def scenario() -> str:
        executor = FakeExecutor()
        gw, session, engine = await gateway_env(executor)
        proposal = await gw.propose(
            tenant_id=TENANT,
            actor_id=ACTOR,
            tool_name="crm.add_note",
            arguments={"account_ref": "a1", "note": "v1"},
            role="support_admin",
            idempotency_key="k6",
            permission_allowed=True,
        )
        confirmation = await gw.confirm(tenant_id=TENANT, proposal_id=proposal.id, actor_id=ACTOR)
        # Tamper: recompute what the hash would be for different args
        tampered = compute_action_hash("crm.add_note", 1, {"account_ref": "a1", "note": "v2"})
        assert confirmation.action_hash == proposal.action_hash != tampered
        execution = await gw.execute(tenant_id=TENANT, actor_id=ACTOR, proposal_id=proposal.id)
        status = execution.status
        await session.close()
        await engine.dispose()
        return status

    # The confirmation binds to the proposal's frozen hash; execution succeeds
    # only because the proposal's args are unchanged.
    assert _run(scenario()) == "executed"


def test_confirmation_expiring_during_pre_execution_check_blocks_dispatch(
    gateway_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expiry is checked again after a slow live guard, at the intent boundary."""
    import platform_core.tool_gateway.gateway as gateway_module

    clock = [int(time.time())]
    monkeypatch.setattr(gateway_module.time, "time", lambda: clock[0])

    async def scenario() -> tuple[str, int, int]:
        executor = FakeExecutor()
        gw, session, engine = await gateway_env(executor)
        proposal = await gw.propose(
            tenant_id=TENANT,
            actor_id=ACTOR,
            tool_name="crm.add_note",
            arguments={"account_ref": "a1", "note": "approval must still be current"},
            role="support_admin",
            idempotency_key="expires-during-precheck",
            permission_allowed=True,
        )
        confirmation = await gw.confirm(tenant_id=TENANT, proposal_id=proposal.id, actor_id=ACTOR)

        async def expire_before_intent(*_args: object, **_kwargs: object) -> None:
            clock[0] = confirmation.expires_at

        try:
            try:
                await gw.execute(
                    tenant_id=TENANT,
                    actor_id=ACTOR,
                    proposal_id=proposal.id,
                    pre_execution_check=expire_before_intent,
                )
                code = "executed"
            except ToolGatewayError as exc:
                code = exc.code
            execution_result = await session.execute(
                select(func.count()).select_from(ToolExecution)
            )
            execution_count = int(execution_result.scalar_one())
            return code, execution_count, len(executor.calls)
        finally:
            await session.close()
            await engine.dispose()

    assert _run(scenario()) == ("CONFIRMATION_EXPIRED", 0, 0)


def test_duplicate_idempotency_key_executes_once(gateway_env) -> None:
    async def scenario() -> int:
        executor = FakeExecutor()
        gw, session, engine = await gateway_env(executor)
        proposal = await gw.propose(
            tenant_id=TENANT,
            actor_id=ACTOR,
            tool_name="crm.tag_account",
            arguments={"account_ref": "a1"},
            role="support_admin",
            idempotency_key="dup-1",
            permission_allowed=True,
        )
        first = await gw.execute(tenant_id=TENANT, actor_id=ACTOR, proposal_id=proposal.id)
        second = await gw.execute(tenant_id=TENANT, actor_id=ACTOR, proposal_id=proposal.id)
        calls = len(executor.calls)
        same_id = first.id == second.id
        await session.close()
        await engine.dispose()
        assert same_id
        return calls

    assert _run(scenario()) == 1


def test_ambiguous_postcondition_stays_unknown(gateway_env) -> None:
    async def scenario() -> str:
        executor = FakeExecutor(postcondition=None)  # cannot determine
        gw, session, engine = await gateway_env(executor)
        proposal = await gw.propose(
            tenant_id=TENANT,
            actor_id=ACTOR,
            tool_name="crm.tag_account",
            arguments={"account_ref": "a1"},
            role="support_admin",
            idempotency_key="k7",
            permission_allowed=True,
        )
        execution = await gw.execute(tenant_id=TENANT, actor_id=ACTOR, proposal_id=proposal.id)
        status = execution.status
        verification = execution.verification_status
        await session.close()
        await engine.dispose()
        assert status == "unknown" and verification == "unknown"
        return status

    assert _run(scenario()) == "unknown"


def test_failed_postcondition_marks_failed(gateway_env) -> None:
    async def scenario() -> str:
        executor = FakeExecutor(postcondition=False)
        gw, session, engine = await gateway_env(executor)
        proposal = await gw.propose(
            tenant_id=TENANT,
            actor_id=ACTOR,
            tool_name="crm.tag_account",
            arguments={"account_ref": "a1"},
            role="support_admin",
            idempotency_key="k8",
            permission_allowed=True,
        )
        execution = await gw.execute(tenant_id=TENANT, actor_id=ACTOR, proposal_id=proposal.id)
        status = execution.status
        await session.close()
        await engine.dispose()
        return status

    assert _run(scenario()) == "failed"


def test_adapter_crash_marks_execution_failed(gateway_env) -> None:
    async def scenario() -> str:
        executor = FakeExecutor(fail=True)
        gw, session, engine = await gateway_env(executor)
        proposal = await gw.propose(
            tenant_id=TENANT,
            actor_id=ACTOR,
            tool_name="crm.tag_account",
            arguments={"account_ref": "a1"},
            role="support_admin",
            idempotency_key="k9",
            permission_allowed=True,
        )
        try:
            await gw.execute(tenant_id=TENANT, actor_id=ACTOR, proposal_id=proposal.id)
            return "no-crash"
        except ToolGatewayError:
            return "failed-cleanly"
        finally:
            await session.close()
            await engine.dispose()

    assert _run(scenario()) == "failed-cleanly"


# --- Worker termination mid-execution (pilot failure-injection gate) ---
#
# `execute` creates the ToolExecution row with status EXECUTING and *then*
# calls the adapter. If the worker is killed inside that call - the classic
# OOM/SIGKILL/deploy case - the row survives with no completed_at and no
# output. Replaying the same idempotency key must not present that corpse as
# a result, because the caller cannot distinguish "already done" from
# "died half way", and the safe answer differs: one is a no-op, the other
# needs a human or a bounded retry.


def _crashed_execution(session, proposal, idempotency_key, *, started_at=0):
    """The row a killed worker leaves behind: EXECUTING, never completed."""
    from platform_core.tool_gateway.models import ToolExecution

    return ToolExecution(
        tenant_id=TENANT,
        proposal_id=proposal.id,
        actor_id=ACTOR,
        tool_definition_id=proposal.tool_definition_id,
        status="executing",
        idempotency_key=idempotency_key,
        sanitized_input={"account_ref": "a1"},
        started_at=started_at,
        completed_at=None,
    )


def test_replay_of_a_crashed_execution_is_not_reported_as_a_result(gateway_env) -> None:
    async def scenario() -> tuple[str, int, str]:
        from sqlalchemy import select

        from platform_core.tool_gateway.models import ToolExecution

        executor = FakeExecutor()
        gw, session, engine = await gateway_env(executor)
        proposal = await gw.propose(
            tenant_id=TENANT,
            actor_id=ACTOR,
            tool_name="crm.tag_account",
            arguments={"account_ref": "a1"},
            role="support_admin",
            idempotency_key="crash-1",
            permission_allowed=True,
        )
        session.add(_crashed_execution(session, proposal, "crash-1"))
        await session.commit()
        try:
            with pytest.raises(ToolGatewayError) as raised:
                await gw.execute(tenant_id=TENANT, actor_id=ACTOR, proposal_id=proposal.id)
            status = await session.scalar(
                select(ToolExecution.status).where(
                    ToolExecution.tenant_id == TENANT,
                    ToolExecution.idempotency_key == "crash-1",
                )
            )
            return raised.value.code, len(executor.calls), str(status)
        finally:
            await session.close()
            await engine.dispose()

    code, calls, status = _run(scenario())
    assert code == "TOOL_EXECUTION_OUTCOME_UNKNOWN"
    assert calls == 0, "an unresolved provider call must not be blindly replayed"
    assert status == "unknown"


def test_recent_execution_intent_is_not_replayed(gateway_env) -> None:
    """A recent intent may still be running, so a duplicate call only reports it."""

    async def scenario() -> tuple[str, int, str]:
        from sqlalchemy import select

        from platform_core.tool_gateway.models import ToolExecution

        executor = FakeExecutor(output={"done": True})
        gw, session, engine = await gateway_env(executor)
        proposal = await gw.propose(
            tenant_id=TENANT,
            actor_id=ACTOR,
            tool_name="crm.tag_account",
            arguments={"account_ref": "a1"},
            role="support_admin",
            idempotency_key="crash-2",
            permission_allowed=True,
        )
        session.add(_crashed_execution(session, proposal, "crash-2", started_at=int(time.time())))
        await session.commit()
        try:
            with pytest.raises(ToolGatewayError) as raised:
                await gw.execute(tenant_id=TENANT, actor_id=ACTOR, proposal_id=proposal.id)
            status = await session.scalar(
                select(ToolExecution.status).where(
                    ToolExecution.tenant_id == TENANT,
                    ToolExecution.idempotency_key == "crash-2",
                )
            )
            return raised.value.code, len(executor.calls), str(status)
        finally:
            await session.close()
            await engine.dispose()

    code, calls, status = _run(scenario())
    assert code == "TOOL_EXECUTION_IN_PROGRESS"
    assert calls == 0
    assert status == "executing"


def test_execution_intent_is_committed_before_call_and_lost_ack_is_not_replayed(
    gateway_env,
) -> None:
    async def scenario() -> tuple[str, int, str]:
        from sqlalchemy import select

        from platform_core.tool_gateway.models import ToolExecution

        gw, session, engine = await gateway_env(FakeExecutor())

        class _LostAckExecutor:
            calls = 0
            durable_status_before_call: str | None = None

            async def execute(self, tool_name, parameters, idempotency_key):
                self.calls += 1
                observer_factory = async_sessionmaker(engine, expire_on_commit=False)
                async with observer_factory() as observer:
                    self.durable_status_before_call = await observer.scalar(
                        select(ToolExecution.status).where(
                            ToolExecution.tenant_id == TENANT,
                            ToolExecution.idempotency_key == idempotency_key,
                        )
                    )
                # Simulate the provider accepting a write, then the process
                # dying before it records the response.
                raise SystemExit("synthetic process exit after provider acceptance")

            async def verify_postcondition(self, tool_name, parameters, output):
                return None

        executor = _LostAckExecutor()
        gw = ToolGateway(session, {"crm.tag_account": executor})
        proposal = await gw.propose(
            tenant_id=TENANT,
            actor_id=ACTOR,
            tool_name="crm.tag_account",
            arguments={"account_ref": "a1"},
            role="support_admin",
            idempotency_key="lost-ack-1",
            permission_allowed=True,
        )
        with pytest.raises(SystemExit):
            await gw.execute(tenant_id=TENANT, actor_id=ACTOR, proposal_id=proposal.id)
        assert executor.durable_status_before_call == "executing"
        await session.close()

        factory = async_sessionmaker(engine, expire_on_commit=False)
        recovery_session = factory()
        try:
            row = (
                await recovery_session.execute(
                    select(ToolExecution).where(
                        ToolExecution.tenant_id == TENANT,
                        ToolExecution.idempotency_key == "lost-ack-1",
                    )
                )
            ).scalar_one()
            assert row.status == "executing"
            row.started_at = 0  # elapse the bounded in-progress grace in the test
            await recovery_session.commit()

            recovery_gateway = ToolGateway(recovery_session, {"crm.tag_account": executor})
            with pytest.raises(ToolGatewayError) as raised:
                await recovery_gateway.execute(
                    tenant_id=TENANT, actor_id=ACTOR, proposal_id=proposal.id
                )
            await recovery_session.commit()
            status = await recovery_session.scalar(
                select(ToolExecution.status).where(
                    ToolExecution.tenant_id == TENANT,
                    ToolExecution.idempotency_key == "lost-ack-1",
                )
            )
            return raised.value.code, executor.calls, str(status)
        finally:
            await recovery_session.close()
            await engine.dispose()

    code, calls, status = _run(scenario())
    assert code == "TOOL_EXECUTION_OUTCOME_UNKNOWN"
    assert calls == 1, "replaying after a lost provider response would duplicate the write"
    assert status == "unknown"


def test_completed_execution_still_short_circuits(gateway_env) -> None:
    """The normal idempotency guarantee must survive the crash fix."""

    async def scenario() -> tuple[str, int]:
        executor = FakeExecutor()
        gw, session, engine = await gateway_env(executor)
        proposal = await gw.propose(
            tenant_id=TENANT,
            actor_id=ACTOR,
            tool_name="crm.tag_account",
            arguments={"account_ref": "a1"},
            role="support_admin",
            idempotency_key="ok-1",
            permission_allowed=True,
        )
        try:
            first = await gw.execute(tenant_id=TENANT, actor_id=ACTOR, proposal_id=proposal.id)
            second = await gw.execute(tenant_id=TENANT, actor_id=ACTOR, proposal_id=proposal.id)
            # The replay must hand back the *same* execution, not a new row.
            assert first.id == second.id
            return first.status, len(executor.calls)
        finally:
            await session.close()
            await engine.dispose()

    status, calls = _run(scenario())
    assert status == "executed"
    assert calls == 1, "a completed execution was executed twice"
