"""Integration tests: bootstrap token -> membership resolution (ticket 23).

This suite exists because the bootstrap resolver used to synthesise a tenant
id with `uuid5(namespace, f"tenant:{slug}")` and return a context with
`role=None`, deferring the real lookup to a
`identity/repository.resolve_membership` function that did not exist. Three
consequences, none of which any existing test could see:

1. The synthesised id only matched a real tenant row by coincidence, so RLS
   scoped requests to a tenant that usually did not exist. A caller got
   "works, but every table is empty" instead of an authentication error.
2. `role=None` is denied by every policy gate, so **every** authenticated
   endpoint returned 403 for **every** user, including `tenant_owner`.
3. A wrong password/token and a suspended tenant were indistinguishable from
   a working login, because all three produced the same shape.

The unit suite missed it because those tests only asserted `actor_id`
round-trips. The integration suite missed it because `_RoleResolver`
fabricates the role directly, bypassing resolution entirely.

The tests below drive the *real* resolver against real Postgres.

Two further defects surfaced while writing these, both fixed and pinned here:

- `Tenant.status` / `Membership.role` were declared as SQLAlchemy `Enum`
  (which persists member *names*, e.g. `ACTIVE`) over migrations that create
  plain string columns seeded with lowercase *values* (`active`). Every ORM
  read of those columns raised LookupError. Pinned by
  `test_tenant_and_role_columns_round_trip_lowercase_values`.
- `memberships` is FORCE-RLS'd, so the membership read is only possible once
  `app.tenant_id` has been bound from the `tenants` lookup. Pinned by
  `test_membership_read_requires_tenant_binding_first`.
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from platform_core.identity.tenant_context import TenantContextError

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = os.environ.get(
    "APP_TEST_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)

SLUG = "resolve-it"
ACTIVE_SLUG = "resolve-active"
SUSPENDED_SLUG = "resolve-suspended"
NO_MEMBERSHIP_SLUG = "resolve-nomember"

# Its own tenant id. This file, `test_cross_tenant_leak_surfaces` and
# `test_outbox_relay` all used `...d1` while seeding different *slugs*, and
# each seed guarded on `ON CONFLICT (slug)` - which does not suppress a
# primary-key conflict. Whichever file ran second lost the race to an existing
# row and died on `tenants_pkey`, in whichever order the suite happened to
# collect them. A shared id across files is a hidden coupling; distinct ids
# make each fixture independent.
TENANT = "01900000-0000-7000-8000-0000000000d5"
ACTIVE_TENANT = "01900000-0000-7000-8000-0000000000d2"
SUSPENDED_TENANT = "01900000-0000-7000-8000-0000000000d3"
NO_MEMBERSHIP_TENANT = "01900000-0000-7000-8000-0000000000d4"

USER = "01900000-0000-7000-8000-0000000000e1"
ORPHAN_USER = "01900000-0000-7000-8000-0000000000e2"


@pytest.fixture(scope="module", autouse=True)
def seed() -> None:
    admin = create_engine(ADMIN_URL)
    _cleanup(admin)
    with admin.begin() as conn:
        for tid, slug, name, status in (
            (TENANT, SLUG, "Resolve Tenant", "active"),
            (ACTIVE_TENANT, ACTIVE_SLUG, "Resolve Active", "active"),
            (SUSPENDED_TENANT, SUSPENDED_SLUG, "Resolve Suspended", "suspended"),
            (NO_MEMBERSHIP_TENANT, NO_MEMBERSHIP_SLUG, "Resolve NoMember", "active"),
        ):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, :status) ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug, "name": name, "status": status},
            )
        for uid, email in ((USER, "resolve-it@example.com"), (ORPHAN_USER, "orphan@example.com")):
            conn.execute(
                text(
                    "INSERT INTO users (id, primary_email, display_name, is_service_account) "
                    "VALUES (:id, :email, 'Resolve IT', false) "
                    "ON CONFLICT (primary_email) DO NOTHING"
                ),
                {"id": uid, "email": email},
            )
        # Memberships: active in SLUG + ACTIVE_SLUG, suspended in SUSPENDED_SLUG.
        for tid, uid, role, status in (
            (TENANT, USER, "tenant_owner", "active"),
            (ACTIVE_TENANT, USER, "support_agent", "active"),
            (SUSPENDED_TENANT, USER, "tenant_owner", "suspended"),
        ):
            conn.execute(
                text(
                    "INSERT INTO memberships (id, tenant_id, user_id, role, status) "
                    "VALUES (gen_random_uuid(), :tid, :uid, :role, :status) "
                    "ON CONFLICT (tenant_id, user_id) DO NOTHING"
                ),
                {"tid": tid, "uid": uid, "role": role, "status": status},
            )
    yield
    _cleanup(admin)
    admin.dispose()


def _cleanup(admin: object) -> None:
    with admin.begin() as conn:  # type: ignore[attr-defined]
        slugs = (SLUG, ACTIVE_SLUG, SUSPENDED_SLUG, NO_MEMBERSHIP_SLUG)
        conn.execute(
            text(
                "DELETE FROM memberships WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE slug = ANY(:slugs))"
            ),
            {"slugs": list(slugs)},
        )
        conn.execute(
            text("DELETE FROM users WHERE primary_email IN (:a, :b)"),
            {"a": "resolve-it@example.com", "b": "orphan@example.com"},
        )
        conn.execute(text("DELETE FROM tenants WHERE slug = ANY(:slugs)"), {"slugs": list(slugs)})


def _run(coro: object) -> object:
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)  # type: ignore[arg-type]


async def _load(slug: str, user_id: uuid.UUID) -> object:
    from platform_core.db import session_scope_with_url
    from platform_core.identity.repository import load_context

    async with session_scope_with_url(APP_URL) as session:
        return await load_context(session, slug, user_id)


async def _resolve_pair(slug: str, user_id: uuid.UUID) -> object:
    """The explicit two-step path: tenants lookup, then membership under RLS."""
    from platform_core.db import session_scope_with_url
    from platform_core.identity.repository import resolve_by_slug

    async with session_scope_with_url(APP_URL) as session:
        return await resolve_by_slug(session, slug, user_id)


# --- 1. Happy path: the context carries a real role ------------------------


def test_resolution_returns_role_and_real_tenant_id() -> None:
    """A valid slug + membership yields the table's tenant id and the role.

    This is the assertion the old unit test was missing: it checked
    `actor_id` only, so a `role=None` context passed.
    """
    ctx = _run(_load(SLUG, uuid.UUID(USER)))
    assert ctx.role == "tenant_owner", f"role must be resolved, got {ctx.role!r}"
    assert str(ctx.tenant_id) == TENANT, "tenant id must be the row's id, not a slug-derived uuid5"
    assert ctx.actor_kind == "user"


def test_resolution_uses_the_membership_role_not_a_default() -> None:
    """The same user in a different tenant resolves to that tenant's role."""
    ctx = _run(_load(ACTIVE_SLUG, uuid.UUID(USER)))
    assert ctx.role == "support_agent"
    assert str(ctx.tenant_id) == ACTIVE_TENANT


