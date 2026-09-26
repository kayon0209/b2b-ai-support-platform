"""Unit tests: the customer-visible notices speak the conversation's language.

The defect these tests pin was measured on the customer surface, 2026-09-23.
A Chinese-speaking customer asked 我的订单到哪了？, was asked for the order
number, and replied `SO-9001`. That reply carries no CJK character, so every
notice that decided its language from the last message alone answered in
English - inside an otherwise entirely Chinese conversation, about a reply the
platform had just asked for.

`language.conversation_is_chinese` is the rule; these tests hold each
customer-visible producer to it, because four modules produce notices and they
must not disagree (that disagreement is what the language module exists to
prevent).
"""

from __future__ import annotations

from platform_core.agent_runtime.conversation import Turn, TurnRole
from platform_core.agent_runtime.hours import offline_notice
from platform_core.agent_runtime.language import answers_in_chinese, conversation_is_chinese
from platform_core.agent_runtime.orchestrator import _clarify_streak
from platform_core.agent_runtime.qa_path import safe_abstention_text
from platform_core.agent_runtime.queue_status import QueueStatus, queue_notice
from platform_core.agent_runtime.tool_card import glossary_for


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


# --- 1. The rule itself ------------------------------------------------------


def test_any_chinese_message_makes_the_conversation_chinese() -> None:
    """One Chinese turn is enough; a later identifier changes nothing."""
    assert conversation_is_chinese(("我的订单到哪了？", "SO-9001")) is True
    assert conversation_is_chinese(("SO-9001", "我的订单到哪了？")) is True


def test_a_conversation_with_no_script_signal_is_not_chinese() -> None:
    """English stays English - the rule widens, it does not flip."""
    assert conversation_is_chinese(("SO-9001", "where is my order?")) is False
    assert conversation_is_chinese(()) is False
    assert conversation_is_chinese((None, "")) is False


def test_the_single_message_rule_is_what_the_conversation_rule_builds_on() -> None:
    """`answers_in_chinese` stays the primitive; the conversation rule is
    `any` over it. Pinning the relationship so the two cannot silently
    diverge into different definitions of "Chinese"."""
    assert conversation_is_chinese(("板子短路了",)) is answers_in_chinese("板子短路了")
    assert conversation_is_chinese(("SO-9001",)) is answers_in_chinese("SO-9001")


# --- 2. Each producer follows the conversation -------------------------------


def test_the_abstention_reply_follows_the_conversation_not_the_last_message() -> None:
    """The measured failure, as a test.

    `SO-9001` alone decided English; with the Chinese question that prompted
    it in `prior_texts`, the same reply must be answered in Chinese.
    """
    notice = safe_abstention_text("NO_CLAIMS", "SO-9001", prior_texts=("我的订单到哪了？",))
    assert _has_cjk(notice), f"expected Chinese, got: {notice}"
    assert "人工同事" in notice

    # And with no history at all the old per-message behaviour is unchanged:
    # a bare id still reads as English, because there is nothing else to go on.
    bare = safe_abstention_text("NO_CLAIMS", "SO-9001")
    assert not _has_cjk(bare)


def test_the_offline_notice_follows_the_conversation() -> None:
    notice = offline_notice(question="SO-9001", prior_texts=("板子短路了，我要索赔",))
    assert _has_cjk(notice), f"expected Chinese, got: {notice}"
    assert "不在线" in notice
    # Same input without the history: English, as before.
    assert "offline" in offline_notice(question="SO-9001").lower()


def test_the_queue_notice_follows_the_conversation() -> None:
    status = QueueStatus(position=2, ahead=1, estimated_wait_minutes=None, open_now=True)
    notice = queue_notice(status, "SO-9001", prior_texts=("我要索赔",))
    assert notice is not None
    assert _has_cjk(notice), f"expected Chinese, got: {notice}"
    assert "人工队列" in notice

    english = queue_notice(status, "SO-9001")
    assert english is not None
    assert "queue" in english.lower()


# --- 3. The receipt glossary -------------------------------------------------


def test_the_glossary_lists_only_the_states_the_receipt_contains() -> None:
    """The model grounds on the receipt plus this mapping, so a state the
    record does not have must not be offered to it (it would be invited to
    mention one)."""
    receipt = (
        '{"order_no": "SO-9001", "status": "in_production",'
        ' "nodes": [{"label": "cutting", "state": "done"},'
        ' {"label": "etching", "state": "active"}]}'
    )
    glossary = glossary_for(receipt)
    assert "in_production=生产中" in glossary
    assert "done=已完成" in glossary
    assert "active=进行中" in glossary
    # A state the record does not carry is not offered.
    assert "shipped" not in glossary


def test_the_glossary_is_empty_when_there_is_nothing_to_translate() -> None:
    """Empty, not a header with no rows - the caller appends it
    unconditionally."""
    assert glossary_for("not json at all") == ""
    assert glossary_for('{"order_no": "SO-9001"}') == ""
    assert glossary_for('{"status": "some_unmapped_state"}') == ""
    assert glossary_for("") == ""
    assert glossary_for('["not", "an", "object"]') == ""


# --- 4. The clarification escalation counter ---------------------------------


def test_the_clarify_streak_counts_trailing_clarifications() -> None:
    """Two asks in a row is the threshold the read path escalates at; the
    count must be of *trailing* clarifications, so one real answer in between
    starts the conversation over."""
    clarify = Turn(role=TurnRole.AGENT, text="能否提供订单号？", ref="clarify:NEEDS_CLARIFICATION")
    answer = Turn(role=TurnRole.AGENT, text="交期是 5 个工作日。")
    customer = Turn(role=TurnRole.CUSTOMER, text="SO-9001")

    assert _clarify_streak([clarify, clarify, customer]) == 2
    # An agent answer breaks the streak: the platform answered since.
    assert _clarify_streak([clarify, answer, customer]) == 0
    # The customer's own turns never count.
    assert _clarify_streak([customer, customer]) == 0
    assert _clarify_streak([]) == 0
