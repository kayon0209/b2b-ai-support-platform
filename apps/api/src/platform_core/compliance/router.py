"""Compliance export endpoints.

    POST /v1/tenant/compliance/export    one bounded, audited extract

Why POST rather than GET
------------------------
The response is a read, but the request has a durable effect: it writes an
audit event recording that a copy of the tenant's data was taken, by whom and
over what window. On this platform that record is what makes the export
legitimate, so it is not a cacheable side effect to be suppressed - and the
platform's rule is that every write-shaped command carries an `Idempotency-Key`.
A GET that quietly appends to the audit log would be the one endpoint whose
retry semantics nobody could reason about.

Authorization
-------------
`COMPLIANCE_EXPORT`, held by roles that already hold both `AUDIT_READ` and
`CASE_READ`. The invariant is asserted in the policy tests: the export must not
be the endpoint through which a role gains access it did not have. The same rule
is why the response carries an audit-event extract - a role that cannot read the
audit log cannot export it either.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from platform_core.api import (
    error_response,
    get_context,
    new_trace_id,
    ok_response,
    require_policy,
    require_write_idempotency,
    tenant_session,
)
from platform_core.compliance import service as compliance
from platform_policy import Action

router = APIRouter(prefix="/v1/tenant/compliance", tags=["compliance"])

_STATUS_BY_CODE: dict[str, int] = {
    # 400: the caller can fix it by sending a narrower window or naming the
    # range. Not 413 - nothing was uploaded, and the fix is a different request
    # rather than a smaller one.
    "EXPORT_SINCE_REQUIRED": 400,
    "EXPORT_WINDOW_INVERTED": 400,
    "EXPORT_WINDOW_TOO_WIDE": 400,
    "EXPORT_UNKNOWN_SECTION": 400,
    "EXPORT_NO_SECTIONS": 400,
}

MESSAGES: dict[str, str] = {
    "EXPORT_SINCE_REQUIRED": "`since` is required: a window must be stated",
    "EXPORT_WINDOW_INVERTED": "`since` must not be after `until`",
    "EXPORT_WINDOW_TOO_WIDE": (
        f"the window must be at most {compliance.MAX_WINDOW_SECONDS // 86400} days; "
        "issue several requests"
    ),
    "EXPORT_UNKNOWN_SECTION": "unknown section",
    "EXPORT_NO_SECTIONS": "at least one section is required",
}


class ExportIn(BaseModel):
    # Required, and no default: see `compliance.service.parse_request`.
    since: int | None = Field(default=None, ge=0)
    until: int | None = Field(default=None, ge=0)
    # Absent means the defaults (`audit`, `cases`), which is what a data
    # protection request usually wants. Naming them explicitly is for the
    # narrower case.
    sections: list[str] | None = None


@router.post("/export")
async def create_export(request: Request, body: ExportIn) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.COMPLIANCE_EXPORT)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.COMPLIANCE_EXPORT)
    if missing_idem is not None:
        return missing_idem

    try:
        export_request = compliance.parse_request(
            since=body.since, until=body.until, sections=body.sections
        )
    except compliance.ComplianceError as exc:
        return error_response(
            exc.code,
            MESSAGES.get(exc.code, exc.code),
            status_code=_STATUS_BY_CODE.get(exc.code, 400),
        )

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        payload = await compliance.build_export(
            session, ctx=ctx, request=export_request, trace_id=trace_id
        )
        await session.commit()
    return ok_response(payload, trace_id=trace_id)


__all__ = ["router"]
