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
    # --- The rest of the vocabulary -----------------------------------------
    #
    # This set *is* the log schema, and `logger.log` drops anything not in it -
    # silently, by design, because it is also the redaction boundary. Nothing
    # enforced the other side of that contract, so call sites invented names
    # and the fields vanished: 37 call sites across the workers, the
    # orchestrator and the identity middleware were logging nothing they
    # thought they were logging. `apps/api/tests/unit/test_log_fields.py` now
    # enforces it, so additions are deliberate and a typo cannot be quiet.
    #
    # Only bounded identifiers and counts belong here. Free text does not:
    # `error` and `detail` were carrying exception messages and are named
    # `error_code` (the class) instead, because that is the part an operator
    # greps for and the part that cannot contain customer content.
    "conversation_id",
    "conversation_ref_id",
    "turn_id",
    "event_id",
    "proposal_id",
    "aggregate_id",
    "aggregate_type",
    "tenant_ref",
    "message_type",
    # ADR 0014: which channel an answer is delivered on ("email" | "wechat").
    # A bounded identifier from a closed set, the same class as `message_type`
    # and `route` - not free text, so it belongs here. Without it the outbound
    # warning says a delivery was withheld but not *which channel*, and that is
    # the one thing an operator needs to fix it. Added deliberately, which is
    # what `test_log_fields.py` exists to force.
    "channel",
    "notice_sent",
    "has_embedding",
    "has_chatwoot_account",
    "local_turns",
    "output_hash",
    "count",
    "tenants",
    "attempts",
    "consecutive_failures",
    "expired_versions",
    "pruned_dead_letters",
    "pruned_inbox_events",
    "abandoned_runs",
    # Object-storage retention (stage 4). Counts only - what the erasure pass
    # removed, what it could not, and what reconciliation found pointing at
    # bytes that are no longer there. No keys and no tenant object names: a
    # document key contains a filename, which is customer content.
    "objects_erased",
    "objects_failed",
    "orphan_objects_removed",
    "orphan_objects_failed",
    "rows_missing_object",
    "reason",
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

REDACTED = "***"

# A key whose *name* says it holds a credential.
#
# The patterns above redact a secret that appears **inside a string**
# (`"api_token=xyz"`). A secret that arrives as a mapping value never does: the
# name is the key, and the value is a bare token that matches nothing.
#
#     {"api_token": "lin_api_key_123"}   ->  before: unchanged, after: redacted
#
# So `redact_value` was a text redactor that happened to walk containers, and
# a credential passed as a field sailed straight through. Found while wiring
# readable metadata into the audit trail, where the caller passes exactly that
# shape.
#
# `credential(?!_ref)` is deliberate: `credential_ref` is a secret-manager
# *reference* (`vault://kv/crm/acme`), not a secret. Redacting it would hide
# which reference was pointed somewhere, which is the question an audit of a
# credential rotation is actually asking.
_SENSITIVE_KEY = re.compile(
    r"(?i)("
    r"authorization"
    r"|api[_-]?key|api[_-]?token"
    r"|access[_-]?token|refresh[_-]?token"
    r"|secret"
    r"|password|passwd"
    r"|credential(?!_ref)"
    r"|private[_-]?key"
    r")"
)


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        for pattern, replacement in _REDACTIONS:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, dict):
        return {
            k: REDACTED if isinstance(k, str) and _SENSITIVE_KEY.search(k) else redact_value(v)
            for k, v in value.items()
        }
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
        # `exc_info` is a logging facility, not a field: passing it through the
        # field path meant it was filtered out by the allowlist and no
        # traceback was ever emitted. Two call sites asked for one - the
        # membership-resolution warning and the span-processor shutdown - and
        # silently got nothing. Forwarded to the stdlib logger, which appends
        # the traceback after the JSON line rather than embedding it in the
        # record, so the structured line stays parseable.
        exc_info = fields.pop("exc_info", None)
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
        self._logger.log(
            level,
            json.dumps(record, ensure_ascii=False, default=str),
            exc_info=exc_info,
        )

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
