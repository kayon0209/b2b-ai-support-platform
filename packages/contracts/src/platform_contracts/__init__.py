"""Shared event schemas and contract validation for the platform."""

from .events import (
    EVENT_VERSION,
    PAYLOAD_SCHEMAS,
    AggregateType,
    CaseCreatedPayload,
    CaseUpdatedPayload,
    ContractError,
    EventEnvelope,
    EventType,
    InboundEventType,
    is_known_event_type,
    is_known_inbound_event_type,
    validate_event,
)

__all__ = [
    "EVENT_VERSION",
    "AggregateType",
    "CaseCreatedPayload",
    "CaseUpdatedPayload",
    "ContractError",
    "EventEnvelope",
    "EventType",
    "InboundEventType",
    "PAYLOAD_SCHEMAS",
    "is_known_event_type",
    "is_known_inbound_event_type",
    "validate_event",
]
