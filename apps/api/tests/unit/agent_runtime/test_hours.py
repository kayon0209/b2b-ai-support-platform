"""Out-of-hours detection (feature list 7.5).

The failure guarded against is a specific lie: "a colleague will be with you
shortly" at 03:00. So the assertions are about the boundary and about the
default, because both directions are bad - promising a person who is not
there, and refusing to answer a tenant who never said when they are open.
"""

from datetime import UTC, datetime

import pytest

from platform_core.agent_runtime.hours import is_open, next_open_label, offline_notice


def _at(hour: int) -> datetime:
    return datetime(2026, 9, 20, hour, 0, tzinfo=UTC)


def test_the_window_is_inclusive_at_the_start_and_exclusive_at_the_end() -> None:
    # Explicit window: the default is unconfigured (always open) by design, so
    # the boundary is only meaningful against a configured one.
    assert is_open(_at(9), open_hour=9, close_hour=18) is True
    assert is_open(_at(17), open_hour=9, close_hour=18) is True
    # 18:00 is the closing hour, so the window has ended by then - saying
    # "open" here would promise someone at the moment they are leaving.
    assert is_open(_at(18), open_hour=9, close_hour=18) is False
    assert is_open(_at(3), open_hour=9, close_hour=18) is False


def test_a_window_wrapping_midnight_is_handled() -> None:
    assert is_open(_at(23), open_hour=22, close_hour=6) is True
    assert is_open(_at(2), open_hour=22, close_hour=6) is True
    assert is_open(_at(12), open_hour=22, close_hour=6) is False


def test_no_window_configured_means_always_open() -> None:
    """A tenant that never told us its hours must not start refusing.

    start == end is indistinguishable from "unconfigured", and reading it as
    "closed all day" would turn a missing setting into a platform that tells
    every customer nobody is available, ever.
    """
    for hour in (0, 3, 9, 13, 23):
        assert is_open(_at(hour), open_hour=0, close_hour=0) is True


def test_the_opening_time_is_reported_only_when_closed() -> None:
    assert next_open_label(_at(3), open_hour=9, close_hour=18) == "09:00"
    assert next_open_label(_at(12), open_hour=9, close_hour=18) == ""


def test_the_default_is_unconfigured_and_therefore_always_open() -> None:
    """The regression that made this test file worth writing.

    The first version shipped a 9-18 default, which silently changed what
    every tenant's customers were told outside those hours - three unrelated
    tests failed after 18:00 on the day it landed. A tenant that has not told
    us its hours must behave exactly as it did before.
    """
    for hour in (0, 3, 9, 18, 23):
        assert is_open(_at(hour)) is True


def test_the_offline_notice_makes_no_promise_about_speed() -> None:
    """It says no one is there, that the message is kept, and when - not how
    quickly. Queue depth at opening is not something this platform knows, and
    "shortly" at 03:00 is the exact lie this exists to remove."""
    notice = offline_notice(9).lower()

    assert "offline" in notice
    assert "logged" in notice
    assert "09:00" in notice
    for forbidden in ("shortly", "right away", "immediately", "a moment"):
        assert forbidden not in notice


def test_the_window_comes_from_settings_not_constants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring, not the arithmetic.

    Every other test passes a window explicitly, so a settings field that is
    never read would still leave this file green. This one sets the
    configuration and asks the real clock question - which is the only way to
    catch a default that silently overrides what a tenant configured.
    """
    from platform_core.agent_runtime import hours
    from platform_core.config import get_settings

    monkeypatch.setenv("APP_SUPPORT_OPEN_HOUR", "9")
    monkeypatch.setenv("APP_SUPPORT_CLOSE_HOUR", "18")
    get_settings.cache_clear()
    try:
        assert hours.is_open(_at(3)) is False
        assert hours.is_open(_at(12)) is True
        assert hours.next_open_label(_at(3)) == "09:00"
        assert "09:00" in hours.offline_notice()
    finally:
        # Restored for the next test: settings are cached process-wide, and a
        # window left configured here would leak into every later assertion.
        get_settings.cache_clear()


def test_an_external_outage_is_named_as_one() -> None:
    """10.2: degradation should say what is actually wrong.

    "I couldn't verify an answer" is true during an ERP outage and useless -
    the customer is waiting on a system we do not own. Saying so is the entire
    point of a graceful degradation.
    """
    from platform_core.agent_runtime.qa_path import SYSTEM_OUTAGE_REASONS, system_outage_notice

    assert "TOOL_UNAVAILABLE" in SYSTEM_OUTAGE_REASONS
    # A tool that was never configured is a setup gap, not an outage; calling
    # it one would mislead in the other direction.
    assert "TOOL_NO_CANDIDATE" not in SYSTEM_OUTAGE_REASONS

    notice = system_outage_notice().lower()
    assert "not responding" in notice
    # It promises no time and claims no ticket: this path records the run and
    # hands off, and a ticket number nobody filed is the same class of lie as a
    # tool reporting success it never verified.
    for forbidden in ("ticket", "shortly", "in 5 minutes", "resolved"):
        assert forbidden not in notice
