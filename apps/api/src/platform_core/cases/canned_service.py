"""Canned reply service: create, find, use.

Read and write are separated deliberately. **Any agent may read and use** a
reply (`CASE_READ`); **only an administrator may change one** (`TENANT_ADMIN`),
because a shared template edits what every agent says at once. That split is
enforced in the router, but the service is written so the difference is visible
here too: `use_canned` only ever increments, it never mutates content.

`use_canned` is a single-statement UPDATE ... RETURNING rather than
read-then-write. Two agents inserting the same reply into two conversations in
the same second is normal traffic, and a read-modify-write would silently lose
one of the counts - a "usage" number that is quietly wrong is worse than no
number, because it is the ordering key for the picker.
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.cases.canned_models import (
    MAX_BODY,
    MAX_SHORTCUT,
    MAX_TITLE,
    CannedReply,
)

# Archived replies are invisible to the picker by default, and an explicit flag
# is required to see them. A boolean argument defaulting to False rather than a
# separate "active" query: one code path, and the unsafe one has to be asked for.
DEFAULT_INCLUDE_ARCHIVED = False


class CannedReplyError(ValueError):
    """A refused write. Mapped to 409 by the router."""


def _clean(value: str | None, max_len: int) -> str:
    return (value or "").strip()[:max_len]


async def list_canned(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    business_line: str | None = None,
    team_ref: str | None = None,
    locale: str | None = None,
    include_archived: bool = DEFAULT_INCLUDE_ARCHIVED,
    limit: int = 200,
) -> list[CannedReply]:
    """Replies visible to an agent, most-used first.

    Scope filtering is **inclusive of the unscoped**: a reply with no business
    line is usable from any queue, so filtering to `pcb` returns the PCB replies
    *and* the general ones. Filtering to exactly-matching rows instead would
    hide the general replies from the agents who most need them.

    `locale` behaves the same way - an untagged reply matches every language.
    """
    stmt = select(CannedReply).where(CannedReply.tenant_id == tenant_id)
    if not include_archived:
        stmt = stmt.where(CannedReply.archived.is_(False))
    if business_line:
        stmt = stmt.where(CannedReply.business_line.in_([business_line, ""]))
    if team_ref:
        stmt = stmt.where(CannedReply.team_ref.in_([team_ref, ""]))
    if locale:
        stmt = stmt.where(CannedReply.locale.in_([locale, ""]))

    return list(
        (
            await session.execute(
                stmt.order_by(
                    CannedReply.usage_count.desc(),
                    CannedReply.last_used_at.desc().nulls_last(),
                    CannedReply.title,
                ).limit(max(1, min(limit, 500)))
            )
        )
        .scalars()
        .all()
    )


async def create_canned(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    title: str,
    body: str,
    actor_id: uuid.UUID | None,
    shortcut: str | None = None,
    business_line: str = "",
    team_ref: str = "",
    locale: str = "",
) -> CannedReply:
    """Create a reply. The caller owns the transaction and the RLS binding."""
    clean_title = _clean(title, MAX_TITLE)
    clean_body = _clean(body, MAX_BODY)
    if not clean_title:
        raise CannedReplyError("a canned reply needs a title")
    if not clean_body:
        raise CannedReplyError("a canned reply needs a body")

    clean_shortcut = _clean(shortcut, MAX_SHORTCUT) or None
    if clean_shortcut:
        # Normalise to the form an agent types: no leading slash stored, so
        # `/eta` and `eta` cannot become two rows that look like one.
        clean_shortcut = clean_shortcut.lstrip("/").strip() or None
        if not clean_shortcut:
            raise CannedReplyError("a shortcut must contain more than a slash")

    row = CannedReply(
        tenant_id=tenant_id,
        title=clean_title,
        body=clean_body,
        shortcut=clean_shortcut,
        business_line=_clean(business_line, 31),
        team_ref=_clean(team_ref, 63),
        locale=_clean(locale, 15),
        created_by=actor_id,
        created_at=int(time.time()),
        updated_at=int(time.time()),
    )
    session.add(row)
    await session.flush()
    return row


async def update_canned(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    reply_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    title: str | None = None,
    body: str | None = None,
    shortcut: str | None = None,
    business_line: str | None = None,
    team_ref: str | None = None,
    locale: str | None = None,
    archived: bool | None = None,
) -> CannedReply:
    """Change a reply. Only the fields actually supplied.

    `archived` is here rather than a separate delete: see the model docstring.
    """
    del actor_id  # Attribution is on the row; an edit does not re-author it.
    row = (
        await session.execute(
            select(CannedReply).where(
                CannedReply.tenant_id == tenant_id, CannedReply.id == reply_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise CannedReplyError("no such canned reply")

    if title is not None:
        clean = _clean(title, MAX_TITLE)
        if not clean:
            raise CannedReplyError("a canned reply needs a title")
        row.title = clean
    if body is not None:
        clean = _clean(body, MAX_BODY)
        if not clean:
            raise CannedReplyError("a canned reply needs a body")
        row.body = clean
    if shortcut is not None:
        row.shortcut = _clean(shortcut, MAX_SHORTCUT).lstrip("/").strip() or None
    if business_line is not None:
        row.business_line = _clean(business_line, 31)
    if team_ref is not None:
        row.team_ref = _clean(team_ref, 63)
    if locale is not None:
        row.locale = _clean(locale, 15)
    if archived is not None:
        row.archived = bool(archived)

    row.updated_at = int(time.time())
    await session.flush()
    return row


async def use_canned(
    session: AsyncSession, *, tenant_id: uuid.UUID, reply_id: uuid.UUID
) -> CannedReply | None:
    """Record one use and return the reply.

    Single-statement UPDATE ... RETURNING - see the module docstring for why
    this is not read-then-write.
    """
    stmt = (
        update(CannedReply)
        .where(CannedReply.tenant_id == tenant_id, CannedReply.id == reply_id)
        .values(
            usage_count=CannedReply.usage_count + 1,
            last_used_at=int(time.time()),
        )
        .returning(CannedReply)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def resolve_shortcut(
    session: AsyncSession, *, tenant_id: uuid.UUID, shortcut: str
) -> CannedReply | None:
    """The reply an agent gets by typing `/eta`, or None.

    Archived replies never resolve: a shortcut that silently produces a
    withdrawn template is the worst possible failure for this feature, because
    the agent has no reason to reread what they just inserted.
    """
    clean = _clean(shortcut, MAX_SHORTCUT).lstrip("/").strip()
    if not clean:
        return None
    return (
        await session.execute(
            select(CannedReply).where(
                CannedReply.tenant_id == tenant_id,
                CannedReply.shortcut == clean,
                CannedReply.archived.is_(False),
            )
        )
    ).scalar_one_or_none()
