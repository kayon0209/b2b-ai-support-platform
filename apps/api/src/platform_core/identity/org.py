"""EnterpriseAccount and Department: the tenant's own org structure.

Why this module exists in the shape it does
-------------------------------------------
`docs/development-plan.md` lists "Tenant, EnterpriseAccount, Department and
Membership" as one Phase 2 epic, and for a long time two of the four did not
exist. `cases.enterprise_account_id` was a bare `uuid` column: no FK, no target
table, and **no writer** - `CaseCreateIn` did not accept the field and
`create_case` never set it. So "which account is this Case for" was not merely
unanswered, it was unanswerable, and the column could have held any uuid at all
without anything reporting it.

Two decisions are load-bearing and both are enforced by the database rather
than by this module:

- **A parent must be in the same tenant.** Enforced by a composite FK
  (`(parent_id, tenant_id)` -> `(id, tenant_id)`). A single-column FK would
  accept another tenant's row as a parent, and RLS would hide that row rather
  than reject the write - so the child would read as a root, which is
  indistinguishable from a deliberate root.
- **The hierarchy is acyclic.** Enforced by a trigger; this module also
  pre-checks so the caller gets a clean 409 instead of a constraint error.

The application checks exist for the error message, not for the guarantee. That
ordering matters: two concurrent requests can both pass a pre-check, so the
constraint is the authority and the pre-check is courtesy.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.audit import service as audit_service
from platform_core.identity.models import (
    AccountTier,
    ContractStatus,
    Department,
    EnterpriseAccount,
    EnterpriseAccountContact,
)
from platform_core.identity.tenant_context import TenantContext

# How many levels the application walks before giving up. The database stops at
# 32 as well; matching it here means a pathological chain is refused by the
# pre-check with a readable code instead of by the trigger.
MAX_DEPTH = 32

AUDIT_ACCOUNT_CREATED = "enterprise_account.created"
AUDIT_ACCOUNT_UPDATED = "enterprise_account.updated"
AUDIT_DEPARTMENT_CREATED = "department.created"
AUDIT_DEPARTMENT_UPDATED = "department.updated"

VALID_TIERS = frozenset(member.value for member in AccountTier)
VALID_CONTRACT_STATUSES = frozenset(member.value for member in ContractStatus)


class OrgError(Exception):
    """A structure edit that must be refused, with a caller-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def normalise_slug(value: str) -> str:
    """Lowercase, and refuse anything that is not a slug.

    `slug` is what an IdP group mapping or a Jira project binding targets, so
    it must survive a rename of the department. Normalising here (rather than
    accepting mixed case and comparing case-insensitively) keeps the uniqueness
    constraint meaningful: a CHECK constraint in the database requires
    `slug = lower(slug)`, and case-insensitive comparison in the application
    would let two rows describe one department while `UNIQUE` saw two values.
    """
    slug = value.strip().lower()
    if not slug:
        raise OrgError("SLUG_REQUIRED")
    if len(slug) > 63:
        raise OrgError("SLUG_TOO_LONG")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-_")
    if not set(slug) <= allowed or not slug[0].isalnum():
        raise OrgError("SLUG_INVALID", slug)
    return slug


def validate_tier(tier: str) -> str:
    if tier not in VALID_TIERS:
        raise OrgError("TIER_INVALID", tier)
    return tier


def validate_contract_status(status: str) -> str:
    if status not in VALID_CONTRACT_STATUSES:
        raise OrgError("CONTRACT_STATUS_INVALID", status)
    return status


