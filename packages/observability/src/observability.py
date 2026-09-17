"""Observability helpers: trace context + redacted JSON logging (ticket 19).

Logging policy (docs/security.md): field allowlist before emission. Raw
prompts, outputs, message bodies, tokens and customer PII never reach a
log line. Trace IDs propagate across webhook -> job -> retrieval -> model
-> tool -> outbound command.

OTel SDK is optional: when the collector is absent we still emit structured
logs and keep a trace context object so call sites do not fork.

`TraceContext` additionally opens real spans through `observability_tracing`.
The span surface is additive: `trace_id` and `log_fields()` behave exactly
as before, so a caller that only logs is unaffected, while a caller that
wants a span calls `ctx.span("retrieval")` and gets one whether or not a
collector is reachable.
"""

import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

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
    """W3C-style trace context with real OTel spans.

    Constructed either from a fresh trace id (`new_trace_context()`) or from
    an inbound one (`trace_context_from_id`) so a trace started at the
    webhook keeps the same identity through the queue and the worker.

    The span surface is intentionally tiny - `span(name)` returns a context
    manager - because the pipeline needs spans at roughly ten points and a
    richer API would not earn its keep.
    """

    trace_id: str
    # Service name attached to spans. Set by the process that owns the
    # context (api/worker) so a trace shows which side of the queue ran what.
    service_name: str = "platform"
    _spans: list[Any] = field(default_factory=list)

    def log_fields(self, **extra: Any) -> dict[str, Any]:
        fields: dict[str, Any] = {"trace_id": self.trace_id}
        for key, value in extra.items():
            if key in ALLOWED_LOG_FIELDS:
                fields[key] = value
            # Non-allowlisted keys are dropped silently: allowlist policy.
        redacted: dict[str, Any] = redact_value(fields)
        return redacted

    def span(self, name: str, **attributes: Any) -> Any:
        """Open a span for this trace.

        Imported lazily so `observability` keeps working if the tracing
        module is unavailable - the module-level promise that log-only call
        sites never fork applies here too.
        """
        from observability_tracing import get_tracer

        span = get_tracer().start_span(name, attributes=attributes or None)
        # Children inherit the context's trace id so a trace assembled from
        # spans produced in different places still joins up.
        span.trace_id = _normalise_trace_id(self.trace_id)
        parent = self._spans[-1] if self._spans else None
        if parent is not None:
            span.set_parent(parent.span_id)
        self._spans.append(span)
        return span

    def spans(self) -> list[Any]:
        """Spans opened through this context, in opening order."""
        return list(self._spans)


def _normalise_trace_id(raw: str) -> str:
    """Map a stored trace id onto the 32-hex form OTel requires."""
    from observability_tracing import to_w3c_trace_id

    return to_w3c_trace_id(raw)


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


def new_trace_context(service_name: str = "platform") -> TraceContext:
    """Start a new trace.

    The trace id stays a UUID string: it is persisted on AgentRun and
    returned in API error envelopes, and both predate OTel. Only the span
    layer normalises to 32 hex characters.
    """
    import uuid

    return TraceContext(trace_id=str(uuid.uuid4()), service_name=service_name)


def trace_context_from_id(trace_id: str, service_name: str = "platform") -> TraceContext:
    """Continue an existing trace across a process boundary.

    Used by the worker, which receives the trace id an API request already
    persisted: without this the webhook's trace and the agent run's trace
    are two unrelated traces and the documented end-to-end trace does not
    exist.
    """
    return TraceContext(trace_id=str(trace_id), service_name=service_name)
