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

# Inclusive start, exclusive end, in hours. A window where start == end means
# "no window configured", which is treated as always open rather than as
# "closed all day" - the less surprising reading, and the one that cannot turn
# an unconfigured tenant into a silently refusing one.
OPEN_HOUR = 9
CLOSE_HOUR = 18


def is_open(
    now: datetime | None = None,
    *,
    open_hour: int = OPEN_HOUR,
    close_hour: int = CLOSE_HOUR,
) -> bool:
    """True when the configured window covers `now` (UTC)."""
    if open_hour == close_hour:
        return True
    moment = now or datetime.now(UTC)
    hour = moment.hour
    if open_hour < close_hour:
        return open_hour <= hour < close_hour
    # A window that wraps midnight (e.g. 22 -> 06).
    return hour >= open_hour or hour < close_hour


def next_open_label(
    now: datetime | None = None,
    *,
    open_hour: int = OPEN_HOUR,
    close_hour: int = CLOSE_HOUR,
) -> str:
    """The time the queue opens again, as `HH:MM` (UTC).

    Named a label and returned as text because that is all the customer-facing
    notice needs, and because a timezone we have not been told is not one we
    should guess at - the window is configured in the same clock it is
    reported in.
    """
    if is_open(now, open_hour=open_hour, close_hour=close_hour):
        return ""
    return f"{open_hour:02d}:00"


def offline_notice(open_hour: int = OPEN_HOUR) -> str:
    """What to say instead of "a colleague will help".

    States that no one is there, that the message is kept, and when someone
    will look at it - in that order. It does not apologise for a wait it did
    not cause and does not promise a reply time it cannot bound, because the
    queue depth at opening is not something this platform knows.
    """
    return (
        "Our team is offline at the moment, so no one can pick this up right "
        f"now. Your message has been logged with this conversation, and "
        f"someone will follow up from {open_hour:02d}:00."
    )
