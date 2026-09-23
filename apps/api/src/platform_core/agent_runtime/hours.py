"""Is a human actually there? (feature list 7.5)

The failure this prevents is a specific lie: "a colleague will be with you
shortly" sent at 03:00, when the queue will not move for six hours. The
customer stops waiting on the channel and starts waiting on a promise that was
never true, which is worse than being told the truth up front.

Deliberately a plain clock window rather than a holiday calendar or a
per-team roster: the promise this makes ("someone replies from HH:MM") has to
be one the platform can actually keep, and every source of truth it cannot see
is a promise it should not make.

Disabled by default (no window configured means "always open"), so a tenant
that has not told us its hours gets today's behaviour rather than a new
refusal.
"""

from __future__ import annotations

from datetime import UTC, datetime

from platform_core.agent_runtime.language import answers_in_chinese

# Inclusive start, exclusive end, in hours. A window where start == end means
# "no window configured", which is treated as always open rather than as
# "closed all day" - the less surprising reading, and the one that cannot turn
# an unconfigured tenant into a silently refusing one.
#
# The default is deliberately UNCONFIGURED. Shipping a default window silently
# changes what every tenant's customers are told at night, which nobody asked
# for - and on the first attempt it did exactly that: the full suite caught
# three tests whose expected notice changed after 18:00. Settings decide when
# a team is open, not constants.
OPEN_HOUR = 0
CLOSE_HOUR = 0


def _window() -> tuple[int, int]:
    from platform_core.config import get_settings

    settings = get_settings()
    return int(settings.support_open_hour), int(settings.support_close_hour)


def is_open(
    now: datetime | None = None,
    *,
    open_hour: int | None = None,
    close_hour: int | None = None,
) -> bool:
    """True when the configured window covers `now` (UTC)."""
    cfg_open, cfg_close = _window()
    start = cfg_open if open_hour is None else open_hour
    end = cfg_close if close_hour is None else close_hour
    if start == end:
        return True
    moment = now or datetime.now(UTC)
    hour = moment.hour
    if start < end:
        return start <= hour < end
    # A window that wraps midnight (e.g. 22 -> 06).
    return hour >= start or hour < end


def next_open_label(
    now: datetime | None = None,
    *,
    open_hour: int | None = None,
    close_hour: int | None = None,
) -> str:
    """The time the queue opens again, as `HH:MM` (UTC).

    Named a label and returned as text because that is all the customer-facing
    notice needs, and because a timezone we have not been told is not one we
    should guess at - the window is configured in the same clock it is
    reported in.
    """
    if is_open(now, open_hour=open_hour, close_hour=close_hour):
        return ""
    cfg_open, _cfg_close = _window()
    shown = cfg_open if open_hour is None else open_hour
    return f"{shown:02d}:00"


def opening_hour() -> int:
    """The hour the team opens, in the configured window.

    Public accessor for the same number `offline_notice` interpolates, so a
    customer surface can say "we are back at 09:00" without reaching into a
    private function or re-deriving the window. Meaningless on its own when no
    window is configured - `is_open()` is always True then, so a caller that
    checks first never reads this.
    """
    cfg_open, _cfg_close = _window()
    return int(cfg_open)


def offline_notice(open_hour: int | None = None, question: str = "") -> str:
    """What to say instead of "a colleague will help".

    States that no one is there, that the message is kept, and when someone
    will look at it - in that order. It does not apologise for a wait it did
    not cause and does not promise a reply time it cannot bound, because the
    queue depth at opening is not something this platform knows.

    `question` decides the language (see `language.answers_in_chinese`); this
    notice sits in the same customer-visible cluster as the abstention copy, so
    it has to follow the same rule or a Chinese customer is told the team is
    offline in English.
    """
    cfg_open, _cfg_close = _window()
    shown = cfg_open if open_hour is None else open_hour
    if answers_in_chinese(question):
        return (
            "我们的人工团队目前不在线，所以现在没有人能接起这条对话。"
            f"您的消息已经记录在这条对话里，会有同事在 {shown:02d}:00 之后跟进。"
        )
    return (
        "Our team is offline at the moment, so no one can pick this up right "
        f"now. Your message has been logged with this conversation, and "
        f"someone will follow up from {shown:02d}:00."
    )