# Constraint (or raised-message) fragment -> the code the caller should see.
#
# The database is the authority for these rules, and the pre-checks above
# cannot be: two concurrent requests can both pass a pre-check and only one
# can win the constraint. Each mapping exists so that the loser gets a 409 with
# a code rather than a 500 with a driver message.
_CONFLICTS: tuple[tuple[str, str], ...] = (
    ("uq_accounts_tenant_crm_ref", "ACCOUNT_CRM_REF_TAKEN"),
    ("uq_departments_tenant_slug", "SLUG_TAKEN"),
    ("ck_accounts_tier", "TIER_INVALID"),
    ("ck_accounts_contract_status", "CONTRACT_STATUS_INVALID"),
    ("ck_departments_slug_lowercase", "SLUG_INVALID"),
    # The acyclicity trigger raises with no constraint name, so the fragment is
    # the message it formats.
    ("cycle in ", "ORG_CYCLE"),
    ("deeper than 32 levels", "ORG_DEPTH_EXCEEDED"),
    ("fk_enterprise_accounts_parent_same_tenant", "ORG_PARENT_NOT_FOUND"),
    ("fk_departments_parent_same_tenant", "ORG_PARENT_NOT_FOUND"),
)


async def _flush(session: AsyncSession) -> None:
    """Flush, translating a constraint violation into a named `OrgError`.

    An unmatched violation is re-raised deliberately. A single catch-all 409
    would present an unhandled integrity error - a genuine bug - as a normal
    outcome the caller should retry, and the driver message is the only thing
    that would have told us which constraint it was.

    The `rollback` is not cosmetic. A failed flush leaves the session unusable,
    so without it the router's later `commit()` raises `PendingRollbackError`
    and the caller gets a 500 instead of the 409 this function just built.
    Rolling back the whole transaction is safe at every call site here: the
    flush is the first statement that can conflict, and the audit record is
    written after it. `begin_nested()` was the first attempt and did not help -
    the same finding as the domain-claim path in `domains.py`.
    """
    try:
        await session.flush()
    except IntegrityError as exc:
        message = str(getattr(exc, "orig", exc))
        for fragment, code in _CONFLICTS:
            if fragment in message:
                await session.rollback()
                raise OrgError(code) from exc
        raise


# --- reads used by other modules --------------------------------------------


