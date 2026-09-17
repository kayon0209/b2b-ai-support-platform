"""Integration tests: the authentication path as the *HTTP entry point* sees it.

Why this file exists
--------------------
Every pre-existing test of authentication stops short of the real entry point:

- `test_membership_resolution.py` calls `load_context` / `resolve_by_slug`
  directly against Postgres. Real resolver, but no request, so the middleware
  wiring is untested.
- `test_audit_api.py`, `test_m2_http_api.py`, `test_knowledge_gaps.py` and
  others build the app with `resolver=bootstrap_token_resolver` hardcoded, or
  with a `_RoleResolver` that fabricates a `TenantContext` outright. Those
  bypass `build_resolver()` entirely.

That combination is precisely the blind spot that let the last round's defects
survive: every layer was covered individually while the seam between them was
not. Concretely, all of these were invisible:

1. `main.py` mounted `bootstrap_token_resolver` unconditionally, so the OIDC
   code existed but was unreachable in production.
2. `build_resolver()` selects the strategy from settings. Nothing asserted
   *which* strategy a given environment actually gets, or that a half-configured
   deployment refuses to start rather than silently running unsigned auth.
3. `oidc_token_resolver` had no test at all, not even a mocked one.

The tests below therefore drive the assembled app with real HTTP requests and a
real database, and assert on the observable contract (status code + error code +
that the body belongs to the right tenant). Where a dependency cannot be real -
a live Keycloak realm is not available in CI - the boundary is faked at the
seam, and the test says so.
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = os.environ.get(
    "APP_TEST_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)

SLUG = "auth-http"
TENANT = "01900000-0000-7000-8000-000000000f01"
USER = "01900000-0000-7000-8000-000000000f02"
# A user row that exists but has no membership in SLUG. Used to prove the
# resolver consults `memberships` rather than trusting the token's user id.
STRANGER = "01900000-0000-7000-8000-000000000f03"

OTHER_SLUG = "auth-http-other"
OTHER_TENANT = "01900000-0000-7000-8000-000000000f11"
OTHER_USER = "01900000-0000-7000-8000-000000000f12"


@pytest.fixture(scope="module", autouse=True)
def seed() -> None:
    admin = create_engine(ADMIN_URL)
    _cleanup(admin)
    with admin.begin() as conn:
        for tid, slug, name in (
            (TENANT, SLUG, "Auth HTTP"),
            (OTHER_TENANT, OTHER_SLUG, "Auth HTTP Other"),
        ):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug, "name": name},
            )
        for uid, email in (
            (USER, "auth-http@example.com"),
            (STRANGER, "auth-http-stranger@example.com"),
            (OTHER_USER, "auth-http-other@example.com"),
        ):
            conn.execute(
                text(
                    "INSERT INTO users (id, primary_email, display_name, is_service_account) "
                    "VALUES (:id, :email, 'Auth HTTP', false) "
                    "ON CONFLICT (primary_email) DO NOTHING"
                ),
                {"id": uid, "email": email},
            )
        for tid, uid, role in (
            (TENANT, USER, "tenant_owner"),
            (OTHER_TENANT, OTHER_USER, "tenant_owner"),
        ):
            conn.execute(
                text(
                    "INSERT INTO memberships (id, tenant_id, user_id, role, status) "
                    "VALUES (gen_random_uuid(), :tid, :uid, :role, 'active') "
                    "ON CONFLICT (tenant_id, user_id) DO NOTHING"
                ),
                {"tid": tid, "uid": uid, "role": role},
            )
        # One case per tenant, so a 200 proves *which* tenant's rows came back
        # rather than merely that the request succeeded.
        #
        # SLA clocks are stored as integer epoch seconds, not timestamptz - the
        # API serialises them as plain integers (see the CASE_* contract). The
        # seed has to match that storage contract or it fails on insert.
        for tid, subject in (
            (TENANT, "auth-http case A"),
            (OTHER_TENANT, "auth-http case B"),
        ):
            conn.execute(
                text(
                    "INSERT INTO cases (id, tenant_id, subject, status, priority, category, "
                    "version, opened_at, first_response_due_at, resolution_due_at) VALUES "
                    "(gen_random_uuid(), :tid, :subject, 'new', 'p2', 'general', 1, "
                    "EXTRACT(EPOCH FROM now())::bigint, "
                    "EXTRACT(EPOCH FROM now() + interval '1 hour')::bigint, "
                    "EXTRACT(EPOCH FROM now() + interval '8 hours')::bigint)"
                ),
                {"tid": tid, "subject": subject},
            )
    yield
    _cleanup(admin)
    admin.dispose()


def _cleanup(admin: object) -> None:
    with admin.begin() as conn:  # type: ignore[attr-defined]
        slugs = (SLUG, OTHER_SLUG)
        conn.execute(
            text(
                "DELETE FROM cases WHERE tenant_id IN (SELECT id FROM tenants WHERE slug = ANY(:s))"
            ),
            {"s": list(slugs)},
        )
        # Order matters: external_identities and memberships both reference
        # `users`, so they must go before the user rows or the delete trips a
        # foreign key (as it did when this suite was first written).
        conn.execute(
            text(
                "DELETE FROM external_identities WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE slug = ANY(:s))"
            ),
            {"s": list(slugs)},
        )
        conn.execute(
            text(
                "DELETE FROM memberships WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE slug = ANY(:s))"
            ),
            {"s": list(slugs)},
        )
        conn.execute(
            text(
                "DELETE FROM users WHERE primary_email IN "
                "('auth-http@example.com','auth-http-stranger@example.com',"
                "'auth-http-other@example.com')"
            )
        )
        conn.execute(text("DELETE FROM tenants WHERE slug = ANY(:s)"), {"s": list(slugs)})


def _token(slug: str, user: str) -> dict[str, str]:
    return {"Authorization": f"Bearer pt_{slug}_{user}"}


def _fresh_app(resolver: object) -> TestClient:
    """Assemble the real routers behind the real middleware, in that order.

    Starlette refuses `add_middleware` once an app has started, and
    `platform_core.main.app` is started by the first TestClient in the process.
    So a new FastAPI instance is built with the same routes and given the
    middleware before anything touches it. The routes are the production
    routers; only the app shell is new.
    """
    from fastapi import FastAPI

    from platform_core.identity.middleware import TenantContextMiddleware
    from platform_core.main import app as main_app

    fresh = FastAPI()
    for route in main_app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=resolver)
    return TestClient(fresh, raise_server_exceptions=False)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The assembled stack, with the *real* strategy selected by `build_resolver`.

    The dev switch is enabled and settings caching is cleared, so the app runs
    the same branch a local deployment would. Nothing is stubbed between the
    request and the database.
    """
    monkeypatch.setenv("APP_ALLOW_BOOTSTRAP_TOKENS", "true")
    monkeypatch.delenv("APP_OIDC_ISSUER", raising=False)

    from platform_core.config import get_settings
    from platform_core.identity.middleware import build_resolver

    get_settings.cache_clear()
    try:
        yield _fresh_app(build_resolver())
    finally:
        get_settings.cache_clear()


