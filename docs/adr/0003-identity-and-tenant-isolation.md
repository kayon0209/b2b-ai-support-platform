# ADR 0003: Enterprise Identity, Tenant Resolution and Authorization

- Status: Accepted
- Date: 2026-09-17

## Context

The platform serves multiple enterprise customers ("tenants") from one
deployment. Every request must answer three questions before any business logic
runs: *which tenant is this?*, *who is acting?*, and *what may they do?*.

The failure mode this ADR exists to prevent is **cross-tenant leakage** — one
enterprise seeing another's knowledge, cases or conversations. In this product
that is not a bug, it is a breach: the knowledge corpus is internal policy
documents.

Existing constraints:

- Chatwoot owns conversations and has its own account model. Its account IDs are
  external references, not our tenant IDs (ADR 0001).
- `AGENTS.md`: "Every persisted business row must carry `tenant_id`" and
  "PostgreSQL RLS must protect tenant-owned tables. Application filtering is
  additional defense, not a replacement."
- The authentication entry point is also the tenant-resolution entry point: the
  tenant is derived from the *caller's* identity, never from the request body.

## Decision

### 1. Tenant resolution is server-side, always

`tenant_id` is resolved from the authenticated token on every request, in
`TenantContextMiddleware`, and placed in a `TenantContext`. It is **never** read
from a request body, query parameter or header. Any handler that needs a tenant
takes it from the context.

This is stated as a rule because the tempting shortcut — "accept `tenant_id` in
the payload so the client can be explicit" — silently converts tenant isolation
into a client-side decision. `AGENTS.md` lists it under prohibited shortcuts.

### 2. Three independent layers, not one

```
   token ──▶ OIDC/JWKS verify ──▶ resolve membership ──▶ TenantContext
                                                              │
        ① server-side resolution  ◀──────────────────────────┘
        ② application-level filters (every query takes tenant_id)
        ③ PostgreSQL RLS, FORCE ROW LEVEL SECURITY
```

Layer 3 is the one that must hold even if ① and ② are wrong. Layers 1 and 2
exist because RLS alone gives a poor error and no defence in depth. A missing
filter in application code must therefore be a *narrower* failure, not a leak:
RLS turns it into zero rows.

Policy shape (installed on every tenant-owned table):

```sql
USING (tenant_id::text = current_setting('app.tenant_id', true))
```

with `FORCE ROW LEVEL SECURITY` so the table owner is not exempt. The two
database roles are deliberately split:

- `platform` — superuser, `BYPASSRLS`. Used for migrations, seeding and
  test cleanup only.
- `platform_app` — `NOBYPASSRLS`. Used by the API and workers for everything
  else.

If application code runs as `platform`, isolation is unenforced and the tests
that would catch it are themselves bypassed. This is why the role is a
configuration decision, not an implementation detail.

### 3. OIDC is the production authentication path

Keycloak via `OidcVerifier` (`identity/oidc.py`): RS256 verified against the
issuer's JWKS, with `iss`, `exp`, `iat` and audience/authorized-party checks, a
30-second leeway, and **fail-closed** behaviour — any verification problem raises
`OidcError` rather than falling through to an unauthenticated context.

`build_resolver()` selects the path explicitly:

1. OIDC if `oidc_issuer` is configured;
2. unsigned bootstrap tokens **only** if `allow_bootstrap_tokens` is set, which
   `get_settings()` refuses outside `local`/`test`;
3. otherwise raise — never default to something permissive.

The bootstrap scheme exists so a developer can exercise the API without standing
up Keycloak. It is unsigned, which is exactly why it is gated twice: an explicit
opt-in flag *and* an environment check.

### 4. Membership is the join between an identity and a tenant

`Membership` carries the role, and roles map to `Action`s through
`packages/policy`. The policy layer is a shared *vocabulary*, not a place for
business shortcuts (`AGENTS.md`): it says whether a `support_viewer` may read a
document, not whether a specific document is relevant.

