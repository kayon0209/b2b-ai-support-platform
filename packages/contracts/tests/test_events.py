"""Contract tests for the shared event schemas.

The point of these tests is anti-drift. `tests/` is in `testpaths` and
`packages/contracts` used to be an empty package listed there anyway, so the
module existed on paper while the contract lived only in the producers'
call sites. These pin the contract down and then assert the producers still
match it.

`test_every_emitted_event_type_is_registered` is the one that earns its
keep: it greps the source for `event_type="..."` and fails when a producer
emits a type the contract does not know about. Without it, adding an event
is invisible until a consumer silently drops rows.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from platform_contracts.events import (
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

TENANT = "01900000-0000-7000-8000-000000000001"

_EMIT_RE = re.compile(r"""event_type\s*=\s*["']([a-z_][a-z0-9_.]*)["']""")


def _envelope(event_type: str, payload: dict, **over) -> dict:
    env = {
        "event_id": "01900000-0000-7000-8000-0000000000ff",
        "tenant_id": TENANT,
        "event_type": event_type,
        "event_version": EVENT_VERSION,
        "aggregate_type": AggregateType.CASE.value,
        "aggregate_id": "01900000-0000-7000-8000-0000000000f1",
        "payload": payload,
        "trace_id": "trace-1",
    }
    env.update(over)
    return env


# --- schema validity ---------------------------------------------------------


def test_case_created_accepts_a_valid_payload() -> None:
    env = validate_event(_envelope("case.created", {"case_id": "c-1", "status": "new"}))
    assert env.event_type == EventType.CASE_CREATED


def test_case_updated_accepts_a_valid_payload() -> None:
    env = validate_event(
        _envelope(
            "case.updated",
            {"case_id": "c-1", "command": "escalate", "status": "open", "version": 2},
        )
    )
    assert env.event_type == EventType.CASE_UPDATED


def test_every_registered_type_has_a_payload_schema() -> None:
    for member in EventType:
        assert member in PAYLOAD_SCHEMAS, f"{member} is emitted but has no schema"


def test_is_known_event_type_matches_the_enum() -> None:
    assert is_known_event_type("case.created")
    assert not is_known_event_type("case.deleted")
    assert not is_known_event_type("")


# --- rejections --------------------------------------------------------------


def test_unregistered_event_type_is_rejected() -> None:
    with pytest.raises(ContractError) as exc:
        validate_event(_envelope("case.deleted", {"case_id": "c-1"}))
    assert exc.value.code == "EVENT_TYPE_UNREGISTERED"


def test_payload_missing_a_required_field_is_rejected() -> None:
    with pytest.raises(ContractError) as exc:
        validate_event(_envelope("case.created", {"case_id": "c-1"}))
    assert exc.value.code == "PAYLOAD_INVALID"


def test_unknown_payload_key_is_rejected_not_ignored() -> None:
    """A producer that drifted ahead of the schema must be told, not
    silently trimmed - otherwise the consumer never learns about new fields.
    """
    with pytest.raises(ContractError) as exc:
        validate_event(_envelope("case.created", {"case_id": "c-1", "status": "new", "typo": 1}))
    assert exc.value.code == "PAYLOAD_INVALID"


def test_envelope_missing_tenant_id_is_rejected() -> None:
    """tenant_id is mandatory: a relay bug that drops it would broadcast one
    tenant's events to everyone."""
    env = _envelope("case.created", {"case_id": "c-1", "status": "new"})
    del env["tenant_id"]
    with pytest.raises(ContractError) as exc:
        validate_event(env)
    assert exc.value.code == "ENVELOPE_INVALID"


def test_negative_event_version_is_rejected() -> None:
    with pytest.raises(ContractError):
        validate_event(
            _envelope("case.created", {"case_id": "c-1", "status": "new"}, event_version=0)
        )


def test_case_updated_rejects_a_non_positive_version() -> None:
    with pytest.raises(ContractError) as exc:
        validate_event(
            _envelope(
                "case.updated",
                {"case_id": "c-1", "command": "escalate", "status": "open", "version": 0},
            )
        )
    assert exc.value.code == "PAYLOAD_INVALID"


def test_empty_case_id_is_rejected() -> None:
    with pytest.raises(ContractError) as exc:
        validate_event(_envelope("case.created", {"case_id": "", "status": "new"}))
    assert exc.value.code == "PAYLOAD_INVALID"


def test_payload_schemas_are_strict() -> None:
    """Both payload models forbid extras; an inherited permissive base would
    quietly disable the drift detection above."""
    for schema in (CaseCreatedPayload, CaseUpdatedPayload):
        assert schema.model_config.get("extra") == "forbid"
    assert EventEnvelope.model_config.get("extra") == "forbid"


# --- anti-drift: producers must emit registered types ------------------------


def _repo_root() -> pathlib.Path:
    # .../packages/contracts/tests/test_events.py -> repo root is 3 up.
    return pathlib.Path(__file__).resolve().parents[3]


def _source_roots() -> list[pathlib.Path]:
    root = _repo_root()
    return [root / "apps" / "api" / "src", root / "apps" / "worker" / "src"]


def _emitted_near(call: str) -> dict[str, str]:
    """Event types passed to a specific producer call.

    Scoped to the call, not to any `event_type=` in the file: the platform
    persists *inbound* events (`persist_inbox_event`) and emits *outbound*
    ones (`enqueue`) using the same keyword, and those are two different
    vocabularies with different guarantees. A bare `event_type=` scan
    conflates them and reports every inbound event as an unregistered
    outbound one.
    """
    found: dict[str, str] = {}
    for root in _source_roots():
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(re.escape(call) + r"\(", text):
                window = text[m.start() : m.start() + 900]
                for em in _EMIT_RE.finditer(window):
                    found.setdefault(em.group(1), str(path.relative_to(_repo_root())))
    return found


def test_every_emitted_event_type_is_registered() -> None:
    """Outbound events must have a payload schema.

    This is the whole reason the package exists. An event emitted without a
    schema is unobservable: the outbox writes it, the relay drains it, and no
    consumer can tell whether a missing field means "absent" or "renamed".
    """
    emitted = _emitted_near("enqueue")
    assert emitted, "no outbox enqueue() emissions found - has this scan stopped working?"
    unregistered = {k: v for k, v in emitted.items() if not is_known_event_type(k)}
    assert not unregistered, (
        f"these event types are emitted but have no payload schema: {unregistered}"
    )


def test_every_inbound_event_type_is_registered() -> None:
    """Inbound events are persisted before any work is enqueued
    (AGENTS.md), so an unregistered inbound name means work is being driven
    by an event nobody declared."""
    persisted = _emitted_near("persist_inbox_event")
    assert persisted, "no persist_inbox_event() sites found - has this scan stopped working?"
    unregistered = {k: v for k, v in persisted.items() if not is_known_inbound_event_type(k)}
    assert not unregistered, (
        f"these inbound event types are persisted but not declared: {unregistered}"
    )


def test_the_two_vocabularies_do_not_overlap() -> None:
    """A name that is both sent and received would let a producer emit an
    inbound event into the outbox and have it look legitimate."""
    overlap = {e.value for e in EventType} & {e.value for e in InboundEventType}
    assert not overlap, f"shared event names: {overlap}"


def test_usage_recorded_accepts_a_valid_payload() -> None:
    envelope = validate_event(
        {
            "event_id": "e-1",
            "tenant_id": TENANT,
            "event_type": "usage.recorded",
            "aggregate_type": "agent_run",
            "aggregate_id": "r-1",
            "payload": {
                "run_id": "r-1",
                "route": "knowledge_qa",
                "status": "completed",
                "prompt_tokens": 120,
                "completion_tokens": 34,
            },
        }
    )
    assert envelope.event_type == EventType.USAGE_RECORDED
    assert envelope.aggregate_type == AggregateType.AGENT_RUN


def test_usage_recorded_rejects_a_negative_token_count() -> None:
    """Metering that cannot be negative must not be recorded as negative."""
    with pytest.raises(ContractError) as exc:
        validate_event(
            {
                "event_id": "e-2",
                "tenant_id": TENANT,
                "event_type": "usage.recorded",
                "aggregate_type": "agent_run",
                "aggregate_id": "r-2",
                "payload": {
                    "run_id": "r-2",
                    "route": "knowledge_qa",
                    "status": "completed",
                    "prompt_tokens": -1,
                },
            }
        )
    assert exc.value.code == "PAYLOAD_INVALID"