# --- 1. The success path, through HTTP, against a real database -----------


def test_valid_token_reaches_the_endpoint(client: TestClient) -> None:
    """A resolvable token authenticates and returns that tenant's rows.

    This is the test the previous round was missing. `bootstrap_token_resolver`
    had been exercised at the repository layer but never through the middleware,
    so nothing proved the assembled stack could authenticate anybody at all.
    """
    resp = client.get("/v1/cases", headers=_token(SLUG, USER))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    subjects = {item["subject"] for item in body["items"]}
    assert "auth-http case A" in subjects, f"expected the caller's own case, got {subjects}"
    assert "auth-http case B" not in subjects, "another tenant's case leaked into the response"


def test_resolved_tenant_scopes_the_query_not_the_token_slug_alone(
    client: TestClient,
) -> None:
    """Two tenants, two tokens, two disjoint result sets.

    Guards against the RLS binding regressing to "bind the slug we were given"
    instead of "bind the tenant id we resolved". Both would pass a naive
    200-check; only comparing the two tenants' rows distinguishes them.
    """
    first = client.get("/v1/cases", headers=_token(SLUG, USER)).json()
    second = client.get("/v1/cases", headers=_token(OTHER_SLUG, OTHER_USER)).json()
    first_subjects = {item["subject"] for item in first["items"]}
    second_subjects = {item["subject"] for item in second["items"]}
    assert first_subjects.isdisjoint(second_subjects)
    assert "auth-http case A" in first_subjects
    assert "auth-http case B" in second_subjects