Two deliberate properties:

- **Auth failures are indistinguishable.** Unknown tenant, suspended tenant,
  missing membership and inactive membership all produce the same
  `identity not found or inactive`. Distinguishing them would turn login into a
  tenant/user enumeration oracle.
- **Roles are lowercased enum values in `String` columns.** Any ORM `Enum(...)`
  must pass `values_callable=_enum_values`, or SQLAlchemy looks for member
  *names* and every read raises `LookupError`. This was a real defect, not a
  hypothetical.

### 5. The RLS bootstrap problem, and its one permitted solution

Some reads must happen **before** a tenant is known — that is the whole point of
resolution. RLS makes those reads return zero rows, because the predicate
compares against an unset `current_setting` and yields NULL.

The rejected fix is to loosen the policy:

```sql
-- REJECTED
USING (current_setting('app.tenant_id', true) IS NULL OR tenant_id::text = ...)
```

That grants a table-wide cross-tenant read to any unbound session, i.e. it
removes the protection exactly when the system is least sure who is asking.

The accepted fix is a **narrow `SECURITY DEFINER` function** that answers one
specific question and nothing else:

- `resolve_active_membership(tenant_slug, user_id)` (migration 0015)
- `resolve_oidc_identity(subject)` (migration 0016)
- `claim_ingestion_versions(batch)` (migration 0018)
- `reclaim_stale_ingestions(timeout)` (migration 0019)

Each one is required to:

- pin `SET search_path = pg_catalog, public` (otherwise the function can be
  hijacked by a shadowing object on the caller's search path);
- `REVOKE ALL FROM PUBLIC` and from `platform`;
- `GRANT EXECUTE` to `platform_app` only — never `SELECT` on the table;
- return a narrow projection, not a table;

and each is asserted in tests: metacharacter and wildcard inputs return nothing,
`LATERAL` composition into a table scan still returns nothing, and `DROP
FUNCTION` as the app role is blocked.

## Consequence worth stating: RLS fails silently on writes

The read failure (zero rows) is well known. The **write** failure is the same and
easier to miss:

```
unbound UPDATE document_versions ... WHERE id = <known id>   -- rowcount 0, NO error
bound   UPDATE document_versions ... WHERE id = <known id>   -- rowcount 1
```

A claim that reported success persisted nothing. Therefore: **any `UPDATE` or
`DELETE` issued from an unbound app-role session must assert its `rowcount`.**
When the tenant is discoverable only from the row itself, bind per row — the
tenant comes from the row, so this is not an escalation.

## Consequences

### Positive

- A single implementation of tenant isolation, testable once.
- A missing application filter degrades to "no data", not to "someone else's
  data".
- Production auth needs no bespoke session store.
- The bootstrap path is confirmable as disabled by inspecting configuration.

### Negative

- Two database roles must be provisioned and kept distinct; running the app as
  `platform` disables the safety net silently.
- `SECURITY DEFINER` functions are a deliberately narrow hole in RLS. Each new
  one is a security review, and the count should stay small.
- Claims cached by Chatwoot's account model must be reconciled with our identity
  separately (ADR 0001 boundary).

## Constraints

- Never accept `tenant_id` from client input.
- Never loosen an RLS policy to solve a bootstrap read; add a `SECURITY DEFINER`
  function instead.
- Never grant `SELECT` on a tenant-owned table to bypass RLS.
- Never run the API or workers as `platform`.
- Every new tenant-owned table gets `tenant_id` + RLS in the same migration that
  creates it.
- Auth failures stay indistinguishable to the caller.

## Revisit criteria

Revisit if:

- SAML or SCIM is added (Phase 5) and the identity model can no longer be
  expressed by `Membership` alone;
- a second database or schema appears, at which point "one policy set" stops
  being true and the isolation argument weakens;
- the `SECURITY DEFINER` count grows to the point where the RLS hole is the
  dominant access path rather than a set of exceptions.