async def account_sla_facts(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> tuple[str | None, str | None] | None:
    """`(tier, contract_status)` for an account, or None if this tenant has none.

    The narrow projection is the point. `cases` needs two values to pick an SLA
    policy and must not import this module's ORM models (AGENTS.md forbids
    cross-module model imports), so the seam is a pair of strings rather than a
    row.

    Returning None for another tenant's account id is deliberate and is not
    information loss: RLS cannot see that row, and the caller reports
    ACCOUNT_NOT_FOUND, which is also what a genuinely unknown id produces - so
    the endpoint cannot be used to discover which accounts exist.
    """
    row = (
        await session.execute(
            select(EnterpriseAccount.tier, EnterpriseAccount.contract_status).where(
                EnterpriseAccount.id == account_id,
                EnterpriseAccount.tenant_id == tenant_id,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return (str(row[0]), str(row[1]))


class ContactAccountFacts(NamedTuple):
    """What routing is allowed to know about a contact's account.

    The same narrow-projection rule as `account_sla_facts`: `agent_runtime`
    needs the tier to decide 转人工优先级 and must not import this module's ORM
    models, so it gets three plain values. The account id is included because
    the handoff note names the account the human is being asked about - without
    it a receiving agent sees "a strategic account" and still has to guess which.
    """

    account_id: uuid.UUID
    tier: str
    contract_status: str


async def account_facts_for_contact(
    session: AsyncSession, *, tenant_id: uuid.UUID, external_contact_id: str
) -> ContactAccountFacts | None:
    """The account a Chatwoot contact belongs to, or None if unbound.

    This is the fact tier-driven routing was missing: a conversation arrives as
    a contact, and nothing could say which contract that contact is under.

    Returns None rather than raising for "no binding", which is the common case
    - most contacts are not bound to any account, and treating that as an error
    would make an unbound customer a failure.
    """
    row = (
        await session.execute(
            select(
                EnterpriseAccountContact.enterprise_account_id,
                EnterpriseAccount.tier,
                EnterpriseAccount.contract_status,
            )
            .join(
                EnterpriseAccount,
                EnterpriseAccount.id == EnterpriseAccountContact.enterprise_account_id,
            )
            .where(
                EnterpriseAccountContact.tenant_id == tenant_id,
                EnterpriseAccountContact.external_contact_id == external_contact_id,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return ContactAccountFacts(account_id=row[0], tier=str(row[1]), contract_status=str(row[2]))


async def bind_contact(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    account_id: uuid.UUID,
    external_contact_id: str,
    actor_id: str | None = None,
) -> uuid.UUID:
    """Bind a Chatwoot contact to one of this tenant's accounts.

    One contact, one account: `uq_account_contact_external` enforces it, and a
    second bind for the same contact is a 409 rather than a silent re-point -
    moving a contact between accounts is an unbind followed by a bind, so the
    change is auditable as two events instead of one overwritten row.
    """
    exists = await account_sla_facts(session, tenant_id=ctx.tenant_id, account_id=account_id)
    if exists is None:
        # Not "ACCOUNT_FORBIDDEN": RLS hides another tenant's row, and naming
        # the real reason would let this endpoint enumerate which accounts
        # exist. Same reasoning as `account_sla_facts`' own None.
        raise OrgError("ACCOUNT_NOT_FOUND", str(account_id))

    row = EnterpriseAccountContact(
        tenant_id=ctx.tenant_id,
        enterprise_account_id=account_id,
        external_contact_id=external_contact_id,
        created_by=actor_id,
        created_at=int(time.time()),
    )
    session.add(row)
    await _flush(session)
    return row.id


async def list_contacts(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> list[str]:
    external_ids = (
        await session.execute(
            select(EnterpriseAccountContact.external_contact_id).where(
                EnterpriseAccountContact.tenant_id == tenant_id,
                EnterpriseAccountContact.enterprise_account_id == account_id,
            )
        )
    ).scalars()
    return list(external_ids)


async def unbind_contact(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    external_contact_id: str,
) -> bool:
    """Remove a binding. Returns False when there was nothing to remove."""
    row = (
        await session.execute(
            select(EnterpriseAccountContact).where(
                EnterpriseAccountContact.tenant_id == tenant_id,
                EnterpriseAccountContact.enterprise_account_id == account_id,
                EnterpriseAccountContact.external_contact_id == external_contact_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def department_exists(
    session: AsyncSession, *, tenant_id: uuid.UUID, department_id: uuid.UUID
) -> bool:
    found = (
        await session.execute(
            select(Department.id).where(
                Department.id == department_id, Department.tenant_id == tenant_id
            )
        )
    ).one_or_none()
    return found is not None


async def _assert_parent_ok(
    session: AsyncSession,
    *,
    table: type[Department] | type[EnterpriseAccount],
    tenant_id: uuid.UUID,
    node_id: uuid.UUID | None,
    parent_id: uuid.UUID | None,
) -> None:
    """Refuse a parent that is missing, foreign, or an ancestor of this node.

    The ancestor walk is why this cannot be a single lookup: `parent_id <> id`
    stops the one-node cycle, and the FK stops a foreign parent, but
    A -> B -> A satisfies both.
    """
    if parent_id is None:
        return
    if node_id is not None and parent_id == node_id:
        raise OrgError("ORG_PARENT_IS_SELF")

    seen = 0
    cursor: uuid.UUID | None = parent_id
    while cursor is not None:
        if node_id is not None and cursor == node_id:
            # Reached this node from below: assigning this parent would close
            # the loop.
            raise OrgError("ORG_CYCLE")
        seen += 1
        if seen > MAX_DEPTH:
            raise OrgError("ORG_DEPTH_EXCEEDED")
        parent = (
            await session.execute(
                select(table.parent_id, table.tenant_id).where(table.id == cursor)
            )
        ).one_or_none()
        if parent is None or parent[1] != tenant_id:
            # Missing *or* another tenant's. One code for both: telling them
            # apart would confirm the existence of another tenant's row.
            raise OrgError("ORG_PARENT_NOT_FOUND")
        cursor = parent[0]


# --- EnterpriseAccount ------------------------------------------------------


async def create_account(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    name: str,
    tier: str = AccountTier.STANDARD.value,
    contract_status: str = ContractStatus.ACTIVE.value,
    parent_id: uuid.UUID | None = None,
    external_crm_ref: str | None = None,
    attributes: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> EnterpriseAccount:
    name = name.strip()
    if not name:
        raise OrgError("NAME_REQUIRED")
    validate_tier(tier)
    validate_contract_status(contract_status)
    await _assert_parent_ok(
        session,
        table=EnterpriseAccount,
        tenant_id=ctx.tenant_id,
        node_id=None,
        parent_id=parent_id,
    )

    row = EnterpriseAccount(
        tenant_id=ctx.tenant_id,
        parent_id=parent_id,
        external_crm_ref=external_crm_ref,
        name=name,
        tier=AccountTier(tier),
        contract_status=ContractStatus(contract_status),
        attributes=attributes or {},
    )
    session.add(row)
    await _flush(session)

    await audit_service.record(
        session,
        ctx=ctx,
        action=AUDIT_ACCOUNT_CREATED,
        resource_type="enterprise_account",
        resource_id=row.id,
        decision="completed",
        reason_code="OK",
        after={"name": name, "tier": tier, "contract_status": contract_status},
        trace_id=trace_id,
    )
    return row


async def list_accounts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0,
) -> list[EnterpriseAccount]:
    rows = (
        await session.execute(
            select(EnterpriseAccount)
            .where(EnterpriseAccount.tenant_id == tenant_id)
            .order_by(EnterpriseAccount.name, EnterpriseAccount.id)
            .limit(limit)
            .offset(offset)
        )
    ).scalars()
    return list(rows)


async def get_account(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> EnterpriseAccount | None:
    return (
        await session.execute(
            select(EnterpriseAccount).where(
                EnterpriseAccount.id == account_id,
                EnterpriseAccount.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()


async def update_account(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    account: EnterpriseAccount,
    name: str | None = None,
    tier: str | None = None,
    contract_status: str | None = None,
    parent_id: uuid.UUID | None = None,
    parent_set: bool = False,
    external_crm_ref: str | None = None,
    external_crm_ref_set: bool = False,
    attributes: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Apply the supplied fields, returning what actually changed.

    `parent_set` / `external_crm_ref_set` exist because "not supplied" and
    "explicitly null" are different requests for a nullable field, and a
    caller that says `parent_id: null` is asking to detach the node, not to
    leave it alone.
    """
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    if name is not None:
        name = name.strip()
        if not name:
            raise OrgError("NAME_REQUIRED")
        _note(before, after, "name", account.name, name)
        account.name = name
    if tier is not None:
        validate_tier(tier)
        _note(before, after, "tier", str(account.tier), tier)
        account.tier = AccountTier(tier)
    if contract_status is not None:
        validate_contract_status(contract_status)
        _note(
            before,
            after,
            "contract_status",
            str(account.contract_status),
            contract_status,
        )
        account.contract_status = ContractStatus(contract_status)
    if parent_set:
        await _assert_parent_ok(
            session,
            table=EnterpriseAccount,
            tenant_id=ctx.tenant_id,
            node_id=account.id,
            parent_id=parent_id,
        )
        _note(
            before,
            after,
            "parent_id",
            str(account.parent_id) if account.parent_id else None,
            str(parent_id) if parent_id else None,
        )
        account.parent_id = parent_id
    if external_crm_ref_set:
        _note(
            before,
            after,
            "external_crm_ref",
            account.external_crm_ref,
            external_crm_ref,
        )
        account.external_crm_ref = external_crm_ref
    if attributes is not None:
        _note(before, after, "attributes", account.attributes, attributes)
        account.attributes = attributes

    await _flush(session)
    if after:
        await audit_service.record(
            session,
            ctx=ctx,
            action=AUDIT_ACCOUNT_UPDATED,
            resource_type="enterprise_account",
            resource_id=account.id,
            decision="completed",
            reason_code="OK",
            before=before,
            after=after,
            trace_id=trace_id,
        )
    return after


# --- Department -------------------------------------------------------------


async def create_department(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    name: str,
    slug: str,
    parent_id: uuid.UUID | None = None,
    external_ref: str | None = None,
    trace_id: str | None = None,
) -> Department:
    name = name.strip()
    if not name:
        raise OrgError("NAME_REQUIRED")
    slug = normalise_slug(slug)
    await _assert_parent_ok(
        session,
        table=Department,
        tenant_id=ctx.tenant_id,
        node_id=None,
        parent_id=parent_id,
    )

    row = Department(
        tenant_id=ctx.tenant_id,
        parent_id=parent_id,
        name=name,
        slug=slug,
        external_ref=external_ref,
    )
    session.add(row)
    await _flush(session)

    await audit_service.record(
        session,
        ctx=ctx,
        action=AUDIT_DEPARTMENT_CREATED,
        resource_type="department",
        resource_id=row.id,
        decision="completed",
        reason_code="OK",
        after={"name": name, "slug": slug},
        trace_id=trace_id,
    )
    return row


async def list_departments(
    session: AsyncSession, *, tenant_id: uuid.UUID, limit: int = 200
) -> list[Department]:
    rows = (
        await session.execute(
            select(Department)
            .where(Department.tenant_id == tenant_id)
            .order_by(Department.slug)
            .limit(limit)
        )
    ).scalars()
    return list(rows)


async def get_department(
    session: AsyncSession, *, tenant_id: uuid.UUID, department_id: uuid.UUID
) -> Department | None:
    return (
        await session.execute(
            select(Department).where(
                Department.id == department_id, Department.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()


async def update_department(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    department: Department,
    name: str | None = None,
    parent_id: uuid.UUID | None = None,
    parent_set: bool = False,
    external_ref: str | None = None,
    external_ref_set: bool = False,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """`slug` is deliberately absent: it is the handle external bindings point
    at, and renaming it would silently re-point an IdP group mapping or a Jira
    binding. Moving a department is a rename of `name`; changing `slug` would
    be a different department."""
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    if name is not None:
        name = name.strip()
        if not name:
            raise OrgError("NAME_REQUIRED")
        _note(before, after, "name", department.name, name)
        department.name = name
    if parent_set:
        await _assert_parent_ok(
            session,
            table=Department,
            tenant_id=ctx.tenant_id,
            node_id=department.id,
            parent_id=parent_id,
        )
        _note(
            before,
            after,
            "parent_id",
            str(department.parent_id) if department.parent_id else None,
            str(parent_id) if parent_id else None,
        )
        department.parent_id = parent_id
    if external_ref_set:
        _note(before, after, "external_ref", department.external_ref, external_ref)
        department.external_ref = external_ref

    await _flush(session)
    if after:
        await audit_service.record(
            session,
            ctx=ctx,
            action=AUDIT_DEPARTMENT_UPDATED,
            resource_type="department",
            resource_id=department.id,
            decision="completed",
            reason_code="OK",
            before=before,
            after=after,
            trace_id=trace_id,
        )
    return after


def _note(before: dict[str, Any], after: dict[str, Any], field: str, old: Any, new: Any) -> None:
    """Record a change only when the value actually differs.

    An audit entry that says "updated" for a request that set a field to the
    value it already had is noise, and noise is what makes an audit log stop
    being read.
    """
    if old != new:
        before[field] = old
        after[field] = new


__all__ = [
    "MAX_DEPTH",
    "OrgError",
    "account_sla_facts",
    "create_account",
    "create_department",
    "department_exists",
    "get_account",
    "get_department",
    "list_accounts",
    "list_departments",
    "normalise_slug",
    "update_account",
    "update_department",
    "validate_contract_status",
    "validate_tier",
]
