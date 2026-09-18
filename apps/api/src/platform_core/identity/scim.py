"""SCIM 2.0 provisioning: the protocol layer, as pure functions.

`docs/integrations.md` describes SCIM as "group-to-role/department mapping",
which is exactly what it is here: an identity provider pushes Users and Groups,
and a Group becomes a `Department` (not a `Role` - see below).

Everything in this module is a pure function over dicts, so the protocol rules
are testable without a database and a reviewer can read them in one screen.
The endpoints in `scim_router.py` do the I/O.

Four protocol decisions that are security decisions
---------------------------------------------------
**A Group becomes a Department, never a Membership role.** `MembershipRole` is
{tenant_owner, support_admin, security_admin, ...} - it decides who can change
credentials, rotate connector secrets and read the audit log. Letting an IdP
group name map onto it would mean whoever administers the IdP can grant
themselves `tenant_owner` in the tenant by renaming a group. Departments are the
thing an IdP legitimately owns. Role assignment stays a tenant-owner action
(`POST /v1/identity/members/{id}`).

**`filter` support is deliberately minimal: `userName eq "..."` only.** SCIM
filter syntax is a small query language; implementing a parser for it and
getting an operator wrong is how a filter returns another tenant's user. The
unsupported case is a 400 that names the supported form, so an IdP
administrator can fix their configuration instead of watching it silently do
nothing.

**`PATCH` is applied as a set of named operations, not as a JSON merge.** SCIM's
`Operations` array has its own semantics (`add`/`replace`/`remove` with `path`),
and treating it as a merge patch would make `remove` a no-op - an account that
is deprovisioned in the IdP stays active here.

**`active: false` deactivates; it never deletes.** A user who has left still
appears in the audit trail and on the Cases they worked on, and a deleted row
would break both.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

# SCIM's own status codes, which are not HTTP codes. Returning a bare 400 loses
# the machine-readable part an IdP's provisioning log shows.
SCIM_INVALID_VALUE = "invalidValue"
SCIM_NOT_FOUND = "notFound"
SCIM_UNIQUENESS = "uniqueness"
SCIM_INVALID_FILTER = "invalidFilter"
SCIM_MUTABILITY = "mutability"

MAX_RESULTS = 200
DEFAULT_COUNT = 100

# Attributes a PATCH may change, per resource type. Anything else - an id, a
# `meta`, a `schemas` - is refused with `mutability` rather than ignored, so
# an IdP trying to change something this endpoint does not own sees an error
# instead of a silent no-op. They live here, with the protocol rules, rather
# than in the router: a reviewer reading `apply_patch` should not have to go
# looking for the set that decides what it will accept.
#
# Note what is absent: no role, no membership, no department. A Group maps to
# a Department and never to a role - see the module docstring.
MUTABLE_USER = frozenset({"username", "displayname", "active", "name", "emails"})
MUTABLE_GROUP = frozenset({"displayname", "members"})

_FILTER = re.compile(r'^\s*(?P<attr>\w+(?:\.\w+)?)\s+eq\s+"(?P<value>[^"]*)"\s*$', re.IGNORECASE)
_SUPPORTED_FILTER_ATTRS = frozenset({"username", "externalid", "displayname"})


class ScimError(Exception):
    """A SCIM protocol error, carrying the SCIM code and HTTP status."""

    def __init__(self, code: str, detail: str, *, status: int = 400, scim_type: str = "") -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status
        self.scim_type = scim_type


@dataclass(frozen=True)
class ParsedFilter:
    attribute: str
    value: str


def parse_filter(raw: str | None) -> ParsedFilter | None:
    """`userName eq "ada@example.com"`, or nothing.

    Only `eq` on a small set of attributes. Anything else is refused by name, so
    a misconfigured IdP produces "this filter is not supported" rather than an
    empty page that looks like "no such user" - and, more importantly, rather
    than a broader query somebody wrote to make the empty page go away.
    """
    if raw is None or not raw.strip():
        return None
    match = _FILTER.match(raw)
    if match is None:
        raise ScimError(
            SCIM_INVALID_FILTER,
            'only `attribute eq "value"` is supported',
            scim_type=SCIM_INVALID_FILTER,
        )
    attribute = match.group("attr").lower()
    if attribute not in _SUPPORTED_FILTER_ATTRS:
        raise ScimError(
            SCIM_INVALID_FILTER,
            f"filtering on {attribute!r} is not supported; "
            f"supported: {', '.join(sorted(_SUPPORTED_FILTER_ATTRS))}",
            scim_type=SCIM_INVALID_FILTER,
        )
    value = match.group("value").strip()
    if not value:
        raise ScimError(SCIM_INVALID_VALUE, "filter value must not be empty")
    return ParsedFilter(attribute=attribute, value=value)


def paging(start_index: int | None, count: int | None, total: int) -> tuple[int, int, int]:
    """`(offset, limit, start_index)` from SCIM's 1-based paging parameters.

    SCIM counts from 1 and SQL counts from 0, and the off-by-one is a classic
    "page two repeats page one" bug. Normalised here, once.
    """
    start = 1 if start_index is None else int(start_index)
    if start < 1:
        raise ScimError(SCIM_INVALID_VALUE, "startIndex must be >= 1")
    limit = DEFAULT_COUNT if count is None else int(count)
    if limit < 0:
        raise ScimError(SCIM_INVALID_VALUE, "count must be >= 0")
    return start - 1, min(limit, MAX_RESULTS), start


def list_response(
    resources: list[dict[str, Any]], *, total: int, start: int, limit: int
) -> dict[str, Any]:
    return {
        "schemas": [SCIM_LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


def error_response(code: str, detail: str, *, scim_type: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {"schemas": [SCIM_ERROR_SCHEMA], "detail": detail, "status": str(code)}
    if scim_type:
        body["scimType"] = scim_type
    return body


# --- serialisation -----------------------------------------------------------


def user_resource(
    *,
    user_id: uuid.UUID,
    email: str,
    display_name: str,
    active: bool,
    external_id: str | None = None,
) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "schemas": [SCIM_USER_SCHEMA],
        "id": str(user_id),
        "userName": email,
        "displayName": display_name,
        "active": active,
        # `meta` is required-ish: several IdPs refuse a resource without it, and
        # the resource type is what a provisioning log shows.
        "meta": {"resourceType": "User", "location": f"/scim/v2/Users/{user_id}"},
    }
    if external_id:
        resource["externalId"] = external_id
    return resource


def group_resource(
    *, department_id: uuid.UUID, display_name: str, slug: str, members: list[uuid.UUID]
) -> dict[str, Any]:
    return {
        "schemas": [SCIM_GROUP_SCHEMA],
        "id": str(department_id),
        "displayName": display_name,
        # `externalId` carries the slug rather than the id: the slug is the
        # stable handle an IdP group mapping targets, and it survives a rename.
        "externalId": slug,
        "members": [{"value": str(m), "type": "User"} for m in members],
        "meta": {"resourceType": "Group", "location": f"/scim/v2/Groups/{department_id}"},
    }


def user_from_payload(payload: dict[str, Any]) -> tuple[str, str, bool]:
    """`(email, display_name, active)`, refusing what this platform cannot accept.

    `userName` is the email here: `users.primary_email` is UNIQUE and NOT NULL,
    and a SCIM resource without one cannot be represented. Refusing it is better
    than inventing an address that silently collides with a real one.
    """
    email = str(payload.get("userName") or "").strip().lower()
    if not email or "@" not in email:
        raise ScimError(SCIM_INVALID_VALUE, "userName must be an email address")
    display = str(payload.get("displayName") or "").strip() or email
    active = payload.get("active")
    # Absent means active: the SCIM spec's default for a newly provisioned
    # resource, and treating absent as inactive would de-provision every user
    # whose IdP omits the field.
    return email, display[:255], True if active is None else bool(active)


def group_from_payload(payload: dict[str, Any]) -> tuple[str, str]:
    """`(display_name, slug)` for a Group.

    The slug comes from `externalId` when the IdP supplies one, so the mapping
    to a Department is stable across a rename. Falling back to a slug derived
    from the display name means an IdP that omits it still works, and the
    department's slug then changes only if they rename the group.
    """
    display = str(payload.get("displayName") or "").strip()
    if not display:
        raise ScimError(SCIM_INVALID_VALUE, "displayName is required")
    raw_slug = str(payload.get("externalId") or display).strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw_slug).strip("-")[:63]
    if not slug or not slug[0].isalnum():
        raise ScimError(SCIM_INVALID_VALUE, "displayName cannot be turned into a slug")
    return display[:255], slug


def member_ids(payload: dict[str, Any]) -> list[uuid.UUID]:
    """Group member ids, refusing anything that is not a UUID.

    A malformed member id is not skipped: silently dropping one leaves a user in
    a group in the IdP and out of it here, which is exactly the drift SCIM is
    supposed to remove.
    """
    out: list[uuid.UUID] = []
    for entry in payload.get("members") or []:
        value = entry.get("value") if isinstance(entry, dict) else entry
        try:
            out.append(uuid.UUID(str(value)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ScimError(SCIM_INVALID_VALUE, f"member value {value!r} is not a uuid") from exc
    return out


# --- PATCH -------------------------------------------------------------------


def apply_patch(payload: dict[str, Any], *, mutable: frozenset[str]) -> dict[str, Any]:
    """Apply a SCIM `PatchOp` to a resource, returning the merged result.

    Supports `add` and `replace` (which are equivalent for a single-valued
    attribute) and `remove`, with a `path` that names a top-level attribute.
    Paths are checked against `mutable`, so a request that tries to change an
    attribute this endpoint does not own - an id, a `meta`, a membership role -
    is refused rather than ignored.

    A `remove` without a path clears the attributes named in `value`, which is
    how an IdP removes all members from a group.
    """
    operations = payload.get("Operations")
    if not isinstance(operations, list) or not operations:
        raise ScimError(SCIM_INVALID_VALUE, "Operations must be a non-empty list")

    result = {k: v for k, v in payload.items() if k != "Operations"}
    for operation in operations:
        if not isinstance(operation, dict):
            raise ScimError(SCIM_INVALID_VALUE, "each operation must be an object")
        op = str(operation.get("op") or "").lower()
        path = operation.get("path")
        if op not in ("add", "replace", "remove"):
            raise ScimError(SCIM_INVALID_VALUE, f"unsupported op {op!r}")

        attribute = _attribute_of(path)
        if attribute is not None and attribute not in mutable:
            raise ScimError(
                SCIM_MUTABILITY,
                f"{attribute!r} cannot be changed through this endpoint",
                scim_type=SCIM_MUTABILITY,
            )

        if op == "remove":
            if attribute is not None:
                result.pop(attribute, None)
                continue
            # No path: `value` names the attributes to clear. Compared
            # case-insensitively because SCIM attribute names are, while the
            # original spelling is kept in the result so callers can read the
            # key they sent.
            for name in _paths_of_values(operation.get("value")):
                if name not in mutable:
                    raise ScimError(
                        SCIM_MUTABILITY,
                        f"{name!r} cannot be changed through this endpoint",
                        scim_type=SCIM_MUTABILITY,
                    )
                result.pop(name, None)
            continue

        if attribute is not None:
            result[attribute] = operation.get("value")
            continue
        value = operation.get("value")
        if not isinstance(value, dict):
            raise ScimError(SCIM_INVALID_VALUE, "an operation with no path needs an object value")
        for name, item in value.items():
            # Case-insensitive, matching SCIM's attribute-name rules. The key
            # keeps its original spelling in the result.
            if name.lower() not in mutable:
                raise ScimError(
                    SCIM_MUTABILITY,
                    f"{name!r} cannot be changed through this endpoint",
                    scim_type=SCIM_MUTABILITY,
                )
            result[name] = item
    return result


def _attribute_of(path: Any) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return None
    # Only the first segment: a sub-attribute path (`name.familyName`) is not
    # used by anything this endpoint owns, and accepting one would mean silently
    # ignoring the part after the dot.
    return path.strip().split(".", 1)[0].lower()


def _paths_of_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.lower()]
    if isinstance(value, list):
        return [str(v).lower() for v in value]
    return []


__all__ = [
    "DEFAULT_COUNT",
    "MAX_RESULTS",
    "MUTABLE_GROUP",
    "MUTABLE_USER",
    "SCIM_ERROR_SCHEMA",
    "SCIM_GROUP_SCHEMA",
    "SCIM_INVALID_FILTER",
    "SCIM_INVALID_VALUE",
    "SCIM_LIST_SCHEMA",
    "SCIM_MUTABILITY",
    "SCIM_NOT_FOUND",
    "SCIM_PATCH_SCHEMA",
    "SCIM_UNIQUENESS",
    "SCIM_USER_SCHEMA",
    "ParsedFilter",
    "ScimError",
    "apply_patch",
    "error_response",
    "group_from_payload",
    "group_resource",
    "list_response",
    "member_ids",
    "paging",
    "parse_filter",
    "user_from_payload",
    "user_resource",
]
