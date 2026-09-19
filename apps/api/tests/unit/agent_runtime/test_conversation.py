"""Unit tests: multi-turn conversation state and context compression.

The behaviour under test is the part a single-turn platform cannot express:
what the model is told when there is a history, and what it is *never* allowed
to forget.
"""

from __future__ import annotations

import pytest

from platform_core.agent_runtime.conversation import (
    CompactedContext,
    ConversationMemory,
    DurableFact,
    Turn,
    TurnRole,
    extract_durable_facts,
    is_continuation,
    needs_clarification,
    pin_reason,
    rewrite_query,
    topic_shifted,
)


def _customer(text: str, ts: int = 0) -> Turn:
    return Turn(role=TurnRole.CUSTOMER, text=text, ts=ts)


def _agent(text: str, ts: int = 0) -> Turn:
    return Turn(role=TurnRole.AGENT, text=text, ts=ts)


def _tool(text: str, ts: int = 0) -> Turn:
    return Turn(role=TurnRole.TOOL, text=text, ts=ts)


# --- Pinning ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I will escalate this to the billing team today.", "commitment"),
        ("The refund was approved by your account manager.", "approval"),
        ("This case has been assigned to Priya.", "ownership"),
        ("We are still waiting on the vendor to respond.", "unresolved"),
    ],
)
def test_obligations_are_pinned(text: str, expected: str) -> None:
    """docs/agent.md: never summarize away commitments, approvals, ownership
    changes or unresolved items.

    Each category is asserted separately because they are pinned for different
    reasons. A single "something was pinned" test would pass if the rule only
    recognised one of the four, and the other three would silently rot.
    """
    assert pin_reason(_agent(text)) == expected


def test_a_tool_turn_is_pinned_regardless_of_its_text() -> None:
    """A bare "ok" from a tool is still an outcome, and an outcome is evidence
    about the world rather than something the assistant said."""
    assert pin_reason(_tool("ok")) == "tool_outcome"


def test_an_ordinary_turn_is_not_pinned() -> None:
    assert pin_reason(_customer("What is the refund window?")) == ""


# --- Compression -----------------------------------------------------------


def test_pinned_lines_survive_a_budget_that_cannot_fit_them() -> None:
    """The pin is unconditional.

    A budget that can drop a commitment is not a budget, it is a bug — so this
    asserts the overrun is visible (`used_chars`) rather than that the budget
    was respected. The caller decides what to do about an overrun; this module
    will not silently truncate an obligation.
    """
    memory = ConversationMemory(
        [
            _customer("What is the refund window?"),
            _agent("I will escalate this to billing today."),
        ],
        budget_chars=10,
    )
    context = memory.compact()
    assert len(context.pinned) == 1
    assert "escalate" in context.pinned[0]
    assert context.used_chars > context.budget_chars


def test_recent_turns_are_kept_verbatim_and_older_ones_summarized() -> None:
    """The recent tail is where anaphora resolves, so it must be word-for-word;
    older turns are summarized rather than dropped outright."""
    memory = ConversationMemory(
        [
            _customer("I was charged twice for the March invoice."),
            _agent("Let me check that for you."),
            _customer("Any update?"),
            _agent("The duplicate charge has been reversed."),
        ],
        recent_turns=2,
    )
    context = memory.compact()
    assert len(context.recent) == 2
    assert context.recent[-1].text == "The duplicate charge has been reversed."
    assert context.dropped_turns == 2
    assert "charged twice" in context.summary


def test_a_pinned_turn_in_the_tail_is_not_counted_twice() -> None:
    """A pinned turn is carried as a pin; including it again in the recent tail
    would spend the budget twice on the same sentence."""
    memory = ConversationMemory([_agent("I will follow this up tomorrow.")])
    context = memory.compact()
    assert len(context.pinned) == 1
    assert context.recent == []


def test_an_empty_memory_compacts_to_nothing() -> None:
    context = ConversationMemory([]).compact()
    assert context.recent == []
    assert context.summary == ""
    assert context.pinned == []
    assert context.used_chars == 0


def test_the_stored_window_is_bounded() -> None:
    """Without a cap a long conversation grows without bound and every
    compaction walks the whole history."""
    memory = ConversationMemory([], max_turns=3)
    for index in range(10):
        memory.add(_customer(f"message {index}"))
    assert len(memory.turns) == 3
    assert memory.turns[-1].text == "message 9"


def test_eviction_reports_lost_pins() -> None:
    """A pinned line falling out of the window is an obligation the platform
    has dropped. It must be countable, not silent."""
    memory = ConversationMemory([], max_turns=2)
    memory.add(_agent("I will refund this by Friday."))
    memory.add(_customer("thanks"))
    assert memory.add(_customer("hello again")) == 1


def test_eviction_of_an_unpinned_turn_reports_nothing() -> None:
    memory = ConversationMemory([], max_turns=2)
    memory.add(_customer("hi"))
    memory.add(_customer("hello"))
    assert memory.add(_customer("hey")) == 0


# --- Durable facts ---------------------------------------------------------


def test_a_customer_stated_fact_is_extracted() -> None:
    facts = extract_durable_facts([_customer("my plan is annual", ts=5)])
    assert facts == (DurableFact(key="plan", value="annual", source_turn=0, ts=5),)


def test_a_later_statement_wins_over_an_earlier_one() -> None:
    """The customer correcting themselves is the normal case: current
    expression beats stored history."""
    facts = extract_durable_facts(
        [_customer("my plan is annual"), _customer("sorry, my plan is monthly")]
    )
    assert [f.value for f in facts] == ["monthly"]


