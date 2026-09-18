"""Connector health and reauthorization state (docs/development-plan.md Phase 3).

The defect this module closes
-----------------------------
`ConnectorStatus.NEEDS_REAUTH`, `ConnectorStatus.DEGRADED` and
`Connector.last_health_at` were declared and never written by any code path,
and `ConnectorAdapter.health_check` had zero callers. So the Phase 3
acceptance criterion "OAuth reauthorization is visible and actionable" could
not hold: when a tenant's Jira token expired, the connector stayed `active`,
every tool call failed with `CONNECTOR_AUTH_EXPIRED`, and nothing in the
platform said so.

What "evidence" means here (read this before changing the transitions)
----------------------------------------------------------------------
`ConnectorAdapter.health_check()` in the pilot adapters performs an
**unauthenticated** reachability call (`GET {base}/health`, no Authorization
header - see `crm.py`). That has a direct consequence for the state machine:

    a successful probe does NOT prove the credential works.

Therefore a probe may never clear `NEEDS_REAUTH`. Treating reachability as
evidence of authentication would silently re-arm a connector whose credential
is known to be rejected, and the next thing the platform does with an
"active" connector is execute a write through it. `NEEDS_REAUTH` is cleared
only by an explicit operator action that also demonstrates the credential is
now resolvable - see `can_clear_reauth`.

`DEGRADED` and `NEEDS_REAUTH` are deliberately not interchangeable:
`DEGRADED` means "retry later, this looks transient", `NEEDS_REAUTH` means "a
human must act". Letting a failed probe downgrade `NEEDS_REAUTH` to
`DEGRADED` would hide an action item behind a transient-looking state.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from platform_contracts.events import EventType
from platform_core.audit import service as audit_service
from platform_core.identity.tenant_context import TenantContext
from platform_core.integrations.models import Connector, ConnectorStatus
from platform_core.outbox_service import enqueue as enqueue_event

ACTIVE = ConnectorStatus.ACTIVE.value
DEGRADED = ConnectorStatus.DEGRADED.value
NEEDS_REAUTH = ConnectorStatus.NEEDS_REAUTH.value
DISABLED = ConnectorStatus.DISABLED.value

AUDIT_ACTION = "connector.status_changed"

# Statuses the tool gateway will build an executor from. Mirrors
# `ConnectorExecutorResolver._available_connectors`; kept here so "is this
# connector usable" has one definition rather than two.
EXECUTABLE_STATUSES = frozenset({ACTIVE})


# --- Pure transition rules (no I/O; unit-tested directly) ------------------


def status_after_probe(current: str, *, reachable: bool) -> str:
    """Status implied by a reachability probe.

    - `DISABLED` is operator intent and outranks any probe result: a
      connector someone switched off must not switch itself back on because
      the endpoint answered.
    - `NEEDS_REAUTH` is preserved. The probe is unauthenticated, so it can
      neither confirm nor refute that the credential is rejected; keeping the
      stronger signal is the fail-closed choice.
    - Otherwise a reachable connector is `ACTIVE` and an unreachable one is
      `DEGRADED`.
    """
    if current == DISABLED:
        return DISABLED
    if current == NEEDS_REAUTH:
        return NEEDS_REAUTH
    return ACTIVE if reachable else DEGRADED


def status_after_auth_failure(current: str) -> str:
    """Status implied by a call the provider rejected on authentication.

    This is the one signal that genuinely identifies an expired or revoked
    credential, so it is the only thing that sets `NEEDS_REAUTH`. A disabled
    connector stays disabled: the operator already knows it is not in use,
    and flipping it to `NEEDS_REAUTH` would create an action item for a
    system nobody is calling.
    """
    if current == DISABLED:
        return DISABLED
    return NEEDS_REAUTH


def can_clear_reauth(*, reachable: bool, credential_resolves: bool) -> tuple[bool, str]:
    """Whether an operator may return a connector to `ACTIVE`.

    Two conditions, and both are real evidence rather than a checkbox:

    1. the endpoint is reachable, and
    2. the credential reference resolves to a non-empty credentials mapping.

    (2) is what catches the common misconfiguration - the operator swapped the
    reference to a new variable and forgot to set it. Without it, the API
    would happily report `active` for a connector that still cannot
    authenticate, which is worse than the state it was in.

    Returns `(allowed, reason_code)` so the caller can report why.
    """
    if not reachable:
        return False, "CONNECTOR_UNREACHABLE"
    if not credential_resolves:
        return False, "CREDENTIAL_UNRESOLVED"
    return True, "OK"


def credential_is_present(credentials: dict[str, str]) -> bool:
    """True when a resolved mapping can actually authenticate a call.

    A mapping of empty strings counts as absent: `{"api_token": ""}` produces
    an `Authorization: Bearer ` header, which is not a credential and would
    make the adapter look configured while every call 401s.
    """
    return any(value.strip() for value in credentials.values())


@dataclass(frozen=True)
class StatusChange:
    """A recorded transition, safe to log and assert on."""

    connector_id: uuid.UUID
    previous: str
    current: str
    reason_code: str

    @property
    def changed(self) -> bool:
        return self.previous != self.current


async def _apply(
    session: AsyncSession,
    connector: Connector,
    *,
    new_status: str,
    reason_code: str,
    ctx: TenantContext,
    error_code: str = "",
    trace_id: str | None = None,
) -> StatusChange:
    """Write a status transition, its audit event and its outbox event.

    All three happen in the caller's transaction, so a rolled-back request
    cannot leave a notification about a state that never committed.
    """
    previous = connector.status
    connector.status = new_status
    change = StatusChange(
        connector_id=connector.id,
        previous=previous,
        current=new_status,
        reason_code=reason_code,
    )
    if not change.changed:
        return change

    await audit_service.record(
        session,
        ctx=ctx,
        action=AUDIT_ACTION,
        resource_type="connector",
        resource_id=connector.id,
        decision=new_status,
        reason_code=reason_code[:63],
        before={"status": previous},
        after={"status": new_status},
        trace_id=trace_id,
    )
    if new_status == NEEDS_REAUTH:
        # The alertable half of "visible and actionable": a tenant-side
        # consumer (email, IM, ticket) subscribes to this rather than polling
        # the API. Emitted only on the transition into NEEDS_REAUTH, so a
        # connector that fails repeatedly notifies once.
        await enqueue_event(
            session,
            tenant_id=connector.tenant_id,
            event_type=EventType.CONNECTOR_NEEDS_REAUTH.value,
            aggregate_type="connector",
            aggregate_id=str(connector.id),
            payload={
                "connector_id": str(connector.id),
                "provider": connector.provider,
                "error_code": error_code or reason_code,
            },
            trace_id=trace_id,
        )
    return change


async def record_probe(
    session: AsyncSession,
    connector: Connector,
    *,
    reachable: bool,
    ctx: TenantContext,
    now: int | None = None,
    trace_id: str | None = None,
) -> StatusChange:
    """Record one health probe: timestamp plus any resulting transition.

    `last_health_at` is written on every probe regardless of outcome - it
    answers "when did we last look", which is what an operator needs to know
    before trusting the status.
    """
    connector.last_health_at = now if now is not None else int(time.time())
    target = status_after_probe(connector.status, reachable=reachable)
    return await _apply(
        session,
        connector,
        new_status=target,
        reason_code="PROBE_OK" if reachable else "PROBE_UNREACHABLE",
        ctx=ctx,
        trace_id=trace_id,
    )


async def record_auth_failure(
    session: AsyncSession,
    connector: Connector,
    *,
    error_code: str,
    ctx: TenantContext,
    trace_id: str | None = None,
) -> StatusChange:
    """Park a connector in NEEDS_REAUTH after a rejected external call."""
    return await _apply(
        session,
        connector,
        new_status=status_after_auth_failure(connector.status),
        reason_code="AUTH_REJECTED",
        ctx=ctx,
        error_code=error_code,
        trace_id=trace_id,
    )


async def clear_reauth(
    session: AsyncSession,
    connector: Connector,
    *,
    reachable: bool,
    credential_resolves: bool,
    ctx: TenantContext,
    now: int | None = None,
    trace_id: str | None = None,
) -> tuple[StatusChange | None, str]:
    """Return a connector to ACTIVE, or explain why it cannot be.

    Returns `(change, reason_code)`. `change` is None when the request was
    refused, in which case nothing was written - a refused reactivation must
    not record a transition it did not make.
    """
    allowed, reason = can_clear_reauth(reachable=reachable, credential_resolves=credential_resolves)
    if not allowed:
        return None, reason

    connector.last_health_at = now if now is not None else int(time.time())
    change = await _apply(
        session,
        connector,
        new_status=ACTIVE,
        reason_code="REACTIVATED",
        ctx=ctx,
        trace_id=trace_id,
    )
    return change, "OK"


def status_is_executable(status: str) -> bool:
    return status in EXECUTABLE_STATUSES