# --- 2. Every failure mode is a 401, and they are indistinguishable -------


def test_missing_header_is_401(client: TestClient) -> None:
    resp = client.get("/v1/cases")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_UNRESOLVED"


def test_malformed_scheme_is_401(client: TestClient) -> None:
    """`Token ...` and a bare token must not be accepted as a bearer token."""
    for value in (f"pt_{SLUG}_{USER}", f"Token pt_{SLUG}_{USER}", "Bearer", "Bearer  "):
        resp = client.get("/v1/cases", headers={"Authorization": value})
        assert resp.status_code == 401, f"{value!r} must not authenticate"


def test_unknown_slug_is_401(client: TestClient) -> None:
    resp = client.get("/v1/cases", headers=_token("auth-http-missing", USER))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_UNRESOLVED"


def test_known_user_without_membership_is_401(client: TestClient) -> None:
    """A real user id is not enough; the membership row is what grants access.

    The token format makes the user id a *claim by the client*. If the resolver
    ever trusted it, this request would succeed - STRANGER exists in `users`.
    """
    resp = client.get("/v1/cases", headers=_token(SLUG, STRANGER))
    assert resp.status_code == 401, resp.text


def test_user_from_another_tenant_is_401(client: TestClient) -> None:
    """Mixing a valid slug with another tenant's user must not resolve."""
    resp = client.get("/v1/cases", headers=_token(SLUG, OTHER_USER))
    assert resp.status_code == 401, resp.text


def test_failure_modes_are_indistinguishable_over_http(client: TestClient) -> None:
    """Unknown slug, unknown user, wrong-user-for-slug: one status, one body.

    Verified at the HTTP boundary rather than the repository, because the
    middleware is where an exception could be translated into a distinguishing
    message (or a 500) before it reaches the client.
    """
    responses = []
    for slug, user in (
        ("auth-http-missing", USER),
        (SLUG, str(uuid.uuid4())),
        (SLUG, STRANGER),
        (SLUG, OTHER_USER),
    ):
        resp = client.get("/v1/cases", headers=_token(slug, user))
        responses.append((resp.status_code, resp.json()))
    statuses = {status for status, _ in responses}
    bodies = {str(body) for _, body in responses}
    assert statuses == {401}, f"all failure modes must be 401, got {statuses}"
    assert len(bodies) == 1, f"failure modes must be indistinguishable, got {bodies}"


def test_malformed_token_is_not_a_server_error(client: TestClient) -> None:
    """Garbage in the token must be rejected, not raise.

    A token splitter that indexes blindly turns `pt_` into an IndexError, which
    surfaces as a 500 and tells the caller their input reached the parser.
    """
    for value in (f"Bearer pt_{SLUG}", f"Bearer pt__{USER}", "Bearer pt", "Bearer pt_a_b_c_d"):
        resp = client.get("/v1/cases", headers={"Authorization": value})
        assert resp.status_code == 401, f"{value!r} produced {resp.status_code}: {resp.text}"