def test_a_credential_is_never_stored() -> None:
    """Storing a password because the customer typed one is a liability, not a
    feature. The whole turn is skipped, not partially parsed."""
    facts = extract_durable_facts([_customer("my plan is annual, my password is hunter2")])
    assert facts == ()


def test_a_turn_containing_pii_is_skipped() -> None:
    facts = extract_durable_facts([_customer("my plan is annual, reach me at a@b.com")])
    assert facts == ()


def test_an_assistant_claim_is_not_memory() -> None:
    """Letting the model's own output become its next input is the feedback
    loop AGENTS.md forbids. Only the customer's statements count."""
    facts = extract_durable_facts([_agent("your plan is annual")])
    assert facts == ()


# --- Topic shift and rewriting ---------------------------------------------


def test_a_follow_up_is_not_a_topic_shift() -> None:
    prior = [_customer("How long is the refund window on an annual plan?")]
    assert topic_shifted(prior, "And what about monthly?") is False


def test_an_unrelated_question_is_a_topic_shift() -> None:
    prior = [_customer("How long is the refund window on an annual plan?")]
    assert topic_shifted(prior, "Do you ship to Germany?") is True


def test_a_continuation_with_no_history_is_not_a_shift() -> None:
    assert topic_shifted([], "Do you ship to Germany?") is False


@pytest.mark.parametrize(
    "question",
    ["how about it?", "and that?", "monthly?", "the enterprise one?"],
)
def test_continuations_are_recognised(question: str) -> None:
    assert is_continuation(question) is True


def test_a_full_question_is_not_a_continuation() -> None:
    assert is_continuation("How long is the refund window on an annual plan?") is False


def test_an_anaphoric_question_inherits_the_antecedent() -> None:
    """The subject is restored lexically, and the customer's own words are kept
    at the end so nothing that would have matched before stops matching."""
    prior = [_customer("How long is the refund window on an annual plan?")]
    rewritten, changed = rewrite_query("And what about monthly?", prior)
    assert changed is True
    assert "refund" in rewritten
    assert rewritten.endswith("And what about monthly?")


def test_a_rewrite_is_refused_when_the_topic_has_shifted() -> None:
    """Carrying the old subject into a new topic answers a question nobody
    asked — which is worse than not rewriting at all."""
    prior = [_customer("How long is the refund window on an annual plan?")]
    rewritten, changed = rewrite_query("Do you ship to Germany?", prior)
    assert changed is False
    assert rewritten == "Do you ship to Germany?"


def test_no_rewrite_without_a_usable_antecedent() -> None:
    rewritten, changed = rewrite_query("monthly?", [])
    assert changed is False
    assert rewritten == "monthly?"


# --- Clarification ---------------------------------------------------------


def test_a_vague_message_asks_for_detail() -> None:
    needs_ask, reason = needs_clarification("it?", [])
    assert needs_ask is True
    assert reason == "QUESTION_TOO_SHORT"


def test_a_follow_up_with_no_history_asks_for_detail() -> None:
    """A continuation with nothing to continue from cannot be resolved, so the
    only way forward is to ask."""
    needs_ask, reason = needs_clarification("and for the enterprise plan?", [])
    assert needs_ask is True
    assert reason == "FOLLOW_UP_WITHOUT_ANTECEDENT"


def test_a_follow_up_with_history_is_not_underspecified() -> None:
    """Asking a customer to repeat what they already said is the expensive
    failure here; the antecedent resolves it."""
    prior = [_customer("How long is the refund window on an annual plan?")]
    assert needs_clarification("and for monthly?", prior) == (False, "")


def test_a_real_question_does_not_ask() -> None:
    assert needs_clarification("How long is the refund window?", []) == (False, "")


# --- Rendering -------------------------------------------------------------


def test_render_orders_obligations_before_narrative() -> None:
    """Order follows docs/agent.md's priority list: what must not be
    contradicted comes first, the recent tail last, nearest the question."""
    context = CompactedContext(
        recent=[_customer("any update?")],
        summary="- customer: I was charged twice",
        pinned=["commitment: I will escalate this today"],
        durable_facts=(DurableFact(key="plan", value="annual", source_turn=0),),
        dropped_turns=1,
        topic_shift=False,
        budget_chars=1500,
        used_chars=200,
    )
    rendered = context.render()
    assert rendered.index("Standing items") < rendered.index("Known about")
    assert rendered.index("Known about") < rendered.index("Earlier in")
    assert rendered.index("Earlier in") < rendered.index("Recent turns")


def test_the_audit_snapshot_carries_no_customer_text() -> None:
    """The run already stores an input hash and the audit log owns what was
    said; a second copy would be a second thing to redact and to retain."""
    context = CompactedContext(
        recent=[_customer("my card number is 4111 1111 1111 1111")],
        summary="- customer: my card number is 4111",
        pinned=[],
        durable_facts=(),
        dropped_turns=1,
        topic_shift=False,
        budget_chars=1500,
        used_chars=100,
    )
    snapshot = context.as_dict()
    assert "4111" not in str(snapshot)
    assert snapshot["turns_kept"] == 1
    assert snapshot["turns_summarized"] == 1


def test_an_empty_context_renders_to_nothing() -> None:
    """A prompt that advertises a section that is not there invites the model
    to infer one."""
    context = CompactedContext(
        recent=[],
        summary="",
        pinned=[],
        durable_facts=(),
        dropped_turns=0,
        topic_shift=False,
        budget_chars=1500,
        used_chars=0,
    )
    assert context.render() == ""
