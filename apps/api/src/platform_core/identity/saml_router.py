"""SAML endpoints: the browser-facing login flow, and connection administration.

    GET  /v1/saml/{connection_id}/login              redirect to the IdP
    POST /v1/saml/{connection_id}/acs                assertion consumer service
    GET  /v1/identity/saml/connections               this tenant's IdPs
    POST /v1/identity/saml/connections               register one
    POST /v1/identity/saml/connections/{id}/status   enable / disable

The two public endpoints are unauthenticated by necessity - a browser arriving
from the IdP has no bearer token - and they authenticate by signature instead.
Both are under the `/v1/saml/` prefix, which `identity/middleware.py` exempts
from tenant resolution; the tenant is established *by the flow itself*: from
the connection id via the SECURITY DEFINER resolver, and then from the verified
assertion.

What the ACS returns, and what it deliberately does not
-------------------------------------------------------
It returns the resolved identity - user, membership, role - and sets no cookie
and issues no token. API authentication is OIDC bearer tokens from Keycloak, and
minting a second kind of credential here would create a less-examined path to
customer data. The endpoint's job is the one SAML exists for in this deployment:
prove the identity, refuse anyone the tenant never granted a role, and write it
to the audit trail.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
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
from platform_core.config import get_settings
from platform_core.db import app_role_url, session_scope_with_url
from platform_core.identity import saml as saml_proto
from platform_core.identity import saml_service
from platform_core.identity.tenant_context import TenantContext
from platform_policy import Action

router = APIRouter(tags=["saml"])

INVALID_SIGNATURE = "SAML_INVALID"
UNKNOWN_CONNECTION = "SAML_CONNECTION_UNKNOWN"
NOT_REGISTERED = "SAML_NO_MEMBERSHIP"
REPLAYED = "SAML_ASSERTION_REPLAYED"
SAML_REQUEST_INVALID = "SAML_REQUEST_INVALID"
UNKNOWN = "SAML_ERROR"


def _public_error(code: str) -> Any:
    """One response for every validation failure.

    The internal codes are kept in the audit trail and in the metric, not in the
    response: telling a caller *why* an assertion failed to verify is telling an
    attacker which part of a forged response to fix. The user-facing signal they
    need is "this login did not work, try again", and the operator's signal is
    the audit row.
    """
    return error_response(
        INVALID_SIGNATURE,
        "the SAML response could not be validated",
        status_code=401,
    )


@router.get("/v1/saml/{connection_id}/login")
async def start_login(request: Request, connection_id: uuid.UUID) -> Any:
    settings = get_settings()
    async with session_scope_with_url(app_role_url()) as session:
        config = await saml_service.resolve_connection(session, connection_id=connection_id)
    if config is None:
        # A 404 would distinguish "no such connection" from "disabled" and let a
        # caller enumerate connection ids; both are the same answer here.
        return error_response(UNKNOWN_CONNECTION, "unknown identity provider", status_code=404)
    if config.status != "active":
        return error_response(UNKNOWN_CONNECTION, "unknown identity provider", status_code=404)

    request_id = saml_proto.new_request_id()
    relay_state = saml_proto.encode_relay_state(
        request_id=request_id, connection_id=connection_id, secret=_secret(settings)
    )
    acs_url = str(request.url_for("saml_acs", connection_id=str(connection_id)))
    target = saml_proto.build_authn_request_url(
        config=config, acs_url=acs_url, request_id=request_id, relay_state=relay_state
    )
    return RedirectResponse(target, status_code=307)


@router.post("/v1/saml/{connection_id}/acs", name="saml_acs")
async def saml_acs(
    request: Request,
    SAMLResponse: str = Form(...),  # noqa: N803 - the SAML binding fixes these names
    RelayState: str = Form(""),  # noqa: N803
) -> Any:
    import base64

    settings = get_settings()
    trace_id = new_trace_id()
    connection_id = connection_id_of(request)
    acs_url = str(request.url_for("saml_acs", connection_id=str(connection_id)))

    async with session_scope_with_url(app_role_url()) as session:
        config = await saml_service.resolve_connection(session, connection_id=connection_id)
    if config is None or config.status != "active":
        return error_response(UNKNOWN_CONNECTION, "unknown identity provider", status_code=404)

    try:
        expected_request_id = saml_proto.decode_relay_state(
            RelayState, secret=_secret(settings), connection_id=config.connection_id
        )
        raw = base64.b64decode(SAMLResponse, validate=True)
        identity = saml_proto.validate_response(
            config=config, xml=raw, acs_url=acs_url, expected_request_id=expected_request_id
        )
    except (saml_proto.SamlError, ValueError):
        # Audited against the connection's tenant, because a run of these is the
        # signal that somebody is probing an IdP integration.
        await _audit_refusal(config, trace_id=trace_id, reason="VALIDATION_FAILED")
        return _public_error(INVALID_SIGNATURE)

    ctx = TenantContext(tenant_id=config.tenant_id, actor_id=None, actor_kind="user")
    async with tenant_session(ctx) as session:
        from platform_core.identity.tenant_context import apply_rls_tenant

        await apply_rls_tenant(session, ctx)
        fresh = await saml_service.record_assertion_once(
            session,
            tenant_id=config.tenant_id,
            connection_id=config.connection_id,
            assertion_id=identity.assertion_id,
        )
        if not fresh:
            await session.commit()
            await _audit_refusal(config, trace_id=trace_id, reason="REPLAYED")
            return error_response(
                REPLAYED,
                "this assertion has already been used",
                status_code=401,
            )

        try:
            login = await saml_service.resolve_login(
                session,
                ctx=ctx,
                connection=config,
                name_id=identity.name_id,
                attributes=identity.attributes,
                trace_id=trace_id,
            )
        except saml_service.SamlServiceError as exc:
            await session.commit()  # keep the replay record: the assertion was used
            await _audit_refusal(config, trace_id=trace_id, reason=exc.code)
            return error_response(
                NOT_REGISTERED,
                "this identity has no active membership in the tenant",
                status_code=403,
            )
        payload = {
            "user_id": str(login.user_id),
            "membership_id": str(login.membership_id),
            "role": login.role,
            "created_user": login.created_user,
            "connection_id": str(config.connection_id),
        }
        await session.commit()
    return ok_response(payload, trace_id=trace_id)


def connection_id_of(request: Request) -> uuid.UUID:
    return uuid.UUID(str(request.path_params["connection_id"]))


async def _audit_refusal(
    config: saml_proto.SamlConnectionConfig, *, trace_id: str, reason: str
) -> None:
    """Record the refusal in the tenant's own trail, best-effort.

    Best-effort because the alternative is failing a request that has already
    been refused: the login did not happen either way, and an audit write that
    cannot land must not turn a clean 401 into a 500.
    """
    from platform_core.audit import service as audit_service
    from platform_core.identity.tenant_context import apply_rls_tenant

    ctx = TenantContext(tenant_id=config.tenant_id, actor_id=None, actor_kind="user")
    try:
        async with session_scope_with_url(app_role_url()) as session:
            await apply_rls_tenant(session, ctx)
            await audit_service.record(
                session,
                ctx=ctx,
                action=saml_service.AUDIT_LOGIN_REFUSED,
                resource_type="saml_connection",
                resource_id=config.connection_id,
                decision="denied",
                reason_code=reason,
                trace_id=trace_id,
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - see the docstring
        return


def _secret(settings: Any) -> str:
    value = settings.secret_key
    raw = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value or "")
    if not raw:
        # No default. A missing signing secret would make every RelayState
        # forgeable, so the flow refuses rather than falling back to something
        # predictable.
        raise saml_proto.SamlError("SAML_SIGNING_SECRET_MISSING")
    return raw


# --- administration ---------------------------------------------------------


class SamlConnectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    idp_entity_id: str = Field(min_length=1, max_length=512)
    idp_sso_url: str = Field(min_length=1, max_length=1024)
    # PEM, public certificate. `min_length` is generous rather than zero because
    # an empty certificate produces a connection that fails on the first login
    # instead of at creation.
    idp_certificate: str = Field(min_length=64)
    sp_entity_id: str = Field(min_length=1, max_length=512)


class SamlStatusIn(BaseModel):
    status: str = Field(max_length=31)


def _out(row: Any) -> dict[str, Any]:
    return {
        "connection_id": str(row.id),
        "name": row.name,
        "idp_entity_id": row.idp_entity_id,
        "idp_sso_url": row.idp_sso_url,
        "sp_entity_id": row.sp_entity_id,
        "status": row.status,
        # The certificate is returned because an operator comparing it against
        # the IdP's metadata is the only way to catch a rotated certificate
        # before every login starts failing. It is public material.
        "idp_certificate_sha256": __import__("hashlib")
        .sha256(row.idp_certificate.encode())
        .hexdigest(),
    }


@router.get("/v1/identity/saml/connections")
async def list_connections(request: Request) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.TENANT_ADMIN)
    if denied is not None:
        return denied

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        rows = await saml_service.list_connections(session, tenant_id=ctx.tenant_id)
        return ok_response({"connections": [_out(r) for r in rows]}, trace_id=trace_id)


@router.post("/v1/identity/saml/connections")
async def create_connection(request: Request, body: SamlConnectionIn) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.TENANT_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.TENANT_ADMIN)
    if missing_idem is not None:
        return missing_idem

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        try:
            row = await saml_service.create_connection(
                session,
                ctx=ctx,
                name=body.name,
                idp_entity_id=body.idp_entity_id,
                idp_sso_url=body.idp_sso_url,
                idp_certificate=body.idp_certificate,
                sp_entity_id=body.sp_entity_id,
                trace_id=trace_id,
            )
        except saml_service.SamlServiceError as exc:
            status = 409 if exc.code == "CONNECTION_NAME_TAKEN" else 400
            return error_response(exc.code, str(exc).split(": ", 1)[-1], status_code=status)
        payload = _out(row)
        await session.commit()
    return ok_response({"connection": payload}, trace_id=trace_id)


@router.post("/v1/identity/saml/connections/{connection_id}/status")
async def set_status(request: Request, connection_id: uuid.UUID, body: SamlStatusIn) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.TENANT_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.TENANT_ADMIN)
    if missing_idem is not None:
        return missing_idem

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        row = await saml_service.find_connection(
            session, tenant_id=ctx.tenant_id, connection_id=connection_id
        )
        if row is None:
            return error_response(
                "SAML_CONNECTION_UNKNOWN", "connection not found", status_code=404
            )
        try:
            changed = await saml_service.set_connection_status(
                session, ctx=ctx, connection=row, status=body.status, trace_id=trace_id
            )
        except saml_service.SamlServiceError as exc:
            return error_response(exc.code, str(exc), status_code=400)
        payload = _out(row)
        await session.commit()
    return ok_response({"connection": payload, "changed": changed}, trace_id=trace_id)


__all__ = ["router"]
