"""Unit tests: redacted JSON logging + trace context (ticket 19)."""

import json
import logging

from observability import (
    ALLOWED_LOG_FIELDS,
    JsonLogger,
    TraceContext,
    new_trace_context,
    redact_value,
)


class _Capture:
    def __init__(self) -> None:
        self.records: list[str] = []

    def __call__(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


def _capturing_logger() -> tuple[JsonLogger, _Capture]:
    capture = _Capture()
    logger = JsonLogger("test-capture")
    handler = logger._logger.handlers[0]
    logger._logger.handlers = [type(handler)(stream=None)] if False else logger._logger.handlers

    # Replace emit target: attach a custom handler
    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            capture(record)

    logger._logger.handlers = [_ListHandler()]
    return logger, capture


def test_allowlist_drops_unknown_fields() -> None:
    ctx = TraceContext(trace_id="tr-1")
    fields = ctx.log_fields(
        tenant_id="t-1",
        delivery_id="d-1",
        raw_message="SECRET CUSTOMER TEXT",  # not allowlisted
        auth_header="Bearer abc",  # not allowlisted
    )
    assert "raw_message" not in fields
    assert "auth_header" not in fields
    assert fields["tenant_id"] == "t-1"


def test_redaction_of_emails_and_tokens() -> None:
    cleaned = redact_value("contact alice@example.com now")
    assert "alice@example.com" not in cleaned
    cleaned2 = redact_value("api_token=supersecret123")
    assert "supersecret123" not in cleaned2


def test_redaction_applies_to_nested_dicts() -> None:
    cleaned = redact_value({"outer": {"email": "bob@corp.test"}, "list": ["x@y.z"]})
    assert "bob@corp.test" not in json.dumps(cleaned)
    assert "x@y.z" not in json.dumps(cleaned)


def test_json_log_output_is_single_line_json() -> None:
    logger, capture = _capturing_logger()
    ctx = new_trace_context()
    logger.info("webhook_received", ctx, tenant_id="t-1", delivery_id="d-9")
    assert len(capture.records) == 1
    parsed = json.loads(capture.records[0])
    assert parsed["event"] == "webhook_received"
    assert parsed["trace_id"] == ctx.trace_id
    assert parsed["tenant_id"] == "t-1"
    assert "\n" not in capture.records[0]


def test_error_code_field_is_allowlisted() -> None:
    logger, capture = _capturing_logger()
    logger.error("ingest_failed", None, error_code="PARSE_FAILED", attempt=2)
    parsed = json.loads(capture.records[0])
    assert parsed["error_code"] == "PARSE_FAILED"
    assert parsed["attempt"] == 2


def test_allowlist_covers_core_pipeline_fields() -> None:
    for field in (
        "trace_id",
        "tenant_id",
        "run_id",
        "route",
        "status",
        "latency_ms",
        "tool_name",
        "reason_code",
    ):
        assert field in ALLOWED_LOG_FIELDS


# --- redaction is key-aware, not only text-aware ---------------------------
#
# The patterns above redact a secret that appears *inside a string*. A secret
# that arrives as a mapping value never does - the name is the key, and the
# value is a bare token - so before this, `redact_value` was a text redactor
# that happened to walk containers and a credential passed as a field sailed
# through. Found while wiring readable metadata into the audit trail, whose
# caller passes exactly that shape.


def test_a_credential_field_is_redacted_by_its_key() -> None:
    assert redact_value({"api_token": "lin_api_key_123", "window": 60}) == {
        "api_token": "***",
        "window": 60,
    }


def test_a_credential_field_is_redacted_at_any_depth() -> None:
    assert redact_value({"outer": {"client_secret": "xyz", "team": "eng"}}) == {
        "outer": {"client_secret": "***", "team": "eng"}
    }


def test_shouty_environment_style_keys_are_redacted() -> None:
    """Names come from both styles - JSON-ish (`api_token`) and environment-ish
    (`APP_CHATWOOT_WEBHOOK_SECRET`)."""
    assert redact_value({"APP_CHATWOOT_WEBHOOK_SECRET": "abc"}) == {
        "APP_CHATWOOT_WEBHOOK_SECRET": "***"
    }


def test_a_credential_reference_is_not_redacted() -> None:
    """`credential_ref` is a secret-manager *reference*, not a secret. Redacting
    it would hide which reference was pointed somewhere, which is the question
    an audit of a credential rotation is asking."""
    value = {"credential_ref": "vault://kv/crm/acme", "status": "active"}
    assert redact_value(value) == value


def test_an_empty_credential_field_is_still_marked() -> None:
    """Marked regardless of emptiness, so the rule is "this field is a
    credential" rather than "this field is a credential that happened to be
    populated" - the conditional version is one refactor away from a leak."""
    assert redact_value({"api_key": ""}) == {"api_key": "***"}


def test_text_patterns_still_apply_to_values() -> None:
    redacted = redact_value({"note": "contact ada@example.com today"})
    assert redacted["note"] == "contact ***@*** today"
