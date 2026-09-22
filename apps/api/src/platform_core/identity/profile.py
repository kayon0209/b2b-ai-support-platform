"""Feature list 2.4: the customer profile a support agent needs in front of them.

What already existed: `ContactAccountFacts` projects the account **tier** and
**contract status**, which is what tier-driven routing (7.3) needs. What did
not: anything an agent would call a profile - the account's payment terms, who
owns it, what industry it is in.

**Why a whitelist and not the whole `attributes` column.** That column is
free-form JSON that a CRM sync writes, so its contents are whatever the
integration decided to put there - including things that were never meant to be
read aloud, and things that will only exist next quarter. Passing it through to
a prompt or a handoff note would make every future CRM field automatically
visible to the model, which is the opposite of data minimisation. Naming the
keys here means adding a field is a deliberate act.

**Missing is reported, not defaulted.** An account with no payment terms is not
"NET 30 by default" - it is an account whose terms nobody recorded, and a
support agent who reads a default will quote it to the customer. `missing`
makes the gap visible so the UI can say "not on file" instead of inventing one.

**Nothing here is derived from customer conversations.** The profile is
account data; conversation-derived facts live in `contact_facts` and are
minimised separately. Keeping them apart is what stops a one-off sentence from
becoming a durable attribute.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.identity.models import EnterpriseAccount

# The attributes an agent may be shown. Deliberately short: each addition is a
# decision about what the platform says out loud about a customer.
PROFILE_ATTRIBUTE_KEYS: tuple[str, ...] = (
    "payment_terms",
    "credit_limit",
    "account_manager",
    "industry",
)

# Reported as missing when absent, so "not on file" stays distinguishable from
# "not applicable". Payment terms are the one that gets quoted, so they are the
# one that must never be filled in by a default.
_REPORTABLE_AS_MISSING: tuple[str, ...] = ("payment_terms",)


@dataclass(frozen=True)
class AccountProfile:
    """The account as a support agent sees it."""

    account_id: uuid.UUID
    name: str
    tier: str
    contract_status: str
    # Only whitelisted keys, stringified for display.
    attributes: dict[str, str]
    # Expected-but-absent keys. Empty is a complete profile.
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether everything the profile is expected to carry is present."""
        return not self.missing


def _display(value: Any) -> str:
    """Render an attribute for display without inventing a format."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


async def account_profile(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> AccountProfile | None:
    """The profile for one account, or None when it does not exist.

    None rather than an empty profile: "this account is not here" and "this
    account has nothing on file" are different answers, and a caller that
    cannot tell them apart will show a wrong one.
    """
    row = (
        await session.execute(
            select(EnterpriseAccount).where(
                EnterpriseAccount.tenant_id == tenant_id,
                EnterpriseAccount.id == account_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    raw = row.attributes if isinstance(row.attributes, dict) else {}
    present = {
        key: _display(raw[key])
        for key in PROFILE_ATTRIBUTE_KEYS
        if raw.get(key) is not None and str(raw.get(key)).strip() != ""
    }
    missing = tuple(key for key in _REPORTABLE_AS_MISSING if key not in present)

    return AccountProfile(
        account_id=row.id,
        name=row.name,
        tier=str(getattr(row.tier, "value", row.tier)),
        contract_status=str(getattr(row.contract_status, "value", row.contract_status)),
        attributes=present,
        missing=missing,
    )


async def profile_for_contact(
    session: AsyncSession, *, tenant_id: uuid.UUID, external_contact_id: str
) -> AccountProfile | None:
    """The profile behind a channel contact, when the contact is bound.

    The same lookup shape as `account_facts_for_contact`: an unbound contact is
    the common case, not an error, so this returns None rather than raising.
    """
    from platform_core.identity.models import EnterpriseAccountContact

    binding = (
        await session.execute(
            select(EnterpriseAccountContact.enterprise_account_id).where(
                EnterpriseAccountContact.tenant_id == tenant_id,
                EnterpriseAccountContact.external_contact_id == external_contact_id,
            )
        )
    ).scalar_one_or_none()
    if binding is None:
        return None
    return await account_profile(session, tenant_id=tenant_id, account_id=binding)


__all__ = [
    "PROFILE_ATTRIBUTE_KEYS",
    "AccountProfile",
    "account_profile",
    "profile_for_contact",
]
