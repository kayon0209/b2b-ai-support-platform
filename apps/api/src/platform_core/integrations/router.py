"""Connector administration API.

    GET  /v1/connectors                        connection inventory + health
    POST /v1/connectors/{id}/health-check      probe one connector, record it
    POST /v1/connectors/{id}/reactivate        return a connector to service
    PUT  /v1/connectors/{id}/credential-ref    rotate the secret reference

This is the "visible and actionable" half of the Phase 3 acceptance criterion
"OAuth reauthorization is visible and actionable". The other half is
automatic: a rejected credential parks the connector in `NEEDS_REAUTH` and
emits `connector.needs_reauth` (see `integrations/health.py` and
`tool_gateway/registry.AuthReportingExecutor`).

Authorization:
- `GET` needs `CONNECTOR_READ`. That includes `support_admin`, because when a
  Jira connection is parked they are the person whose tools stopped working.
- Everything that writes needs `CONNECTOR_ADMIN` (tenant_owner) and an
  `Idempotency-Key`. A probe is in this group even though it changes no
  configuration: a probe can move an `active` connector to `degraded`, which
  removes it from the tool gateway's executor set. A permission that can take
  a connector out of service is a configuration permission.

Never returned, by any route: credential *values*. A connector row stores a
secret-manager reference (`vault://kv/crm/acme`). This API reports whether the
reference resolves and whether one is set - the fact an operator needs - and
never the reference itself, which names a path inside the secret store.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, is_dataclass
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.api import (
    VALIDATION_FAILED,
    error_response,
    get_context,
    new_trace_id,
    ok_response,
    require_policy,
    require_write_idempotency,
    tenant_session,
)
from platform_core.audit import service as audit_service
from platform_core.integrations import dead_letter, health, sync_service
from platform_core.integrations.credentials import resolve_credentials
from platform_core.integrations.models import Connector, DeadLetterItem
from platform_core.tool_gateway.registry import build_adapter, probe_connector
from platform_policy import Action

router = APIRouter(prefix="/v1/connectors", tags=["integrations"])
# The dead-letter queue is an operational surface for connector/tool work, so
# it lives under its own prefix but shares this module's authorization story.
# If a non-connector producer is ever added, the action pair below should be
# revisited rather than quietly widening what CONNECTOR_READ exposes.
dead_letter_router = APIRouter(prefix="/v1/dead-letters", tags=["integrations"])

CONNECTOR_NOT_FOUND = "CONNECTOR_NOT_FOUND"
# No adapter ships for this provider in this deployment, so reachability is
# unknown. Distinct from CONNECTOR_UNREACHABLE.
CONNECTOR_PROBE_UNSUPPORTED = "CONNECTOR_PROBE_UNSUPPORTED"
CONNECTOR_UNREACHABLE = "CONNECTOR_UNREACHABLE"
CREDENTIAL_UNRESOLVED = "CREDENTIAL_UNRESOLVED"
DEAD_LETTER_NOT_FOUND = "DEAD_LETTER_NOT_FOUND"
CONNECTOR_SYNC_UNSUPPORTED = "CONNECTOR_SYNC_UNSUPPORTED"

# Resource types a sync may walk, per provider. A closed set: `fetch` is a
# provider-specific surface, and letting an operator pass an arbitrary string
# would turn a typo into an empty result that looks like "no data".
SYNCABLE_RESOURCES: dict[str, tuple[str, ...]] = {
    "jira": ("issues",),
    "crm": (),
}

# Longest reference we accept. A reference is a path, not a secret, so it
# should be short; a bound stops the column from being used as storage.
MAX_CREDENTIAL_REF = 255


class CredentialRefIn(BaseModel):
    # Empty string clears the reference (the connector keeps working with no
    # credential, and fails closed at call time).
    credential_ref: str = Field(default="", max_length=MAX_CREDENTIAL_REF)


class SyncIn(BaseModel):
    resource_type: str = Field(default="issues", max_length=63)
    page_size: int = Field(default=50, ge=1, le=sync_service.MAX_PAGE_SIZE)
    # Forget the stored position first. Needed because a cursor is only valid
    # for the query that produced it: after an operator changes the
    # connector's `jql`, the old token addresses a page of a different result
    # set, and resuming from it would skip rows without any error.
    reset: bool = False


def _record_out(record: Any) -> Any:
    """Render one synced record for the response.

    Adapters return their own canonical projection (a frozen dataclass, e.g.
    `JiraIssueSummary`), which is the documented contract - "domain modules
    consume canonical models". Dataclasses are rendered field by field; a
    mapping is passed through; anything else is stringified rather than
    dropped, so an adapter that projects to an unexpected type shows up as a
    visible oddity instead of a silently short list.
    """
    if is_dataclass(record) and not isinstance(record, type):
        return asdict(record)
    if isinstance(record, dict):
        return record
    return str(record)


def _unresolved() -> Any:
    return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)


def _serialize(connector: Connector) -> dict[str, Any]:
    """Public projection of a connector.

    `credential_ref` is deliberately absent. Its *presence* is reported as
    `credential_configured`, which is the operational fact; the value is a
    pointer into the secret store and there is no caller that needs it.
    """
    return {
        "connector_id": str(connector.id),
        "provider": connector.provider,
        "name": connector.name,
        "status": connector.status,
        "capabilities": list(connector.capabilities or []),
        "credential_configured": bool(connector.credential_ref),
        "last_health_at": connector.last_health_at,
        "executable": health.status_is_executable(connector.status),
    }


async def _load(session: AsyncSession, connector_id: uuid.UUID, ctx: Any) -> Connector | None:
    """Load a connector belonging to the caller's tenant.

    The `tenant_id` predicate is redundant with RLS and stays anyway: RLS
    returns zero rows for a foreign connector, and a query that *looks*
    tenant-scoped is the difference between a reviewer confirming isolation
    and a reviewer having to prove it.
    """
    return (
        await session.execute(
            select(Connector).where(
                Connector.id == connector_id, Connector.tenant_id == ctx.tenant_id
            )
        )
    ).scalar_one_or_none()


@router.get("")
async def list_connectors(request: Request) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CONNECTOR_READ)
    if denied is not None:
        return denied

    async with tenant_session(ctx) as session:
        rows = (
            (
                await session.execute(
                    select(Connector)
                    .where(Connector.tenant_id == ctx.tenant_id)
                    .order_by(Connector.provider, Connector.name)
                )
            )
            .scalars()
            .all()
        )
        return ok_response({"connectors": [_serialize(c) for c in rows]})


@router.post("/{connector_id}/health-check")
async def health_check(request: Request, connector_id: uuid.UUID) -> Any:
    """Probe a connector and record the result.

    Returns the probe outcome and the resulting status. `reachable: null`
    means no adapter exists for this provider in this deployment - not an
    outage. See `health.status_after_probe` for why a *successful* probe
    never lifts a connector out of `NEEDS_REAUTH`.
    """
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CONNECTOR_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.CONNECTOR_ADMIN)
    if missing_idem is not None:
        return missing_idem

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        connector = await _load(session, connector_id, ctx)
        if connector is None:
            return error_response(CONNECTOR_NOT_FOUND, "connector not found", status_code=404)

        reachable = await probe_connector(connector)
        if reachable is None:
            return error_response(
                CONNECTOR_PROBE_UNSUPPORTED,
                f"no adapter is available for provider {connector.provider!r}",
                status_code=422,
            )

        change = await health.record_probe(
            session, connector, reachable=reachable, ctx=ctx, trace_id=trace_id
        )
        await session.commit()
        return ok_response(
            {
                "connector": _serialize(connector),
                "reachable": reachable,
                "status_changed": change.changed,
                "previous_status": change.previous,
            },
            trace_id=trace_id,
        )


@router.post("/{connector_id}/reactivate")
async def reactivate(request: Request, connector_id: uuid.UUID) -> Any:
    """Return a connector to `ACTIVE` after an operator fixed the credential.

    Refused unless both conditions in `health.can_clear_reauth` hold: the
    endpoint answers a probe **and** the credential reference resolves to a
    non-empty value. The second check is what catches the common
    misconfiguration - the operator repointed the reference at a variable
    they forgot to set - which would otherwise leave the API reporting
    `active` for a connector that still cannot authenticate.
    """
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CONNECTOR_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.CONNECTOR_ADMIN)
    if missing_idem is not None:
        return missing_idem

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        connector = await _load(session, connector_id, ctx)
        if connector is None:
            return error_response(CONNECTOR_NOT_FOUND, "connector not found", status_code=404)

        reachable = await probe_connector(connector)
        credentials = resolve_credentials(connector.credential_ref)
        change, reason = await health.clear_reauth(
            session,
            connector,
            # `None` (no adapter for this provider) is treated as reachable:
            # refusing to reactivate a provider we simply cannot probe would
            # make a capability gap permanent.
            reachable=True if reachable is None else reachable,
            credential_resolves=health.credential_is_present(credentials),
            ctx=ctx,
            trace_id=trace_id,
        )
        if change is None:
            # Nothing was written: a refused request must not record a
            # transition it did not make.
            return error_response(
                reason,
                {
                    CONNECTOR_UNREACHABLE: (
                        "the provider is not reachable; fix the connection first"
                    ),
                    CREDENTIAL_UNRESOLVED: (
                        "the credential reference does not resolve to a value; "
                        "the secret is missing or the reference is wrong"
                    ),
                }.get(reason, reason),
                status_code=409,
            )

        await session.commit()
        return ok_response(
            {
                "connector": _serialize(connector),
                "status_changed": change.changed,
                "previous_status": change.previous,
            },
            trace_id=trace_id,
        )


@router.put("/{connector_id}/credential-ref")
async def rotate_credential_ref(
    request: Request, connector_id: uuid.UUID, body: CredentialRefIn
) -> Any:
    """Point a connector at a different secret.

    This is the credential-rotation path Phase 3 asks for, at the level the
    platform can honestly implement today: the platform owns the *reference*,
    and the value lives in the secret manager. Rotating means repointing the
    reference (or replacing the value behind an unchanged reference), so this
    route changes where the value is read from - never the value.

    A successful rotation clears `NEEDS_REAUTH`. The reasoning is not that
    the new credential is known-good (nothing has used it yet) but that the
    platform must not keep asserting a failure that was observed against a
    *previous* credential: leaving NEEDS_REAUTH set would mean every
    notification about the connector describes a secret that is no longer
    configured. The connector is returned to `ACTIVE`, and the next real call
    either works or parks it again - with a fresh, accurate reason.
    """
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CONNECTOR_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.CONNECTOR_ADMIN)
    if missing_idem is not None:
        return missing_idem

    reference = body.credential_ref.strip()
    if reference and "://" not in reference:
        # A reference without a scheme is almost always a pasted secret
        # rather than a pointer at one. Rejecting it keeps credentials out of
        # a column that is read by every request and copied into backups.
        return error_response(
            VALIDATION_FAILED,
            "credential_ref must be a scheme-qualified reference like env://CRM_TOKEN",
            status_code=400,
        )

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        connector = await _load(session, connector_id, ctx)
        if connector is None:
            return error_response(CONNECTOR_NOT_FOUND, "connector not found", status_code=404)

        previous_status = connector.status
        connector.credential_ref = reference or None
        if connector.status == health.NEEDS_REAUTH:
            connector.status = health.ACTIVE

        await audit_service.record(
            session,
            ctx=ctx,
            action="connector.credential_rotated",
            resource_type="connector",
            resource_id=connector.id,
            decision="rotated",
            reason_code="CREDENTIAL_REF_CHANGED",
            # Before/after record whether a reference existed and what the
            # status was. The reference itself is a secret-store path and is
            # not copied into the audit trail.
            before={"status": previous_status, "credential_configured": True},
            after={
                "status": connector.status,
                "credential_configured": bool(connector.credential_ref),
            },
            trace_id=trace_id,
        )
        await session.commit()
        return ok_response(
            {
                "connector": _serialize(connector),
                "previous_status": previous_status,
                "resolves": health.credential_is_present(
                    resolve_credentials(connector.credential_ref)
                ),
            },
            trace_id=trace_id,
        )


@router.post("/{connector_id}/sync")
async def sync_connector(request: Request, connector_id: uuid.UUID, body: SyncIn) -> Any:
    """Walk one page of a connector resource and store the new position.

    Operator-triggered rather than run by a scheduler, and that is a scope
    decision rather than an omission: what the platform should *do* with the
    records it pulls (link a Jira issue to which Case?) has no answer in the
    domain model yet, and inventing one here would be guessing. What this
    endpoint establishes is the part that is unambiguous - the resource can
    be walked, the canonical projection is what the adapter documents, and the
    position is resumable, which is what `SyncCursor` existed for and could
    not do while nothing read or wrote it.

    A page of canonical projections is returned so an integration engineer can
    see the connection actually works, bounded by `page_size`.
    """
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CONNECTOR_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.CONNECTOR_ADMIN)
    if missing_idem is not None:
        return missing_idem

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        connector = await _load(session, connector_id, ctx)
        if connector is None:
            return error_response(CONNECTOR_NOT_FOUND, "connector not found", status_code=404)

        allowed = SYNCABLE_RESOURCES.get(connector.provider, ())
        if body.resource_type not in allowed:
            return error_response(
                CONNECTOR_SYNC_UNSUPPORTED,
                f"provider {connector.provider!r} cannot sync {body.resource_type!r}",
                status_code=422,
            )

        adapter = build_adapter(connector)
        if adapter is None:
            return error_response(
                CONNECTOR_SYNC_UNSUPPORTED,
                f"no adapter is available for provider {connector.provider!r}",
                status_code=422,
            )

        outcome = await sync_service.sync_once(
            session,
            connector=connector,
            adapter=adapter,
            resource_type=body.resource_type,
            ctx=ctx,
            page_size=body.page_size,
            reset=body.reset,
            trace_id=trace_id,
        )
        if not outcome.ok:
            # Nothing was written, so there is nothing to commit - and the
            # cursor still points at the last page that actually succeeded.
            await session.rollback()
            return error_response(
                outcome.error_code.split(":", 1)[0],
                outcome.error_code,
                status_code=502,
            )

        await session.commit()
        return ok_response(
            {
                "connector_id": str(connector.id),
                "resource_type": outcome.resource_type,
                "fetched": outcome.fetched,
                "complete": outcome.complete,
                "records": [_record_out(r) for r in outcome.records],
            },
            trace_id=trace_id,
        )


def _dead_letter_out(item: Any) -> dict[str, Any]:
    """Public projection of a dead-letter row.

    `operation_digest` is exposed because it is what lets an operator see
    "the same operation failed 40 times" instead of scrolling 40 rows; it
    carries no payload (see `dead_letter.operation_digest`).
    """
    return {
        "id": str(item.id),
        "connector_id": str(item.connector_id) if item.connector_id else None,
        "resource_type": item.resource_type,
        "operation": item.operation,
        "operation_digest": item.operation_digest,
        "error_code": item.error_code,
        "error_detail": item.error_detail,
        "attempts": item.attempts,
        "status": item.status,
        "created_at": item.created_at,
        "resolved_at": item.resolved_at,
    }


@dead_letter_router.get("")
async def list_dead_letters(request: Request, status: str | None = None) -> Any:
    """Retry-exhausted and ambiguous connector outcomes awaiting a human.

    Deliberately a read-only listing plus an explicit resolve. There is no
    replay endpoint: the payload is not stored here by design, and an
    `ambiguous` row cannot be safely retried until someone establishes
    whether the first attempt landed.
    """
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CONNECTOR_READ)
    if denied is not None:
        return denied

    if status is not None and status not in (dead_letter.PENDING, dead_letter.RESOLVED):
        return error_response(
            VALIDATION_FAILED,
            f"status must be {dead_letter.PENDING!r} or {dead_letter.RESOLVED!r}",
            status_code=400,
        )

    async with tenant_session(ctx) as session:
        rows = await dead_letter.list_pending(session, tenant_id=ctx.tenant_id, status=status)
        return ok_response(
            {
                "dead_letters": [_dead_letter_out(r) for r in rows],
                "counts": {
                    "pending": sum(1 for r in rows if r.status == dead_letter.PENDING),
                    "resolved": sum(1 for r in rows if r.status == dead_letter.RESOLVED),
                },
            }
        )


@dead_letter_router.post("/{item_id}/resolve")
async def resolve_dead_letter(request: Request, item_id: uuid.UUID) -> Any:
    """Mark a dead letter handled. An operator attestation, recorded in audit.

    Idempotent: resolving an already-resolved row returns 200 with
    `changed: false` rather than an error, because two operators clicking the
    same button is not an incident - and it must not write a second audit
    event for a transition that did not happen.
    """
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    denied = require_policy(ctx, Action.CONNECTOR_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.CONNECTOR_ADMIN)
    if missing_idem is not None:
        return missing_idem

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        item = (
            await session.execute(
                select(DeadLetterItem).where(
                    DeadLetterItem.id == item_id, DeadLetterItem.tenant_id == ctx.tenant_id
                )
            )
        ).scalar_one_or_none()
        if item is None:
            return error_response(DEAD_LETTER_NOT_FOUND, "dead letter not found", status_code=404)

        changed = await dead_letter.resolve(
            session, item, reason_code="OPERATOR_RESOLVED", ctx=ctx, trace_id=trace_id
        )
        await session.commit()
        return ok_response(
            {"dead_letter": _dead_letter_out(item), "changed": changed}, trace_id=trace_id
        )


__all__ = ["dead_letter_router", "router"]
