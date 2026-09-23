"""Agent directory: who can take work, what they can take, and how much.

The one ordinary thing this platform had no concept of. `cases.assignee_ref`
existed as a free-form string, and `cases/transfer.py` states the omission
plainly: assignment to a *person* is deliberately not modelled. That is fine for
routing to a team and useless for running a queue: without a directory there is
no answer to "who is on shift", "who can read PCB", or "who is already at
capacity" - so every case either sat unowned or was assigned by hand.

What this adds, and only this
------------------------------
A directory of agents with **skills** and **capacity**, plus the two operations
a queue needs: *claim* and *release*. Deliberately not included: a scheduler
that assigns on a timer (a worker job is the right home for that later), and any
notion of shift or working hours (that is `agent_runtime/hours.py`'s job, and
duplicating it here would give two answers to "is anyone there").

Why `user_ref` is an opaque string and not a foreign key
--------------------------------------------------------
`cases.assignee_ref` is already an opaque string, and `users` is a global table
while `memberships` carries the tenant link. Pointing at `users` here would mean
the value written by an assignment and the value stored on a case were two
different shapes needing a translation layer - and a translation layer between
"who owns this" and "who is this" is exactly how they drift apart. One opaque
ref, one shape, no translation.

Skills are a tag list matched against the case's business line and team. An
empty tag list means "accepts anything", which is the correct default: a
deployment that never tags anyone must still be able to assign work.
"""

import enum

from sqlalchemy import BigInteger, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin

MAX_REF = 255
MAX_NAME = 191
# Ceiling on concurrent open cases. A directory entry that claims unlimited
# capacity makes "least loaded" meaningless - every agent ties at zero.
MAX_CONCURRENT_CEILING = 100


class AgentStatus(enum.StrEnum):
    ACTIVE = "active"
    # Off shift or on leave. Kept rather than deleted: an agent with open cases
    # cannot disappear, or those cases become unowned and unexplainable.
    INACTIVE = "inactive"


class AgentProfile(Base, PkMixin, TenantMixin):
    """One agent in one tenant."""

    __tablename__ = "agent_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_ref", name="uq_agent_user"),
        Index("ix_agent_profiles_active", "tenant_id", "status"),
    )

    # Same shape as `cases.assignee_ref` - see the module docstring.
    user_ref: Mapped[str] = mapped_column(String(MAX_REF), nullable=False)
    display_name: Mapped[str] = mapped_column(String(MAX_NAME), nullable=False)
    # Tag list: business lines and team slugs. Empty = accepts anything.
    skills: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    max_concurrent: Mapped[int] = mapped_column(BigInteger, nullable=False, default=5)
    status: Mapped[str] = mapped_column(
        String(15), nullable=False, default=AgentStatus.ACTIVE.value
    )
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