def test_two_step_path_resolves_the_same_identity() -> None:
    """`resolve_by_slug` and the bootstrap function must agree.

    Both paths are live: the single-probe function is used by the token
    resolver, the two-step form by callers that also need tenant settings.
    If they ever disagree, one of them is reading the wrong row.
    """
    tenant, membership = _run(_resolve_pair(SLUG, uuid.UUID(USER)))
    assert str(tenant.id) == TENANT
    assert str(membership.user_id) == USER
    assert membership.role.value == "tenant_owner"


# --- 2. Failure modes all fail closed -------------------------------------


def test_unknown_slug_is_rejected() -> None:
    with pytest.raises(TenantContextError):
        _run(_load("resolve-does-not-exist", uuid.UUID(USER)))


def test_suspended_tenant_is_rejected() -> None:
    with pytest.raises(TenantContextError):
        _run(_load(SUSPENDED_SLUG, uuid.UUID(USER)))


def test_user_without_membership_is_rejected() -> None:
    """A known user in a known tenant with no membership row gets nothing.

    Critical: this must not succeed via a synthesised context, and it must
    not be reachable through the tenant binding done in step 1.
    """
    with pytest.raises(TenantContextError):
        _run(_load(SLUG, uuid.UUID(ORPHAN_USER)))


def test_inactive_membership_is_rejected() -> None:
    with pytest.raises(TenantContextError):
        _run(_load(SUSPENDED_SLUG, uuid.UUID(USER)))


