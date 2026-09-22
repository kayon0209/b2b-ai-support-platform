"""Feature list 1.7: queue position and the waiting notice.

The assertions that carry weight are the ones about what is *not* said:

- No estimated wait when no average handling time has been declared. The
  platform has no historical record of how long a human takes (cases carry no
  creation timestamp), so any number here would be invented. A customer acts
  on an estimate - they go and do something else - so an undefendable one is
  worse than none.
- Nothing at all when the conversation is not queued. Telling someone who is
  not waiting that they are first in line buys nothing and costs trust.

Both are pinned because both are easy to "improve" later by adding a guess,
and the guess would look like a feature.
"""

from __future__ import annotations

import pytest

from platform_core.agent_runtime.queue_status import (
    _AVG_HANDLE_ENV,
    QueueStatus,
    _avg_handle_minutes,
    queue_notice,
)


def _status(ahead: int, minutes: int | None, *, open_now: bool = True) -> QueueStatus:
    return QueueStatus(
        position=ahead + 1,
        ahead=ahead,
        estimated_wait_minutes=minutes,
        open_now=open_now,
    )


def test_a_conversation_that_is_not_queued_gets_no_notice() -> None:
    """Not waiting means not told about waiting."""
    notice = queue_notice(
        QueueStatus(position=0, ahead=0, estimated_wait_minutes=None, open_now=True)
    )
    assert notice is None


def test_nobody_ahead_means_being_connected_not_a_queue_number() -> None:
    notice = queue_notice(_status(ahead=0, minutes=None))
    assert notice is not None
    assert "前面还有" not in notice


def test_people_ahead_are_counted() -> None:
    notice = queue_notice(_status(ahead=3, minutes=None))
    assert notice is not None
    assert "前面还有 3 位" in notice


def test_no_estimate_when_no_average_was_declared(monkeypatch) -> None:
    """The no-fabrication guard: a count is given, a time is not."""
    monkeypatch.delenv(_AVG_HANDLE_ENV, raising=False)
    notice = queue_notice(_status(ahead=3, minutes=None))
    assert notice is not None
    assert "预计等待" not in notice


def test_an_estimate_is_given_only_when_there_is_a_source_for_it() -> None:
    status = _status(ahead=2, minutes=15)
    notice = queue_notice(status)
    assert notice is not None
    assert "预计等待约 15 分钟" in notice


def test_average_handling_time_is_read_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(_AVG_HANDLE_ENV, "7")
    assert _avg_handle_minutes() == 7


@pytest.mark.parametrize("value", ["", "0", "soon", "-3", "  "])
def test_unset_zero_and_garbage_all_mean_unknown(monkeypatch, value: str) -> None:
    """Zero and unparseable are "no source", not "immediate service".

    A negative or zero average would otherwise produce an estimate of 0
    minutes, which reads as "someone is with you now".
    """
    monkeypatch.setenv(_AVG_HANDLE_ENV, value)
    assert _avg_handle_minutes() is None


def test_the_environment_variable_name_is_stable() -> None:
    """Operators configure this by name; renaming it silently disables the
    estimate everywhere and looks like the feature never worked."""
    assert _AVG_HANDLE_ENV == "APP_QUEUE_AVG_HANDLE_MINUTES"