# --- 3. The OIDC path is reachable and fails closed -----------------------
#
# A live Keycloak realm is not available here, so the seam is faked: the
# verifier is the boundary, and it is the only thing replaced. Everything
# downstream - membership mapping, RLS binding, the endpoint, the error
# translation - is real.
#
# Note the `system` column contract: `MembershipResolver.resolve` uses
# `claims["iss"]` as the external system id (`oidc.py:116`), not a friendly
# name like "keycloak". Seed rows must therefore key on the issuer URL.

ISSUER = "http://localhost:8081/realms/platform"


@pytest.fixture
def oidc_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Assemble the app with `oidc_token_resolver` and a fake verifier.

    `_oidc_cache` is populated with a stub verifier and a real
    `MembershipResolver` over the app role, so the token -> claims -> membership
    -> tenant context chain is genuine apart from signature verification.
    """
    monkeypatch.setenv("APP_OIDC_ISSUER", ISSUER)
    monkeypatch.delenv("APP_ALLOW_BOOTSTRAP_TOKENS", raising=False)

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.config import get_settings
    from platform_core.identity import middleware as mw
    from platform_core.identity.middleware import build_resolver
    from platform_core.identity.oidc import MembershipResolver

    get_settings.cache_clear()

    class _StubVerifier:
        """Accepts anything shaped like a claim set; refuses the sentinel."""

        def verify(self, token: str) -> dict:
            if token == "reject-me":
                # Mirrors a real verifier: raises on signature/iss/aud/exp
                # failure. The concrete exception type must not matter to the
                # resolver, which is part of what this test asserts.
                raise ValueError("signature verification failed")
            return {
                "sub": f"kc|{token}",
                "iss": ISSUER,
                "aud": "platform-api",
            }

    app_url = os.environ.get(
        "APP_TEST_DATABASE_URL",
        "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
    )
    from platform_core.db import create_engine

    engine = create_engine(app_url)
    mw._oidc_cache = (_StubVerifier(), MembershipResolver(async_sessionmaker(engine)))

    try:
        yield _fresh_app(build_resolver())
    finally:
        mw._oidc_cache = None
        get_settings.cache_clear()


def test_oidc_resolver_is_selected_when_an_issuer_is_configured(
    oidc_client: TestClient,
) -> None:
    """The OIDC path is actually mounted, not merely implemented.

    Asserts the observable consequence rather than the resolver identity: a
    bootstrap-shaped token must now be refused (the OIDC verifier never sees a
    matching `external_identities` row), which can only be true if OIDC is the
    active strategy.
    """
    resp = oidc_client.get("/v1/cases", headers=_token(SLUG, USER))
    assert resp.status_code == 401, "a bootstrap token must not work under OIDC"


def test_oidc_token_maps_to_a_membership_through_external_identities(
    oidc_client: TestClient,
) -> None:
    """Claims -> `external_identities` -> `memberships` -> tenant context.

    The seeded link is `kc|auth-http-subject`; presenting that subject as a
    token must resolve to the same tenant the bootstrap path resolves to.
    """
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO external_identities (id, tenant_id, user_id, system, subject) "
                "VALUES (gen_random_uuid(), :t, :u, :sys, :s) "
                "ON CONFLICT (system, subject) DO NOTHING"
            ),
            {"t": TENANT, "u": USER, "s": "kc|auth-http-subject", "sys": ISSUER},
        )
    admin.dispose()

    resp = oidc_client.get("/v1/cases", headers={"Authorization": "Bearer auth-http-subject"})
    assert resp.status_code == 200, resp.text
    subjects = {item["subject"] for item in resp.json()["items"]}
    assert "auth-http case A" in subjects
    assert "auth-http case B" not in subjects, "OIDC path leaked another tenant's rows"


def test_oidc_membership_mapping_requires_tenant_binding(oidc_client: TestClient) -> None:
    """A subject known to another tenant must not resolve under this one.

    `external_identities` is FORCE-RLS'd like `memberships`, so this pins that
    the OIDC mapper - which runs on a *different* code path from the bootstrap
    resolver - did not quietly fall back to a superuser connection.
    """
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO external_identities (id, tenant_id, user_id, system, subject) "
                "VALUES (gen_random_uuid(), :t, :u, :sys, :s) "
                "ON CONFLICT (system, subject) DO NOTHING"
            ),
            {"t": OTHER_TENANT, "u": OTHER_USER, "s": "kc|auth-http-cross", "sys": ISSUER},
        )
    admin.dispose()

    # The claim is valid for the *other* tenant, but the request carries no
    # slug: the mapper must derive the tenant from its own lookup.
    resp = oidc_client.get("/v1/cases", headers={"Authorization": "Bearer auth-http-cross"})
    assert resp.status_code == 200, resp.text
    subjects = {item["subject"] for item in resp.json()["items"]}
    assert "auth-http case B" in subjects, f"expected tenant B's rows, got {subjects}"
    assert "auth-http case A" not in subjects


def test_oidc_verification_failure_is_401_not_500(oidc_client: TestClient) -> None:
    """A raising verifier is translated, not propagated.

    Note the stub raises `ValueError`, not a bespoke auth exception. The
    resolver catches broadly on purpose - a JWKS fetch failure raises a
    transport error - and leaking any of them as a 500 would both mislead the
    caller and disclose the failure mode.
    """
    resp = oidc_client.get("/v1/cases", headers={"Authorization": "Bearer reject-me"})
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "AUTH_UNRESOLVED"


def test_oidc_missing_bearer_is_401(oidc_client: TestClient) -> None:
    resp = oidc_client.get("/v1/cases")
    assert resp.status_code == 401


# --- 4. The SECURITY DEFINER escape hatch is not a hole in the table ------
#
# `resolve_oidc_identity` runs as its owner precisely so RLS does not filter
# the lookup that has to happen *before* a tenant is known. That is the shape
# of a cross-tenant read, so the same falsification attempts applied to
# `resolve_active_membership` in `test_membership_resolution.py` are applied
# here. If any of these starts returning rows, migration 0016's design is no
# longer valid and the OIDC path is leaking identities.


async def _exec_scalar(sql: str, params: dict | None = None) -> int:
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(APP_URL)
    try:
        async with eng.connect() as conn:
            return int((await conn.execute(text(sql), params or {})).scalar_one())
    finally:
        await eng.dispose()


def _run(coro: object) -> object:
    """psycopg refuses Windows' ProactorEventLoop; Selector is required."""
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)  # type: ignore[arg-type]