def test_no_membership_tenant_still_resolves_its_tenant_row() -> None:
    """The tenant exists and is active; only the membership is missing.

    Distinguishes "unknown tenant" from "known tenant, no membership". The
    single-probe auth path deliberately collapses both into one message (a
    login must not be a tenant/user enumeration oracle), so this asserts the
    distinction on the two-step path, which *does* surface it: step 1 finds
    the tenant, step 2 finds no membership.
    """
    with pytest.raises(TenantContextError) as exc:
        _run(_resolve_pair(NO_MEMBERSHIP_SLUG, uuid.UUID(USER)))
    assert "membership" in str(exc.value)


def test_auth_failures_are_indistinguishable_to_the_caller() -> None:
    """Unknown slug, suspended tenant, no membership: one error, one message.

    This is a security property, not a cosmetic one. If the four failure
    modes produced different messages, an unauthenticated caller could probe
    which tenant slugs exist and which users belong to them.
    """
    messages = set()
    for slug, user in (
        ("resolve-does-not-exist", uuid.UUID(USER)),
        (SUSPENDED_SLUG, uuid.UUID(USER)),
        (SLUG, uuid.UUID(ORPHAN_USER)),
        (NO_MEMBERSHIP_SLUG, uuid.UUID(USER)),
    ):
        with pytest.raises(TenantContextError) as exc:
            _run(_load(slug, user))
        messages.add(str(exc.value))
    assert messages == {"identity not found or inactive"}, messages


# --- 3. The RLS ordering constraint ---------------------------------------


def test_membership_read_requires_tenant_binding_first() -> None:
    """Reading memberships before binding the tenant returns nothing.

    This is why resolution is two steps, and why the old single-step lookup
    could never have worked: the policy predicate compares against NULL and
    the row is invisible. Pinned directly against the database so the
    constraint is documented in executable form.
    """
    import asyncio

    from sqlalchemy import text as sql_text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def probe() -> tuple[int, int]:
        eng = create_async_engine(APP_URL)
        async with eng.connect() as conn:
            # No app.tenant_id bound: RLS hides every membership row.
            unbound = (
                await conn.execute(
                    sql_text("SELECT count(*) FROM memberships WHERE tenant_id = :t"),
                    {"t": TENANT},
                )
            ).scalar()
            await conn.execute(
                sql_text("SELECT set_config('app.tenant_id', :t, false)"), {"t": TENANT}
            )
            bound = (
                await conn.execute(
                    sql_text("SELECT count(*) FROM memberships WHERE tenant_id = :t"),
                    {"t": TENANT},
                )
            ).scalar()
        await eng.dispose()
        return int(unbound or 0), int(bound or 0)

    unbound, bound = asyncio.run(probe(), loop_factory=asyncio.SelectorEventLoop)
    assert unbound == 0, "memberships must be invisible without a bound tenant"
    assert bound >= 1, "memberships must be visible once the tenant is bound"


def test_tenant_table_is_readable_without_context() -> None:
    """`tenants` is global reference data, which is what makes step 1 work."""
    import asyncio

    from sqlalchemy import text as sql_text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def probe() -> int:
        eng = create_async_engine(APP_URL)
        async with eng.connect() as conn:
            n = (
                await conn.execute(
                    sql_text("SELECT count(*) FROM tenants WHERE slug = :s"), {"s": SLUG}
                )
            ).scalar()
        await eng.dispose()
        return int(n or 0)

    assert asyncio.run(probe(), loop_factory=asyncio.SelectorEventLoop) == 1


# --- 4. Enum/storage alignment -------------------------------------------


def test_tenant_and_role_columns_round_trip_lowercase_values() -> None:
    """ORM reads of `Tenant.status` and `Membership.role` must work.

    Both columns are plain strings holding lowercase *values* (`active`,
    `tenant_owner`). Declaring them as bare SQLAlchemy `Enum` makes the ORM
    look for member *names* (`ACTIVE`) and raise LookupError on every read.
    Reading through the ORM here is the point: a raw-text assertion would
    pass while the ORM stayed broken.
    """
    from sqlalchemy import select

    from platform_core.identity.models import Membership, Tenant

    async def probe() -> tuple[str, str]:
        from platform_core.db import session_scope_with_url

        async with session_scope_with_url(APP_URL) as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
            )
            tenant = (await session.execute(select(Tenant).where(Tenant.slug == SLUG))).scalar_one()
            membership = (
                await session.execute(
                    select(Membership).where(
                        Membership.tenant_id == uuid.UUID(TENANT),
                        Membership.user_id == uuid.UUID(USER),
                    )
                )
            ).scalar_one()
            # The ORM attribute is a StrEnum, which compares equal to its
            # lowercase value; str() confirms what is actually stored.
            return str(tenant.status), str(membership.role)

    status, role = _run(probe())  # type: ignore[misc]
    assert status == "active", f"status must read back as 'active', got {status!r}"
    assert role == "tenant_owner", f"role must read back as 'tenant_owner', got {role!r}"


