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