def test_oidc_function_reads_behind_rls_but_the_table_still_does_not() -> None:
    """Baseline: the function sees the row, a bare connection does not.

    Without this pair the remaining assertions are vacuous - a function that
    sees nothing leaks nothing, and a table that is readable directly would
    make the function pointless.
    """

    direct = _run(
        _exec_scalar("SELECT count(*) FROM external_identities WHERE subject = :s", {"s": "kc|x"})
    )
    via_fn = _run(
        _exec_scalar(
            "SELECT count(*) FROM resolve_oidc_identity(:sys, :s)",
            {"sys": ISSUER, "s": "kc|auth-http-subject"},
        )
    )
    assert direct == 0, "RLS must still hide external_identities from the app role"
    assert via_fn == 1, "the function must resolve the seeded identity"


def test_oidc_function_cannot_be_widened_by_like_metacharacters() -> None:
    """Subjects are matched as plain text, never as SQL wildcards.

    An attacker cannot authenticate by presenting `%` as a subject. A LIKE-
    based implementation would hand out the first identity in the table.
    """
    for subject in ("%", "_", "kc|%", "' OR '1'='1", "", "kc|auth-http-"):
        n = _run(
            _exec_scalar(
                "SELECT count(*) FROM resolve_oidc_identity(:sys, :s)",
                {"sys": ISSUER, "s": subject},
            )
        )
        assert n == 0, f"subject {subject!r} must not match any row, got {n}"


