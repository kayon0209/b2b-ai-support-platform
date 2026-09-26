"""Integration: a quality complaint is handed to a person, not answered.

The research report (`docs/research/huaqiu-research.md` 2.1/2.2) classes
compensation as **L6 争议归责**: 必须转人工, with the red line 绝不可做归责表态或
赔付承诺. Measured before this file existed, the platform did the opposite -
"板子短路了，我要索赔" routed to `knowledge_qa` and was answered from the
corpus, and "我要投诉" routed to `business_write` and produced a proposal.

These tests pin the corrected behaviour against real Postgres, because the
claim is about rows and about what was never written: no proposal, no
execution, no citation. A fake session would prove none of that.

Two of the tests are mutation guards for the two judgement calls in
`complaint.py`, and they are the reason this file exists in this shape:
without them, "the detector is narrow" is an assertion about a comment.
"""

from __future__ import annotations

import asyncio
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
APP_URL = os.environ.get(
    "APP_TEST_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)

TENANT = "01900000-0000-7000-8000-0000000000c4"
SLUG = "agent-complaint-handoff"

WRITE_FLAG = "agent.business_write_enabled"

COMPLAINT_REQUIRES_HUMAN = "COMPLAINT_REQUIRES_HUMAN"

# The report's own example (2.3): it must not be answered.
COMPENSATION_CLAIM = "板子短路了，我要索赔"
# Classifies as Route.BUSINESS_WRITE today, so it would produce a proposal.
FILING_A_COMPLAINT = "我要投诉"


def _run(coro):
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
                "(:id, :slug, 'Agent Complaint Handoff', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT, "slug": SLUG},
        )
    admin.dispose()


def _clear() -> None:
    """Children before parents; a leftover row poisons the next run."""
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
        "DELETE FROM case_conversations WHERE tenant_id = :t",
        "DELETE FROM cases WHERE tenant_id = :t",
    )
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for statement in statements:
            conn.execute(text(statement), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": SLUG})
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


async def _execute(
    *,
    question: str,
    enable_write_flag: bool = False,
    attachment_types: list[str] | None = None,
) -> tuple[object, list[dict], dict[str, int]]:
    """One orchestrator run. Returns (outcome, sent messages, row counts)."""
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine
    from platform_core.identity import lease_service
    from platform_core.retrieval.hybrid import PrincipalScope

    if enable_write_flag:
        _enable_write_flag()

    tid = uuid.UUID(TENANT)
    conv = uuid.uuid4()
    principal = PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",))
    sender = _RecordingTransport()

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
            session, OrchestratorDeps(channel_sender=ChannelSender({"email": sender}))
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
            attachment_types=attachment_types,
        )
        await session.commit()

    async with factory() as session:
        await _with_ctx(session, TENANT)
        counts = {
            "proposals": int(
                (
                    await session.execute(
                        text("SELECT count(*) FROM tool_proposals WHERE tenant_id = :t"),
                        {"t": TENANT},
                    )
                ).scalar_one()
            ),
            "executions": int(
                (
                    await session.execute(
                        text("SELECT count(*) FROM tool_executions WHERE tenant_id = :t"),
                        {"t": TENANT},
                    )
                ).scalar_one()
            ),
            "citations": int(
                (
                    await session.execute(
                        text("SELECT count(*) FROM citations WHERE tenant_id = :t"),
                        {"t": TENANT},
                    )
                ).scalar_one()
            ),
        }

    await engine.dispose()
    return outcome, sender.calls, counts


def test_a_compensation_claim_is_handed_off_and_nothing_is_retrieved() -> None:
    """The report's own example, measured as answered before this change.

    The assertions are about what did **not** happen: no citation means
    retrieval never ran, which is the point - the gate sits before it, so there
    is no corpus excerpt in the run that a reviewer could mistake for grounds.
    """
    from platform_core.agent_runtime.qa_path import safe_abstention_text

    outcome, _sent, counts = _run(_execute(question=COMPENSATION_CLAIM))

    assert outcome.handoff is True
    assert outcome.abstain_reason == COMPLAINT_REQUIRES_HUMAN
    # Not "" - the notice is the text, because 弃权必须出声. What it must not
    # contain is anything the knowledge path produced, which is what the
    # citation count below is really asserting.
    #
    # The question is passed because the language of the notice is derived from
    # it (see `language.answers_in_chinese`), and `COMPENSATION_CLAIM` is
    # Chinese - so the expected text is the Chinese one. Calling without it
    # would compare against the English default and assert that a Chinese
    # customer is answered in English, which is the defect the language rule
    # exists to remove.
    assert outcome.answer_text == safe_abstention_text(COMPLAINT_REQUIRES_HUMAN, COMPENSATION_CLAIM)
    assert counts["citations"] == 0
    assert counts["proposals"] == 0
    assert counts["executions"] == 0