def test_stored_values_are_lowercase_not_member_names() -> None:
    """Guards the storage contract itself, independent of the ORM layer."""
    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        statuses = {
            r[0] for r in conn.execute(text("SELECT DISTINCT status FROM tenants")).fetchall()
        }
        roles = {
            r[0] for r in conn.execute(text("SELECT DISTINCT role FROM memberships")).fetchall()
        }
    admin.dispose()
    assert "ACTIVE" not in statuses, "status must be stored as the enum value, not the name"
    assert not any(r.isupper() for r in roles), f"roles must be lowercase values, got {roles}"


# --- 5. The SECURITY DEFINER function is not a hole in the table ----------
#
# `resolve_active_membership` runs as its owner so RLS does not filter it.
# That is exactly the shape of a cross-tenant leak, so these tests try to
# falsify the claim that it stays narrower than the table it reads. If any of
# them starts passing "successfully" with rows, the design decision recorded
# in migration 0015 is no longer valid.


async def _exec_scalar(sql: str, params: dict | None = None) -> int:
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(APP_URL)
    try:
        async with eng.connect() as conn:
            return int((await conn.execute(text(sql), params or {})).scalar_one())
    finally:
        await eng.dispose()


def test_function_reads_the_base_table_behind_rls() -> None:
    """Sanity: the function can see rows the app role cannot select directly.

    Same (slug, user) pair twice: direct SELECT returns 0 (RLS hides it),
    the function returns 1. Without this the remaining tests would be
    vacuous - a function that sees nothing leaks nothing.
    """
    direct = _run(_exec_scalar("SELECT count(*) FROM memberships WHERE user_id = :u", {"u": USER}))
    via_fn = _run(
        _exec_scalar(
            "SELECT count(*) FROM resolve_active_membership(:s, :u)", {"s": SLUG, "u": USER}
        )
    )
    assert direct == 0, "RLS must still hide the table from a bare connection"
    assert via_fn == 1, "the function must resolve the one matching membership"


def test_function_cannot_be_widened_by_like_metacharacters() -> None:
    """Slug patterns are matched as plain text, never as SQL wildcards."""
    for slug in ("%", "_", "resolve-%", "%", "' OR '1'='1", ""):
        n = _run(
            _exec_scalar(
                "SELECT count(*) FROM resolve_active_membership(:s, :u)", {"s": slug, "u": USER}
            )
        )
        assert n == 0, f"slug {slug!r} must not match any row, got {n}"


def test_function_cannot_be_composed_into_a_table_scan() -> None:
    """The strongest attack: drive the function from a LATERAL join.

    If the base table were somehow readable, this would enumerate every
    membership in every tenant by feeding each `user_id` through the
    function. It returns 0 because `memberships` is still FORCE-RLS'd in the
    outer query - the function does not make the table readable.
    """
    # Both the slug and the user id are bound parameters; the string is a
    # literal SQL skeleton, not interpolation (hence the S608 exemption).
    sql = (  # noqa: S608
        "SELECT count(*) FROM memberships m, LATERAL resolve_active_membership(:s, m.user_id) r"
    )
    n = _run(_exec_scalar(sql, {"s": SLUG}))
    assert n == 0, f"lateral composition must yield nothing, got {n}"


def test_function_attributes_are_pinned() -> None:
    """SECURITY DEFINER with a pinned search_path, owned, EXECUTE-only.

    An unpinned search_path on a SECURITY DEFINER function is the classic
    privilege-escalation vector: a caller creates a look-alike `tenants`
    table in an earlier schema and the function resolves against it.
    """
    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        row = conn.execute(
            text(
                "SELECT prosecdef, proconfig, pg_get_userbyid(proowner) FROM pg_proc "
                "WHERE proname = 'resolve_active_membership'"
            )
        ).one()
    admin.dispose()
    secdef, proconfig, owner = row[0], row[1] or [], row[2]
    assert secdef is True, "the function must be SECURITY DEFINER"
    assert any("search_path" in c for c in proconfig), "search_path must be pinned"
    assert owner != "platform_app", "the function must not be owned by the app role"
