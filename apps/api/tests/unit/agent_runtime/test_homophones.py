"""Feature list 3.8: homophone tolerance in the intent layer.

The load-bearing assertions:

- **A misspelled question routes like the correct one.** "我的定单发货了吗"
  must reach the same scene as "我的订单发货了吗" - before this, the order
  vocabulary simply did not match and the question was classified by whatever
  else it contained.
- **Correct words are not "fixed".** "开发票" and "交货" are valid; a corrector
  that rewrites them changes what the customer asked about. The identity pair
  in the table and a real sentence are both asserted.
- **The correction is recorded, not applied to the customer's words.** Routing
  changed because of a substitution, so the substitution has to be visible -
  and the audit snapshot carries the count, never the text.
"""

from __future__ import annotations

from platform_core.agent_runtime.homophones import (
    HOMOPHONE_FIXES,
    corrections_in,
    normalize_for_matching,
)
from platform_core.agent_runtime.intent import classify


def test_a_homophone_misspelling_is_corrected_for_matching() -> None:
    assert normalize_for_matching("我的定单") == "我的订单"


def test_several_misspellings_in_one_message_are_all_corrected() -> None:
    assert normalize_for_matching("定单的物留") == "订单的物流"


def test_a_correct_message_is_returned_unchanged() -> None:
    assert normalize_for_matching("我要开发票") == "我要开发票"


def test_the_identity_pairs_are_not_applied() -> None:
    """'打样' is spelled correctly; a future edit must not "fix" it."""
    for misspelling, intended in HOMOPHONE_FIXES.items():
        if misspelling == intended:
            assert normalize_for_matching(f"我要{misspelling}") == f"我要{misspelling}"


def test_empty_input_is_safe() -> None:
    assert normalize_for_matching("") == ""
    assert corrections_in("") == ()


def test_the_corrections_are_reported_for_audit() -> None:
    """A routing decision made about the customer's words must be explainable."""
    assert ("定单", "订单") in corrections_in("我的定单发货了吗")


def test_a_correct_message_reports_no_corrections() -> None:
    assert corrections_in("我的订单发货了吗") == ()


def test_a_misspelled_question_reaches_the_order_scene() -> None:
    """The behaviour the feature exists for."""
    assert classify("我的定单发货了吗").scene.value == "order_fulfilment"


def test_the_correct_spelling_reaches_the_same_scene() -> None:
    """Same destination either way - that is what "tolerant" means."""
    assert classify("我的定单发货了吗").scene.value == classify("我的订单发货了吗").scene.value


def test_a_misspelled_refund_request_is_read_as_an_action() -> None:
    assert classify("我要退宽").primary_kind.value == "business_action"


def test_the_audit_snapshot_counts_corrections_without_the_text() -> None:
    """The snapshot is read back without re-exposing what the customer typed."""
    snapshot = classify("我的定单发货了吗").as_dict()
    assert snapshot["spelling_corrections"] == 1
    assert "定单" not in str(snapshot)
    assert "订单" not in str(snapshot)


def test_a_correct_message_records_zero_corrections() -> None:
    assert classify("我的订单发货了吗").as_dict()["spelling_corrections"] == 0
