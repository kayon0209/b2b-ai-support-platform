"""Issue categories: the operational state of "should this be automated?".

The last mile of the closed loop. Everything upstream of it already exists -
`metrics.automation_candidates` ranks handoff *reasons* and says whether each is
ours to fix, and `gap_service` turns an unanswered question into a reviewed
draft. What was missing is the object an operator acts on: **a category of
question, with a state, that can be marked, worked and then measured**.

Why a table at all, when the statistics are derived: the *statistics* are not
stored anywhere (they are aggregated from `agent_runs`, exactly as feature 8.7's
intent distribution is - no new write path, no backfill). What has to persist is
the **operator's decision** - "we are working on this one", "this one is a human
decision forever". That is human-owned data and nothing else can reconstruct it.

Why `category_key` is derived rather than a foreign key to something: there is
no taxonomy table to point at, and inventing one would make the taxonomy
editable, which is how two tenants end up unable to compare. The key is a pure
function of the run's own classification (see `categories.derive_category`), so
it is reproducible from history and needs no migration when a new scene appears.

The one thing this table must not become: a second place that decides what is
automatable. `fix_type` records what a human *confirmed* the gap was; the
ranking that proposes it lives in `categories.py`.
"""

import enum
import uuid

from sqlalchemy import BigInteger, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class CategoryState(enum.StrEnum):
    """Where a category is in the loop.

        observed ──mark──> candidate ──start──> automating ──live──> automated
           │
           └──────── routing / policy：directly ──────> human_only

    `observed` is the default and means "we are counting it and nobody has
    decided anything". A category only leaves it because a person said so -
    the platform proposes, it does not promote itself.
    """

    OBSERVED = "observed"
    CANDIDATE = "candidate"
    AUTOMATING = "automating"
    AUTOMATED = "automated"
    # Not a failure state. A complaint, a red line or a negotiation is a
    # decision a person must make, and recording that permanently is what keeps
    # the candidate list honest - otherwise the same category is re-proposed
    # every week and reviewers learn to ignore the list.
    HUMAN_ONLY = "human_only"


class FixType(enum.StrEnum):
    """What is missing, which decides what the fix *is*.

    The distinction is the whole reason this object exists: two categories can
    have identical volume and need opposite work. "My order status?" needs a
    document; "change my delivery address" needs a write tool; "this is
    unacceptable" needs a person and always will.

    Ordering below is the ranking used when nothing else separates two
    categories: cheaper and more durable fixes first.
    """

    # A document closes it. Cheapest, and it survives a rule change.
    CONTENT = "content"
    # The platform cannot reach the customer's data - a connector, a credential,
    # or the customer's own proof that the data is theirs.
    DATA = "data"
    # The answer exists but acting on it needs a tool and an approval path.
    ACTION = "action"
    # A person with the right skill must handle it. Automatable *later* by
    # routing to the right team, but never by answering it.
    ROUTING = "routing"
    # A control decided. There is no fix, and proposing one would be undoing
    # the control.
    POLICY = "policy"


# The two fix types a candidate list must never propose automating. Kept as one
# frozenset so the "can this ever be automated" question has a single answer.
NEVER_AUTOMATABLE: frozenset[FixType] = frozenset({FixType.ROUTING, FixType.POLICY})


class IssueCategory(Base, PkMixin, TenantMixin):
    """One category's operational state. One row per (tenant, key)."""

    __tablename__ = "issue_categories"
    __table_args__ = (
        UniqueConstraint("tenant_id", "category_key", name="uq_issue_category"),
        Index("ix_issue_categories_state", "tenant_id", "state"),
    )

    # `{business_line}|{scene}|{primary_kind}`, all three from the run's own
    # intent snapshot. Never empty: a run with no snapshot derives the
    # `unrecorded` key rather than being dropped, because a category that
    # silently vanishes is a category nobody can fix.
    category_key: Mapped[str] = mapped_column(String(191), nullable=False)
    state: Mapped[str] = mapped_column(
        String(31), nullable=False, default=CategoryState.OBSERVED.value
    )
    # What a human confirmed the gap was. Empty until someone says.
    fix_type: Mapped[str] = mapped_column(String(31), nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Who decided, and when. An unattributed state change is not auditable, and
    # "who marked this as automated" is the first question when the rate moves.
    marked_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    marked_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Stamped once when the state first reaches AUTOMATED. The before/after
    # comparison is measured from here - see `categories.category_report`.
    automated_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
