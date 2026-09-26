"""Integration: the agent's write path against real Postgres.

`Route.BUSINESS_WRITE` has been produced by `intent.py` since the taxonomy
landed, and `IntentAction.PROPOSE_WRITE` was mapped alongside it — and the
orchestrator had no branch for either. An action request fell through to the
knowledge path, which refuses action requests, so the agent could only ever
refuse. The capability existed and had no consumer.

These tests pin what replaced that, and above all the property that makes it
safe to ship: **the agent proposes and never approves.** A confirmed write
stops at the proposal; a low-risk write runs; nothing is ever reported as done
on the strength of a transport call alone.

Real Postgres, because the claims under test are about rows: that a proposal
exists with `required_confirmation`, that no `action_confirmations` row exists
for it, and that the tenant boundary holds. A fake session would prove none of
that.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.channels.outbound import ChannelSender, SendResult

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "01900000-0000-7000-8000-0000000000b1"
SLUG = "agent-write-path"

# An utterance the taxonomy routes to the write path ("escalate" is one of the
# shared ACTION_VERBS). It names both a defect and an escalation, so the
# selector offers the ticket tool and the notification tool and the tenant's
# connected systems decide which one survives.
WRITE_REQUEST = "Please escalate this defect to your engineering team"

WRITE_FLAG = "agent.business_write_enabled"


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _with_ctx(session, tenant_id: str) -> None:
    await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})


def _seed_tenant() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'Agent Write Path', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT, "slug": SLUG},
        )
    admin.dispose()


def _clear() -> None:
    """Remove everything the tests write, children before parents.

    Leftovers are not cosmetic here: a stray `tool_proposals` row would make
    the next test's "exactly one proposal" assertion pass for the wrong
    reason, and the cross-tenant queue scans elsewhere in the suite are global
    by design.
    """
    # Written out rather than generated from a table-name list: a generated
    # DELETE is the shape SQL injection has, and a test that builds one is a
    # bad example even when every name is a literal.
    #
    # Order is children-before-parents. `citations` must go before
    # `agent_runs`, which it carries an FK to.
    statements = (
        "DELETE FROM tool_executions WHERE tenant_id = :t",
        "DELETE FROM action_confirmations WHERE tenant_id = :t",
        "DELETE FROM tool_proposals WHERE tenant_id = :t",
        "DELETE FROM tool_definitions WHERE tenant_id = :t",
        "DELETE FROM connectors WHERE tenant_id = :t",
        "DELETE FROM feature_flags WHERE tenant_id = :t",
        "DELETE FROM feature_flag_targets WHERE tenant_id = :t",
        "DELETE FROM citations WHERE tenant_id = :t",
        "DELETE FROM agent_runs WHERE tenant_id = :t",
        "DELETE FROM audit_events WHERE tenant_id = :t",
        "DELETE FROM conversation_control_leases WHERE tenant_id = :t",
        # The EQ-confirmation tests seed a case and its conversation link, and
        # a cleanup that omits a child table poisons the file's own next run:
        # `case_conversations` carries an FK to `cases`, so it goes first.
        "DELETE FROM case_conversations WHERE tenant_id = :t",
        "DELETE FROM cases WHERE tenant_id = :t",
    )
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for statement in statements:
            conn.execute(text(statement), {"t": TENANT})
        # The tenant itself, last. A leftover tenant is not a claimable queue
        # row, so it cannot perturb another suite's batch - but a dev database
        # accumulating one tenant per test file is how "which tenant is that?"
        # becomes a recurring question.
        conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": SLUG})
    admin.dispose()


def _seed_connector(
    *, provider: str, capabilities: list[str], configuration: dict[str, str] | None = None
) -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO connectors (id, tenant_id, provider, name, status, "
                "capabilities, configuration, credential_ref) VALUES "
                "(:id, :t, :p, :n, 'active', CAST(:caps AS jsonb), CAST(:cfg AS jsonb), NULL)"
            ),
            {
                "id": str(uuid.uuid4()),
                "t": TENANT,
                "p": provider,
                "n": f"{provider}-primary",
                "caps": json.dumps(capabilities),
                "cfg": json.dumps(configuration or {}),
            },
        )
    admin.dispose()


def _enable_write_flag() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO feature_flags (id, tenant_id, key, description, enabled, "
                "rollout_percent, created_at) VALUES (:id, :t, :k, '', true, 100, 0) "
                "ON CONFLICT (tenant_id, key) DO UPDATE SET enabled = true, rollout_percent = 100"
            ),
            {"id": str(uuid.uuid4()), "t": TENANT, "k": WRITE_FLAG},
        )
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_tenant() -> None:
    _seed_tenant()
    _clear()
    yield
    _clear()


async def _execute_write_run(
    *,
    tool_factories=None,
    generator=None,
    question: str = WRITE_REQUEST,
    conversation_ref_id: uuid.UUID | None = None,
):
    """Drive one orchestrator run over the write path.

    Returns (outcome, proposals, executions, confirmations, citations) read
    back from the database, so the assertions are about persisted truth rather
    than about what the run said it did.
    """
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine
    from platform_core.identity import lease_service
    from platform_core.retrieval.hybrid import PrincipalScope

    tid = uuid.UUID(TENANT)
    # Caller-supplied when a test needs the conversation to already be linked
    # to a case; fresh otherwise, so runs stay independent by default.
    conv = conversation_ref_id or uuid.uuid4()
    principal = PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",))

    engine = create_engine(APP_URL)
    factory = _factory(engine)

    async with factory() as session:
        await _with_ctx(session, TENANT)
        lease = await lease_service.acquire_or_get(session, tenant_id=tid, conversation_ref_id=conv)
        await session.commit()
    expected_version = int(lease.lease_version)

    async with factory() as session:
        await _with_ctx(session, TENANT)
        orch = AgentOrchestrator(
            session,
            OrchestratorDeps(
                channel_sender=ChannelSender({"email": _RecordingTransport()}),
                generator=generator,
                tool_factories=tool_factories,
            ),
        )
        outcome = await orch.run(
            tenant_id=tid,
            conversation_ref_id=conv,
            question=question,
            principal=principal,
            expected_lease_version=expected_version,
            channel_system="email",
            channel_address="buyer@example.test",
            channel_conversation_key="1",
        )
        await session.commit()

    async with factory() as session:
        await _with_ctx(session, TENANT)
        proposals = (
            (
                await session.execute(
                    text(
                        "SELECT p.status, p.required_confirmation, p.sanitized_input, "
                        "d.name AS tool_name, d.risk "
                        "FROM tool_proposals p JOIN tool_definitions d "
                        "ON d.id = p.tool_definition_id WHERE p.tenant_id = :t"
                    ),
                    {"t": TENANT},
                )
            )
            .mappings()
            .all()
        )
        executions = (
            (
                await session.execute(
                    text(
                        "SELECT status, verification_status, error_code FROM tool_executions "
                        "WHERE tenant_id = :t"
                    ),
                    {"t": TENANT},
                )
            )
            .mappings()
            .all()
        )
        confirmations = (
            await session.execute(
                text("SELECT count(*) AS n FROM action_confirmations WHERE tenant_id = :t"),
                {"t": TENANT},
            )
        ).scalar_one()
        citations = (
            (
                await session.execute(
                    text(
                        "SELECT source_uri, document_version_id FROM citations WHERE tenant_id = :t"
                    ),
                    {"t": TENANT},
                )
            )
            .mappings()
            .all()
        )

    await engine.dispose()
    return outcome, proposals, executions, int(confirmations), citations


class _RecordingTransport:
    """Channel transport double: records every outbound answer.

    It replaced a Chatwoot-shaped `sender` double. The channel path is the only
    path that still leaves the platform, so it is the only delivery an outside
    observer can see; the platform's own surface delivers by persisting the
    agent turn, which these tests read back from the database.
    """

    system = "email"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send(self, *, address, conversation_key, content, command_id):
        self.calls.append(
            {
                "address": address,
                "conversation_key": conversation_key,
                "content": content,
                "command_id": command_id,
            }
        )
        return SendResult()

        class _Result:
            ambiguous = False

        return _Result()


class _FakeNotificationAdapter:
    """A low-risk write adapter that reports a verified postcondition."""

    def __init__(self, context) -> None:
        self.context = context
        self.calls: list[tuple[str, dict, str]] = []

    async def execute(self, tool_name, parameters, idempotency_key):
        self.calls.append((tool_name, dict(parameters), idempotency_key))
        return {"ok": True, "delivered": True}

    async def verify_postcondition(self, tool_name, parameters, output):
        return True


def _notification_factories(adapter: _FakeNotificationAdapter):
    from platform_core.tool_gateway.registry import AdapterFactory

    return {"im_webhook": AdapterFactory(provider="im_webhook", build=lambda _ctx: adapter)}


class _ReceiptCitingGenerator:
    """Cites the first piece of evidence, so the write receipt is what the
    reply stands on. Bypasses the model; the generator's own contract is
    covered by `tests/unit/agent_runtime/test_generator.py`."""

    def __init__(self) -> None:
        from platform_core.agent_runtime.prompts import KNOWLEDGE_QA_PROMPT

        self.template = KNOWLEDGE_QA_PROMPT

    async def generate(self, question, evidence, **kwargs):
        from platform_core.agent_runtime.qa_path import DraftAnswer

        assert evidence, "a verified write must hand its receipt to the generator"
        return DraftAnswer(
            text="I have notified the on-call engineer.",
            claims={0: [evidence[0].chunk_id]},
        )


# --- The safety property ---------------------------------------------------


def test_a_confirmed_write_is_proposed_and_never_approved() -> None:
    """The whole point of the write path, in one test.

    The agent proposes a `confirmed_write` and stops. Asserted from the rows:
    the proposal exists and is `authorized` with `required_confirmation`, no
    confirmation exists for it, and nothing executed. The agent has no code
    path to `confirm`, and the only route that creates a confirmation requires
    CASE_UPDATE, which the agent's role does not hold — so "a human approves"
    is a control rather than a convention.
    """
    _enable_write_flag()
    _seed_connector(
        provider="jira", capabilities=["create_issue"], configuration={"default_project": "HQ"}
    )

    outcome, proposals, executions, confirmations, _citations = _run(_execute_write_run())

    assert outcome.handoff is True
    assert outcome.abstain_reason == "TOOL_CONFIRMATION_PENDING"

    assert len(proposals) == 1, proposals
    proposal = proposals[0]
    assert proposal["tool_name"] == "jira.create_issue"
    assert proposal["risk"] == "confirmed_write"
    assert proposal["status"] == "authorized"
    assert proposal["required_confirmation"] is True
    # The customer's own words become the summary; the project comes from the
    # tenant's connector configuration rather than from the customer.
    assert proposal["sanitized_input"]["project"] == "HQ"
    assert proposal["sanitized_input"]["summary"] == WRITE_REQUEST

    assert confirmations == 0, "the agent approved its own proposal"
    assert executions == [], "a confirmed write must not execute before a human approves it"


def test_the_natural_way_a_customer_asks_reaches_the_write_path() -> None:
    """Why `create` was added to `ACTION_VERBS`.

    The ticket tools were reachable only through "escalate", so a customer
    asking in the words they actually use - create, report, raise, file -
    routed to the knowledge path, which refuses action requests. The whole
    propose-and-confirm flow existed and nothing could trigger it. This is the
    end-to-end assertion that the classifier change reached the orchestrator
    and not just the unit tests.
    """
    _enable_write_flag()
    _seed_connector(
        provider="jira", capabilities=["create_issue"], configuration={"default_project": "HQ"}
    )
    question = "Please create a ticket for this defect in the rev C board."

    outcome, proposals, executions, confirmations, _citations = _run(
        _execute_write_run(question=question)
    )

    assert outcome.handoff is True
    assert outcome.abstain_reason == "TOOL_CONFIRMATION_PENDING"
    assert len(proposals) == 1, proposals
    assert proposals[0]["tool_name"] == "jira.create_issue"
    assert proposals[0]["sanitized_input"]["summary"] == question
    assert confirmations == 0
    assert executions == []


def test_the_write_path_is_not_taken_while_the_flag_is_off() -> None:
    """Flag off means the old behaviour, byte for byte.

    No connector, no flag, no proposal — and the run still refuses the action
    request through the knowledge path, which is what it did before this
    existed. A write path that activated itself would be a change nobody
    asked for.
    """
    _seed_connector(
        provider="jira", capabilities=["create_issue"], configuration={"default_project": "HQ"}
    )

    outcome, proposals, executions, confirmations, _citations = _run(_execute_write_run())

    assert proposals == []
    assert executions == []
    assert confirmations == 0
    assert outcome.handoff is True
    assert outcome.abstain_reason != "TOOL_CONFIRMATION_PENDING"


# --- Degradations, each one a distinct handoff reason ----------------------


def test_no_connected_system_hands_off_without_writing_a_proposal() -> None:
    """A tenant that has connected nothing must not get a proposal row.

    The distinction matters to an operator: "we proposed nothing because
    there is nothing to propose through" is a configuration gap, while a
    pending proposal is work waiting on a person.
    """
    _enable_write_flag()

    outcome, proposals, _executions, _confirmations, _citations = _run(_execute_write_run())

    assert outcome.handoff is True
    assert outcome.abstain_reason == "TOOL_UNAVAILABLE"
    assert proposals == []


def test_a_connector_without_the_configured_default_hands_off() -> None:
    """No `default_project` means no proposal, not a guessed one.

    A ticket filed into the wrong project is worse than a ticket never filed,
    and the failure would be invisible until somebody noticed the issue in a
    board it does not belong to.
    """
    _enable_write_flag()
    _seed_connector(provider="jira", capabilities=["create_issue"], configuration={})

    outcome, proposals, _executions, _confirmations, _citations = _run(_execute_write_run())

    assert outcome.handoff is True
    assert outcome.abstain_reason == "TOOL_ARGUMENT_MISSING"
    assert proposals == []


# --- The low-risk half: a write the catalog says needs no confirmation -----


def test_a_low_write_executes_and_verifies_without_a_confirmation() -> None:
    """`low_write` is the class that exists to run unattended.

    Only an IM connector is seeded, so the ticket tool is unreachable and the
    notification wins. The assertions are about the rows: the execution
    reached `executed` with a verified postcondition, and no confirmation was
    needed or created. The adapter is injected because the alternative is a
    live Slack workspace, which in practice means this branch is exercised by
    nothing.
    """
    _enable_write_flag()
    _seed_connector(
        provider="im_webhook",
        capabilities=["send_notification"],
        configuration={"default_channel": "#oncall"},
    )
    adapter = _FakeNotificationAdapter(None)

    outcome, proposals, executions, confirmations, citations = _run(
        _execute_write_run(
            tool_factories=_notification_factories(adapter),
            generator=_ReceiptCitingGenerator(),
        )
    )

    assert len(proposals) == 1, proposals
    assert proposals[0]["tool_name"] == "im.send_notification"
    assert proposals[0]["risk"] == "low_write"
    assert proposals[0]["required_confirmation"] is False
    assert proposals[0]["sanitized_input"]["channel"] == "#oncall"

    assert len(executions) == 1, executions
    assert executions[0]["status"] == "executed"
    assert executions[0]["verification_status"] == "verified"
    assert confirmations == 0

    assert len(adapter.calls) == 1, adapter.calls
    assert adapter.calls[0][0] == "im.send_notification"
    assert adapter.calls[0][1]["text"] == WRITE_REQUEST

    # A write that ran is not a handoff: the customer is owed an answer about
    # what the platform did. That answer is grounded in the verified receipt,
    # which arrives through the evidence channel rather than through a special
    # case in the generator.
    assert outcome.handoff is False
    assert outcome.answer_text == "I have notified the on-call engineer."
    assert outcome.citation_count == 1
    assert len(citations) == 1, citations
    assert citations[0]["source_uri"].startswith("tool://im.send_notification")
    # A receipt is not a document, so it must not be attributed to one - a
    # citation pointing at a document_version_id would claim the reply came
    # from the knowledge base.
    assert citations[0]["document_version_id"] is None


# --- The EQ confirmation: the agent's job is to relay, not to record -------


def _seed_eq_case(
    *,
    conversation_ref_id: uuid.UUID,
    status: str = "waiting_customer",
    category: str = "eq_confirmation",
) -> str:
    """A case in the state the confirmation flow waits for, linked to a conversation."""
    case_id = str(uuid.uuid4())
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, subject, description, status, priority, "
                "category, version, opened_at, elapsed_running_seconds, "
                "last_state_changed_at) VALUES (:i, :t, 'EQ 70001: confirm stackup', '', "
                ":s, 'p2', :c, 1, 0, 0, 0)"
            ),
            {"i": case_id, "t": TENANT, "s": status, "c": category},
        )
        conn.execute(
            text(
                "INSERT INTO case_conversations (id, tenant_id, case_id, "
                "conversation_ref_id, relationship) VALUES "
                "(gen_random_uuid(), :t, :c, :conv, 'origin')"
            ),
            {"t": TENANT, "c": case_id, "conv": str(conversation_ref_id)},
        )
    admin.dispose()
    return case_id


def test_a_confirmation_in_a_linked_conversation_hands_off_to_a_human() -> None:
    """The agent relays and collects; recording the confirmation is not its act.

    "确认，按这个方案生产" carries no verb and no object, so it never classifies
    as a write request. Left alone the QA path would either answer it from the
    corpus or ask a pointless clarification, both wrong for a customer who has
    just answered the question the platform asked.

    What it must **not** do is record it. `case.eq_confirm` is
    `human_approval`, which the policy engine keeps unreachable by the agent at
    every stage including propose, because the case status is what the factory
    reads and a customer's word in a conversation is not a production release.
    """
    _enable_write_flag()
    conv = uuid.uuid4()
    _seed_eq_case(conversation_ref_id=conv)

    outcome, proposals, executions, confirmations, _citations = _run(
        _execute_write_run(question="确认，按这个方案生产", conversation_ref_id=conv)
    )

    assert outcome.handoff is True
    assert outcome.abstain_reason == "EQ_CONFIRMATION_REQUIRES_HUMAN"
    # The point: it handed off *instead of* proposing.
    assert proposals == []
    assert confirmations == 0
    assert executions == []


def test_the_agent_cannot_propose_a_human_approval_tool() -> None:
    """The class is the control, and it is asserted rather than assumed.

    `integration_service` does not hold `tool.human_approval`, so a proposal
    for this tool is refused at the gateway. This pins the pair together: if
    `case.eq_confirm` is ever downgraded to `confirmed_write` - which would put
    it back in reach of the agent - this fails, and the handoff test above
    would fail with it.
    """
    from platform_core.tool_gateway.registry import TOOL_CATALOG

    risk, _schema, permissions, _requires_confirmation = TOOL_CATALOG["case.eq_confirm"]

    assert risk == "human_approval"
    assert permissions == ["tool.human_approval"]


def test_a_confirmation_with_no_linked_case_is_left_alone() -> None:
    """The context is the whole signal, so without it nothing happens.

    An "ok" in a conversation that is not waiting on anything is a pleasantry,
    and handing off for it would be handing a question to a human over nothing.
    """
    _enable_write_flag()

    outcome, proposals, _executions, _confirmations, _citations = _run(
        _execute_write_run(question="确认，按这个方案生产")
    )

    assert outcome.abstain_reason != "EQ_CONFIRMATION_REQUIRES_HUMAN"
    assert proposals == []


def test_a_linked_case_that_is_not_waiting_is_left_alone() -> None:
    """Linked is not enough: the case must still be waiting on the customer."""
    _enable_write_flag()
    conv = uuid.uuid4()
    _seed_eq_case(conversation_ref_id=conv, status="in_progress")

    outcome, _proposals, _executions, _confirmations, _citations = _run(
        _execute_write_run(question="确认", conversation_ref_id=conv)
    )

    assert outcome.abstain_reason != "EQ_CONFIRMATION_REQUIRES_HUMAN"


def test_a_linked_case_that_is_not_an_eq_is_left_alone() -> None:
    """The category is the second axis, and it is not implied by the link."""
    _enable_write_flag()
    conv = uuid.uuid4()
    _seed_eq_case(conversation_ref_id=conv, category="general")

    outcome, _proposals, _executions, _confirmations, _citations = _run(
        _execute_write_run(question="确认", conversation_ref_id=conv)
    )

    assert outcome.abstain_reason != "EQ_CONFIRMATION_REQUIRES_HUMAN"


def test_a_negated_confirmation_is_not_a_confirmation() -> None:
    """Contains 确认 and means the opposite."""
    _enable_write_flag()
    conv = uuid.uuid4()
    _seed_eq_case(conversation_ref_id=conv)

    outcome, _proposals, _executions, _confirmations, _citations = _run(
        _execute_write_run(question="先不确认，我们再评估一下", conversation_ref_id=conv)
    )

    assert outcome.abstain_reason != "EQ_CONFIRMATION_REQUIRES_HUMAN"