def test_a_complaint_never_reaches_the_write_path() -> None:
    """我要投诉 classifies as BUSINESS_WRITE, so it would propose.

    With the write flag **on** - the configuration in which the old behaviour
    was most visible - the run still hands off and proposes nothing. A
    complaint is not a write request; recording one is not the AI's act any
    more than recording an EQ confirmation is.
    """
    outcome, _sent, counts = _run(_execute(question=FILING_A_COMPLAINT, enable_write_flag=True))

    assert outcome.abstain_reason == COMPLAINT_REQUIRES_HUMAN
    assert counts["proposals"] == 0


def test_the_customer_is_told_a_person_will_take_over() -> None:
    """弃权必须出声, and the words must not be the ones that deflect.

    The generic abstention fallback invites the customer to "rephrase the
    question", which to someone claiming compensation reads as being sent away
    - and it is false here, since nothing failed to be found. Asserted rather
    than assumed because abstention-with-silence is already a fixed defect in
    this codebase and would silently regress.
    """
    _outcome, sent, _counts = _run(_execute(question=COMPENSATION_CLAIM))

    customer_visible = [c["content"] for c in sent if c["content"]]
    assert customer_visible, "an abstention that says nothing is a red line"
    notice = customer_visible[0]
    # `COMPENSATION_CLAIM` above is Chinese, so the notice follows it
    # (`language.answers_in_chinese`): "人工同事" is the handoff promise and
    # "换个说法" is the rephrase deflection the fallback would use. The
    # assertions were English-only before the copy was localised.
    assert "人工同事" in notice
    assert "换个说法" not in notice


def test_the_notice_makes_no_payout_promise_and_no_admission() -> None:
    """The red line: 绝不可做归责表态或赔付承诺.

    The handoff text is the only thing the customer sees, so it is the only
    place the AI could commit the company. It must promise no outcome and admit
    no fault - a person decides, and saying more would be deciding for them.
    """
    _outcome, sent, _counts = _run(_execute(question="this is unacceptable, I want compensation"))

    notice = [c["content"] for c in sent if c["content"]][0].lower()
    forbidden = (
        "we will refund",
        "we will compensate",
        "we will pay",
        "you will receive",
        "our fault",
        "we are responsible",
        "we accept liability",
    )
    assert not [phrase for phrase in forbidden if phrase in notice]


@pytest.mark.parametrize(
    "question",
    [
        "全测板开短路不良怎么赔付？",
        # The same question with no trailing punctuation, on purpose. The first
        # form is also saved by the "?" fallback in the detector, so on its own
        # it cannot tell whether the procedure veto is doing any work; this one
        # can, and it is the form a customer actually types into a chat box.
        "你们的赔付政策是什么",
    ],
)
def test_the_policy_question_still_reaches_the_knowledge_path(question: str) -> None:
    """Mutation guard for the procedure veto.

    Both ask how compensation works, which the report puts at L1 (检索 + 引用,
    可直接回答) and which `test_intent.py` pins as TECHNICAL_SUPPORT. Remove the
    veto and these are hijacked - the platform would refuse a documented
    capability to a customer who asked for it, and would do it while claiming
    to be protecting them.
    """
    outcome, _sent, _counts = _run(_execute(question=question))

    assert outcome.abstain_reason != COMPLAINT_REQUIRES_HUMAN


def test_a_stalled_order_is_not_hijacked() -> None:
    """Mutation guard for keying on the claim rather than on the scene.

    This measures as Scene.COMPLAINT, because the scene pattern counts "still
    not" as a complaint signal. If the gate is ever changed to consult the
    scene - the obvious-looking implementation - an ordinary order-status
    question goes to a human queue instead of to the order tool.
    """
    outcome, _sent, _counts = _run(_execute(question="my order has still not arrived"))

    assert outcome.abstain_reason != COMPLAINT_REQUIRES_HUMAN


def test_supplied_evidence_is_named_on_the_handoff() -> None:
    """1.3: don't ask again for what the customer already sent.

    "Please send a photo" when two images are attached is the exchange that
    makes a handoff feel like starting over. The content types travel with the
    handoff so the agent opens the conversation already knowing evidence
    exists - content types only; the files never entered the platform.

    They used to ride in a private Chatwoot note. That transport is gone
    (ADR 0012), so the handoff's audit event carries them - the same record an
    operator or reviewer reads.
    """
    outcome, _sent, _counts = _run(
        _execute(question=COMPENSATION_CLAIM, attachment_types=["image/png", "image/jpeg"])
    )

    assert outcome.abstain_reason == "COMPLAINT_REQUIRES_HUMAN"
    metadata = _handoff_metadata(outcome.run_id)
    assert metadata.get("customer_attachments") == "image/png,image/jpeg", metadata


def _handoff_metadata(run_id: object) -> dict:
    """The handoff's audit metadata: what the receiving side actually reads.

    `after=` on an audit event is hashed and unreadable by design; `metadata`
    is the documented narrow exception for an event's own parameters, which is
    why the handoff's routing facts live there.
    """
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = conn.execute(
            text(
                "SELECT metadata FROM audit_events "
                "WHERE resource_id = :r AND action = 'agent_run.abstained'"
            ),
            {"r": str(run_id)},
        ).first()
    admin.dispose()
    return dict(row[0]) if row and row[0] else {}
