"""Resource-level ACL evaluation, shared by retrieval and download.

Why this module exists
---------------------
`retrieval/hybrid.py` implemented the ACL predicate inline, as a SQL fragment
inside two read queries. That was fine while retrieval was the only consumer.
It stops being fine the moment a second path can hand out document content -
a download URL - because then "can this principal read this document" has two
implementations, and they will drift. The drift is a security bug, not a
cosmetic one: a document that is invisible to search but downloadable is still
a leak.

So the predicate lives here once, in two forms that must agree:

- `acl_filter_fragment()` returns the SQL fragment retrieval splices into its
  queries. It is a code-owned constant; every value flows through bound
  parameters.
- `can_read_document()` evaluates the same rule in Python for a single
  document, for callers that are not building a set-based query.

Semantics (from docs/security.md, and matching what retrieval already did):

    A resource carrying ACL entries is readable only if one of those entries
    matches the caller's principal set. A resource carrying NO ACL entries is
    readable by anyone in the tenant.

That second clause is the default-open case and is deliberate - documents are
tenant-scoped by RLS, and requiring an explicit grant per document would make
every upload invisible until someone remembered to grant it. The first clause
is what makes a grant meaningful: adding an ACL entry to a resource *narrows*
it.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class PrincipalScope:
    """The set of (type, id) pairs a request acts as.

    Multi-valued because one request can act as a user *and* their enterprise
    account: an account-level grant should apply to a user who belongs to it.
    """

    principal_types: tuple[str, ...]
    principal_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.principal_types) != len(self.principal_ids):
            raise ValueError("principal types and ids must be the same length")

    @property
    def is_empty(self) -> bool:
        return not self.principal_types


# The single source of truth for the retrieval-side predicate. `{acl}` in
# hybrid.py expands to this. Kept as a named constant so a reader can find it
# from either direction.
ACL_FILTER_SQL = """
  AND NOT EXISTS (
    SELECT 1 FROM knowledge_acls a
    WHERE a.tenant_id = CAST(:tid AS uuid)
      AND (
        (a.resource_type = 'space' AND a.resource_id = d.space_id)
        OR (a.resource_type = 'document' AND a.resource_id = d.id)
        OR (a.resource_type = 'version' AND a.resource_id = dv.id)
      )
      AND NOT (
        a.principal_type = ANY(CAST(:p_types AS text[]))
        AND a.principal_id = ANY(CAST(:p_ids AS text[]))
      )
  )
"""


def acl_filter_fragment() -> str:
    return ACL_FILTER_SQL


async def can_read_document(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    principal_id: str,
    role: str,
) -> bool:
    """Evaluate the ACL rule for one document.

    Implemented as a single query that counts *blocking* entries rather than
    fetching rows and deciding in Python, so the answer cannot depend on
    pagination or on how many entries exist.

    A blocking entry is one that applies to this document (via its space,
    itself, or any of its versions) and whose principal does not match the
    caller. No blocking entries -> readable.
    """
    if not principal_id and not role:
        # No usable principal. Fail closed rather than falling through to the
        # default-open branch: an unattributable request must not be treated
        # as "no ACLs apply, therefore allowed".
        return False

    ids = [i for i in (principal_id, role) if i]
    row = (
        await session.execute(
            text(
                """
                SELECT count(*) FROM knowledge_acls a
                WHERE a.tenant_id = CAST(:tid AS uuid)
                  AND (
                    (a.resource_type = 'document' AND a.resource_id = CAST(:doc AS uuid))
                    OR (a.resource_type = 'space'
                        AND a.resource_id = (SELECT space_id FROM documents
                                             WHERE id = CAST(:doc AS uuid)))
                    OR (a.resource_type = 'version'
                        AND a.resource_id IN (SELECT id FROM document_versions
                                              WHERE document_id = CAST(:doc AS uuid)))
                  )
                  AND NOT (
                    a.principal_type = ANY(CAST(:p_types AS text[]))
                    AND a.principal_id = ANY(CAST(:p_ids AS text[]))
                  )
                """
            ),
            {
                "tid": str(tenant_id),
                "doc": str(document_id),
                # A grant may name the principal by user id or by role, so the
                # caller contributes both and either satisfies it.
                "p_types": ["user", "role"],
                "p_ids": ids,
            },
        )
    ).scalar()
    return int(row or 0) == 0