def test_oidc_function_cannot_be_composed_into_a_table_scan() -> None:
    """The strongest attack: drive the function from a LATERAL join.

    If the function made `external_identities` readable, this would enumerate
    every IdP subject in every tenant. It returns 0 because the base table
    stays FORCE-RLS'd in the outer query.
    """
    sql = (  # noqa: S608 - literal skeleton; both values are bound parameters
        "SELECT count(*) FROM external_identities e, "
        "LATERAL resolve_oidc_identity(e.system, e.subject) r"
    )
    assert _run(_exec_scalar(sql)) == 0, "lateral composition must yield nothing"


def test_oidc_function_attributes_are_pinned() -> None:
    """SECURITY DEFINER, pinned search_path, not owned by the app role.

    An unpinned search_path on a SECURITY DEFINER function is the classic
    privilege-escalation vector: the caller shadows `public` with a look-alike
    `external_identities` table and chooses what the function reads.
    """
    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        row = conn.execute(
            text(
                "SELECT prosecdef, proconfig, pg_get_userbyid(proowner) FROM pg_proc "
                "WHERE proname = 'resolve_oidc_identity'"
            )
        ).one()
    admin.dispose()
    secdef, proconfig, owner = row[0], row[1] or [], row[2]
    assert secdef is True, "the function must be SECURITY DEFINER"
    assert any("search_path" in c for c in proconfig), "search_path must be pinned"
    assert owner != "platform_app", "the function must not be owned by the app role"


def test_oidc_table_grant_is_scoped_by_rls_not_by_privilege() -> None:
    """The app role may hold table SELECT; RLS - not the grant - is the control.

    Worth pinning because it is the opposite of what one might assume, and
    because it means "revoke SELECT" is not a defense here: the guarantees come
    from the FORCE RLS policy, so a future migration that drops
    `relforcerowsecurity` would silently open the table while every privilege
    check kept passing.
    """
    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        has_select = conn.execute(
            text("SELECT has_table_privilege('platform_app', 'external_identities', 'SELECT')")
        ).scalar()
        forced = conn.execute(
            text("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'external_identities'")
        ).scalar()
    admin.dispose()
    assert has_select is True, "the app role is expected to hold SELECT; RLS scopes it"
    assert forced is True, "FORCE RLS is the actual control and must stay on"


def test_oidc_function_requires_an_active_membership_and_tenant() -> None:
    """A suspended tenant must not authenticate, even with a valid identity row.

    The function joins `tenants` and filters on status; this pins that the
    filter is present, since a token minted before suspension stays
    cryptographically valid.
    """
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO external_identities (id, tenant_id, user_id, system, subject) "
                "VALUES (gen_random_uuid(), :t, :u, :sys, 'kc|suspended-subject') "
                "ON CONFLICT (system, subject) DO NOTHING"
            ),
            {"t": TENANT, "u": USER, "sys": ISSUER},
        )
        conn.execute(text("UPDATE tenants SET status = 'suspended' WHERE id = :t"), {"t": TENANT})

    try:
        n = _run(
            _exec_scalar(
                "SELECT count(*) FROM resolve_oidc_identity(:sys, :s)",
                {"sys": ISSUER, "s": "kc|suspended-subject"},
            )
        )
        assert n == 0, "a suspended tenant must not resolve to an identity"
    finally:
        with admin.begin() as conn:
            conn.execute(text("UPDATE tenants SET status = 'active' WHERE id = :t"), {"t": TENANT})
            conn.execute(
                text("DELETE FROM external_identities WHERE subject = 'kc|suspended-subject'")
            )
        admin.dispose()
