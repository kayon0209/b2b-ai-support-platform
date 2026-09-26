"""The category state machine: the operator's half of the loop.

`categories.py` proposes; this module records what a person decided. The split
is deliberate - a proposal can be recomputed from history, a decision cannot, so
only the decision is persisted.

Transitions are whitelisted rather than free-form. A state machine that accepts
anything is a text field with extra steps, and the two transitions worth
refusing are:

- **`observed → automated`.** Skipping the work is not a state change, it is a
  claim, and it would put a 0%-before/0%-after pair on the report that looks
  like a successful automation.
- **`human_only → automated`.** A category was recorded as a decision a person
  must always make. Promoting it straight to automated is how a red line gets
  automated by a dropdown.

Every transition is attributed (`marked_by`) and stamped (`marked_at`), because
"who marked this automated" is the first question asked when the rate moves.
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.evaluation.category_models import CategoryState, FixType, IssueCategory


class CategoryStateError(ValueError):
    """An illegal transition, or an unknown state/fix type.

    A `ValueError` subclass so a router can map it to a 409 without importing
    the domain, matching how `QueueRefused` and the other refusals behave.
    """


# Which states may follow which. `automated → automating` is allowed on
# purpose: an automation that regressed has to be recordable, and forcing a
# detour through `candidate` would lose the fact that it had ever worked.
ALLOWED_TRANSITIONS: dict[CategoryState, frozenset[CategoryState]] = {
    CategoryState.OBSERVED: frozenset({CategoryState.CANDIDATE, CategoryState.HUMAN_ONLY}),
    CategoryState.CANDIDATE: frozenset(
        {CategoryState.AUTOMATING, CategoryState.HUMAN_ONLY, CategoryState.OBSERVED}
    ),
    CategoryState.AUTOMATING: frozenset(
        {CategoryState.AUTOMATED, CategoryState.CANDIDATE, CategoryState.HUMAN_ONLY}
    ),
    CategoryState.AUTOMATED: frozenset({CategoryState.AUTOMATING, CategoryState.HUMAN_ONLY}),
    CategoryState.HUMAN_ONLY: frozenset({CategoryState.OBSERVED}),
}


def _parse_state(value: str) -> CategoryState:
    try:
        return CategoryState(value)
    except ValueError as exc:
        raise CategoryStateError(f"unknown category state: {value!r}") from exc


def _parse_fix_type(value: str) -> FixType | None:
    if not value:
        return None
    try:
        return FixType(value)
    except ValueError as exc:
        raise CategoryStateError(f"unknown fix type: {value!r}") from exc


async def set_category_state(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    category_key: str,
    state: str,
    actor_id: uuid.UUID | None,
    fix_type: str = "",
    note: str = "",
) -> IssueCategory:
    """Move a category, creating the row if this is the first decision about it.

    The caller owns the transaction and the RLS binding, as elsewhere.

    `automated_at` is stamped on the way into AUTOMATED and **cleared on the way
    out**. Leaving a stale stamp behind would make the report compare a
    category's current rate against a baseline from an automation that was
    reverted, which reads as a regression that never happened.
    """
    target = _parse_state(state)
    # Validate the fix type even when it is not changing, so a bad value fails
    # here rather than being stored and misread by the ranking later.
    _parse_fix_type(fix_type)

    row = (
        await session.execute(
            select(IssueCategory).where(
                IssueCategory.tenant_id == tenant_id,
                IssueCategory.category_key == category_key,
            )
        )
    ).scalar_one_or_none()

    now = int(time.time())
    if row is None:
        if target is not CategoryState.OBSERVED:
            # A first decision may not skip the work - see the module docstring.
            if target is CategoryState.AUTOMATED:
                raise CategoryStateError(
                    "a category cannot be marked automated before it was worked on"
                )
        row = IssueCategory(
            tenant_id=tenant_id,
            category_key=category_key,
            state=CategoryState.OBSERVED.value,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.flush()

    current = _parse_state(row.state)
    if target is not current:
        allowed = ALLOWED_TRANSITIONS[current]
        if target not in allowed:
            raise CategoryStateError(
                f"cannot move a category from {current.value} to {target.value}; "
                f"allowed: {', '.join(sorted(s.value for s in allowed))}"
            )

    row.state = target.value
    if fix_type:
        row.fix_type = fix_type
    if note:
        row.note = note
    row.marked_by = actor_id
    row.marked_at = now
    row.updated_at = now
    if target is CategoryState.AUTOMATED:
        # Only stamp the first time: re-marking must not reset the baseline the
        # before/after comparison is measured from.
        if row.automated_at is None:
            row.automated_at = now
    else:
        row.automated_at = None

    await session.flush()
    return row
