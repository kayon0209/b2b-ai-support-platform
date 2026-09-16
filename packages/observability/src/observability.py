"""Observability helpers: trace context + redacted JSON logging (ticket 19).

Logging policy (docs/security.md): field allowlist before emission. Raw
prompts, outputs, message bodies, tokens and customer PII never reach a
log line. Trace IDs propagate across webhook -> job -> retrieval -> model
-> tool -> outbound command.

OTel SDK is optional: when the collector is absent we still emit structured
logs and keep a trace context object so call sites do not fork.
"""

import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# --- Field allowlist ---

ALLOWED_LOG_FIELDS = {
    "event",
    "tenant_id",
    "trace_id",
    "delivery_id",
    "event_type",
    "run_id",
    "route",
    "status",
    "model_name",
    "token_count",
    "latency_ms",
    "tool_name",
    "decision",
    "reason_code",
    "document_version_id",
    "chunk_count",
    "error_code",
    "queue",
    "attempt",
}

# Patterns redacted defensively even inside allowlisted fields.
_REDACTIONS = [
    (
        re.compile(r"(?i)(authorization|api[_-]?token|access[_-]?token|secret)[\"':=\s]+\S+"),
        r"\1***",
    ),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "***@***"),  # emails
    (re.compile(r"\b\d{15,19}\b"), "***card***"),  # long digit runs
]


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        for pattern, replacement in _REDACTIONS:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


@dataclass
class TraceContext:
    """Minimal W3C-style trace context; swapped for real OTel span context
    when the collector ships. Carries through job payloads as trace_id."""

    trace_id: str

    def log_fields(self, **extra: Any) -> dict[str, Any]:
        fields: dict[str, Any] = {"trace_id": self.trace_id}
        for key, value in extra.items():
            if key in ALLOWED_LOG_FIELDS:
                fields[key] = value
            # Non-allowlisted keys are dropped silently: allowlist policy.
        redacted: dict[str, Any] = redact_value(fields)
        return redacted


class JsonLogger:
    """Emits single-line JSON logs to stdout with redaction applied."""

    def __init__(self, name: str = "platform") -> None:
        self._logger = logging.getLogger(name)
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)
            self._logger.propagate = False

    def log(self, level: int, event: str, ctx: TraceContext | None = None, **fields: Any) -> None:
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": logging.getLevelName(level),
            "event": event,
        }
        if ctx is not None:
            record.update(ctx.log_fields(**fields))
        else:
            allowed = {k: v for k, v in fields.items() if k in ALLOWED_LOG_FIELDS}
            record.update(redact_value(allowed))
        self._logger.log(level, json.dumps(record, ensure_ascii=False, default=str))

    def info(self, event: str, ctx: TraceContext | None = None, **fields: Any) -> None:
        self.log(logging.INFO, event, ctx, **fields)

    def warning(self, event: str, ctx: TraceContext | None = None, **fields: Any) -> None:
        self.log(logging.WARNING, event, ctx, **fields)

    def error(self, event: str, ctx: TraceContext | None = None, **fields: Any) -> None:
        self.log(logging.ERROR, event, ctx, **fields)


def new_trace_context() -> TraceContext:
    import uuid

    return TraceContext(trace_id=str(uuid.uuid4()))
