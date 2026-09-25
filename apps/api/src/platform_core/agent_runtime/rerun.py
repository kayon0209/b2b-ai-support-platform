"""A failed run has to be findable, and a re-run has to be safe.

Not to be confused with `replay.py`
-----------------------------------
That module is a *read*: an operator asking what was said, what the system
decided and why. This one is a *write*: deciding to do it again. The two share
a word in common usage and nothing else, and merging them would be the sort of
ambiguity that produces an endpoint named "replay" which shows a conversation
on one route and sends an email on another.

The gap this closes
-------------------
A failed agent run wrote `status = 'failed'` and returned. No reason, no
record, no way to try again. An operator's whole view was a count in a
dashboard, and the customer whose question went unanswered had no path to an
answer except somebody noticing the number and guessing at it.

What a re-run deliberately is not
---------------------------------
It is not a retry. A retry re-executes the same attempt; this is a human
deciding the first outcome was not good enough, and it leaves the first outcome
intact. So it creates a **new run** pointing back at the old one via
`replay_of_run_id`, and the original stays `failed` forever.

That distinction is the safety story:

- Reusing the failed row would erase the evidence that the attempt failed -
  which is the reason somebody is looking at it - and would let a second
  delivery masquerade as the first.
- The new run is `queued` and has answered nothing, so nothing is sent until a
  worker picks it up and claims it (`agent_runtime/terminal.py`).

The refusals
------------
A re-run that accepts a `completed` run is a send-everything-twice button, so
that is checked first. A second refusal covers the double-click: at most one
*live* re-run per failed run, enforced by a partial unique index as well as
here, because a read-then-write check in the service is exactly the shape two
simultaneous operators defeat.

What is stored about the failure
-------------------------------
The error code, and a truncated detail. A dead-letter table is read by an
operational endpoint and copied into backups, and an exception message can
carry a connection string with a password in it. The code is what an operator
greps for and the part that cannot contain customer content.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.models import AgentRun, RunStatus

# Duplicated rather than imported from `integrations.dead_letter`: that
# constant belongs to a module about connector operations, and an agent-run
# failure importing from there would couple the two domains for no benefit.
MAX_DETAIL = 2_000

# Only these may be re-run. `completed` is excluded on purpose - it has already
# answered the customer. `abandoned` and `superseded` are excluded because
# nothing failed: a re-run would be a second answer to a question nobody asked
# twice.
REPLAYABLE = (RunStatus.FAILED.value,)

# A re-run that has finished does not block the next attempt; the first one may
# have failed for a reason that has since been fixed.
_LIVE = (RunStatus.QUEUED.value, RunStatus.RUNNING.value)


class ReplayRefused(Exception):
    """A re-run was asked for and must not happen.

    Carries a `code` rather than prose so the caller can map it to a status and
    an operator-facing reason, and so a test can assert the specific refusal
    instead of matching on an English sentence.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class RerunRef:
    """What a successful re-run produced."""

    new_run_id: uuid.UUID
    rerun_of: uuid.UUID


async def record_run_failure(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    error_code: str,
    error_detail: str | None = None,
    attempts: int = 0,
    ambiguous: bool = False,
    now: int | None = None,
) -> uuid.UUID:
    """Write the dead letter for a failed run. Returns the dead-letter id.

    Reuses `integrations.dead_letter.record` rather than writing a second,
    agent-shaped table: one table means one operator list, one retention sweep
    and one place to look. The `resource_id` column added in migration 0061 is
    what makes the row resolvable back to the run - the pre-existing
    `operation_digest` cannot do that, and for connector rows it never needed
    to.
    """
    from sqlalchemy import update

    from platform_core.integrations.dead_letter import record
    from platform_core.integrations.models import DeadLetterItem

    ref = await record(
        session,
        tenant_id=tenant_id,
        connector_id=None,
        resource_type="agent_run",
        tool_name="agent_run",
        # Digested, never stored. The run id travels in `resource_id`; this
        # only has to make the row match other agent-run failures of the same
        # kind, which is what an operator comparing repeats actually needs.
        parameters={"run_id": str(run_id), "error_code": error_code},
        error_code=error_code,
        error_detail=(error_detail or "")[:MAX_DETAIL] or None,
        attempts=attempts,
        ambiguous=ambiguous,
        now=now if now is not None else int(time.time()),
    )
    # `record` owns the insert; the pointer is the one thing it cannot know.
    await session.execute(
        update(DeadLetterItem).where(DeadLetterItem.id == ref.item_id).values(resource_id=run_id)
    )
    return ref.item_id


async def rerun_failed_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    actor_ref: str,
    now: int | None = None,
) -> RerunRef:
    """Queue a new run that repeats `run_id`. Raises `ReplayRefused` if it must not.

    `actor_ref` is required rather than optional: a re-run is a human decision
    with a customer-visible consequence, and "who asked" is the first question
    anyone will ask afterwards.
    """
    if not actor_ref.strip():
        raise ReplayRefused("ACTOR_REQUIRED", "a re-run must name the operator who asked")

    from sqlalchemy import select

    original = await session.scalar(
        select(AgentRun).where(AgentRun.id == run_id, AgentRun.tenant_id == tenant_id)
    )
    if original is None:
        raise ReplayRefused("NOT_FOUND", f"no agent run {run_id} for this tenant")
    if original.status not in REPLAYABLE:
        raise ReplayRefused(
            "NOT_REPLAYABLE",
            f"a run in status {original.status!r} has either already answered the "
            f"customer or never failed; repeating it is refused",
        )

    live = await session.scalar(
        select(AgentRun.id).where(
            AgentRun.replay_of_run_id == run_id,
            AgentRun.status.in_(_LIVE),
        )
    )
    if live is not None:
        raise ReplayRefused(
            "ALREADY_IN_FLIGHT",
            f"run {run_id} already has a live re-run ({live}); wait for it or resolve it",
        )

    replacement = AgentRun(
        tenant_id=tenant_id,
        conversation_ref_id=original.conversation_ref_id,
        case_id=original.case_id,
        route=original.route,
        status=RunStatus.QUEUED.value,
        replay_of_run_id=original.id,
        model_config=dict(original.model_config or {}),
        retrieval_config=dict(original.retrieval_config or {}),
        policy_version=original.policy_version,
        code_version=original.code_version,
        trace_id=original.trace_id,
        input_hash=original.input_hash,
        # `started_at` is deliberately left unset. A row that has never been
        # executed is what the retention sweep looks for, and a re-run that
        # inherited the original's timestamp is one it may abandon on sight.
    )
    session.add(replacement)
    await session.flush()
    return RerunRef(new_run_id=replacement.id, rerun_of=original.id)
