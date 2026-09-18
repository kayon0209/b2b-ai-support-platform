"""Versioned business event contracts.

AGENTS.md: "Every inbound webhook and write command requires an idempotency
key" and "Persist inbound events before enqueueing work". This module is the
shared vocabulary the outbox emits and the relay consumes, so a producer and
a consumer deployed at different times can still agree on what a payload
means.

Two rules hold the whole thing together:

1. **Every emitted event type has a payload schema here.** An event without a
   schema is a contract nobody can rely on, and a consumer cannot tell a
   missing field from a renamed one.
2. **Schemas are additive-only within a version.** Removing or retyping a
   field is a new `event_version`, because a relay running the old code is
   still draining rows written by the new one.

The registry is intentionally small and explicit rather than discovered:
adding an event is a deliberate act, and `test_event_types_are_declared`
fails if someone emits a type that is not registered.
"""

from __future__ import annotations

import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

EVENT_VERSION = 1


class EventType(enum.StrEnum):
    """Emitted business events. Add here before emitting anywhere."""

    CASE_CREATED = "case.created"
    CASE_UPDATED = "case.updated"
    # Emitted when an agent run reaches a terminal, billable outcome. Carries
    # the metering facts (route, token counts) a billing consumer needs.
    USAGE_RECORDED = "usage.recorded"
    # Emitted when an outbound connector call is rejected on authentication
    # and the connector is parked in NEEDS_REAUTH. This is the alertable half
    # of "OAuth reauthorization is visible and actionable"
    # (docs/development-plan.md Phase 3): without an event there is no
    # consumer to notify the tenant, and the tenant only discovers the
    # expired credential when a support agent's tool call fails.
    CONNECTOR_NEEDS_REAUTH = "connector.needs_reauth"


class InboundEventType(enum.StrEnum):
    """Events the platform *receives* and persists before working on them.

    Distinct from `EventType` because the two travel different paths with
    different guarantees: inbound events are deduped by delivery id and
    drive work, outbound events are written transactionally and fan out to
    consumers. Sharing one vocabulary would let a producer accidentally
    emit an inbound name into the outbox (or vice versa) and have it look
    legitimate.
    """

    MESSAGE_CREATED = "message_created"


class AggregateType(enum.StrEnum):
    CASE = "case"
    AGENT_RUN = "agent_run"
    CONNECTOR = "connector"


class _Strict(BaseModel):
    """Reject unknown fields.

    Silently ignoring an unexpected key hides a producer that has drifted
    ahead of the schema, which is exactly when the consumer needs to know.
    """

    model_config = ConfigDict(extra="forbid")


class CaseCreatedPayload(_Strict):
    case_id: str = Field(min_length=1)
    status: str = Field(min_length=1)


class CaseUpdatedPayload(_Strict):
    case_id: str = Field(min_length=1)
    command: str = Field(min_length=1)
    status: str = Field(min_length=1)
    version: int = Field(ge=1)


class UsageRecordedPayload(_Strict):
    """One billable agent run reaching a terminal outcome.

    `route` and the token counts are the metering facts: a consumer can bill
    per run, per token, or both without re-reading the run row.
    """

    run_id: str = Field(min_length=1)
    route: str = Field(min_length=1)
    status: str = Field(min_length=1)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)


class ConnectorNeedsReauthPayload(_Strict):
    """A connector whose credential was rejected.

    Carries no secret and no credential reference: the reference names a path
    in the secret manager, and a notification consumer has no need for it.
    `error_code` is the classified connector error, so a consumer can tell
    "the token expired" (reauthorize) from "the provider rejected us"
    (investigate) without re-reading anything.
    """

    connector_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    error_code: str = Field(min_length=1)


class EventEnvelope(BaseModel):
    """The row shape the outbox guarantees to any consumer.

    Mirrors `platform_core.outbox.OutboxEvent`: `event_id` is the idempotency
    key for consumers (distinct from the row's own surrogate id), and
    `tenant_id` is always present so a cross-tenant relay bug cannot silently
    fan one tenant's events out to another.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    # Carried as a plain string, not the enum, on purpose: validating the
    # envelope must only answer "is this a well-formed event", so that an
    # unregistered type reports as `EVENT_TYPE_UNREGISTERED` rather than
    # being swallowed into a generic `ENVELOPE_INVALID`. Mixing the two
    # makes "you invented a new event" indistinguishable from "your JSON is
    # broken", and only one of those is a consumer-side emergency.
    event_type: str = Field(min_length=1)
    event_version: int = Field(default=EVENT_VERSION, ge=1)
    aggregate_type: AggregateType
    aggregate_id: str = Field(min_length=1)
    payload: dict[str, Any]
    trace_id: str = Field(default="")


PAYLOAD_SCHEMAS: dict[EventType, type[BaseModel]] = {
    EventType.CASE_CREATED: CaseCreatedPayload,
    EventType.CASE_UPDATED: CaseUpdatedPayload,
    EventType.USAGE_RECORDED: UsageRecordedPayload,
    EventType.CONNECTOR_NEEDS_REAUTH: ConnectorNeedsReauthPayload,
}


class ContractError(ValueError):
    """Raised when an event does not satisfy its declared contract."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def validate_event(envelope_dict: dict[str, Any]) -> EventEnvelope:
    """Validate an event against its registered payload schema.

    Order matters: the envelope is validated first so a malformed envelope
    reports as a malformed envelope, then the payload is checked against the
    schema registered for that type. An unregistered type is a hard error
    rather than a pass-through, because an event nobody declared is an event
    nobody is consuming.
    """
    try:
        envelope = EventEnvelope.model_validate(envelope_dict)
    except Exception as exc:  # pydantic ValidationError
        raise ContractError("ENVELOPE_INVALID", str(exc)) from exc

    try:
        event_type = EventType(envelope.event_type)
    except ValueError as exc:
        raise ContractError(
            "EVENT_TYPE_UNREGISTERED",
            f"{envelope.event_type} is not a declared event type",
        ) from exc

    schema = PAYLOAD_SCHEMAS.get(event_type)
    if schema is None:
        raise ContractError(
            "EVENT_TYPE_UNREGISTERED",
            f"{envelope.event_type} has no payload schema",
        )
    try:
        schema.model_validate(envelope.payload)
    except Exception as exc:
        raise ContractError("PAYLOAD_INVALID", str(exc)) from exc
    envelope.event_type = event_type
    return envelope


def is_known_event_type(value: str) -> bool:
    try:
        EventType(value)
    except ValueError:
        return False
    return True


def is_known_inbound_event_type(value: str) -> bool:
    try:
        InboundEventType(value)
    except ValueError:
        return False
    return True


__all__ = [
    "EVENT_VERSION",
    "AggregateType",
    "CaseCreatedPayload",
    "CaseUpdatedPayload",
    "ConnectorNeedsReauthPayload",
    "ContractError",
    "UsageRecordedPayload",
    "EventEnvelope",
    "EventType",
    "InboundEventType",
    "PAYLOAD_SCHEMAS",
    "is_known_event_type",
    "is_known_inbound_event_type",
    "validate_event",
]
