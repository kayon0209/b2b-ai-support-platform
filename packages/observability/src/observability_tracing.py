"""OpenTelemetry tracing that degrades to a local span recorder.

Why this exists
---------------
`observability.TraceContext` used to be a `uuid4()` in a dataclass: trace
ids propagated, but nothing was ever emitted anywhere. Ticket 19 asks for
real OpenTelemetry traces, and docs/deployment-and-operations.md asks for
one trace id to cross webhook -> job -> retrieval -> model -> tool ->
outbound command. A propagated id with no spans is not a trace.

Design rules
------------
1. **The collector is genuinely optional.** `opentelemetry-sdk` may be
   absent (it is not in the base runtime dependency set), and even when it
   is installed, `OTEL_EXPORTER_OTLP_ENDPOINT` may be unset. In both cases
   we record spans into an in-process ring buffer and expose the trace id
   exactly as before. Call sites never branch on availability - that
   property is what the old module docstring promised, and it is preserved.

2. **Spans never carry raw content.** `set_attribute` is only reached
   through `safe_attributes`, which drops non-allowlisted keys and applies
   the same redaction as the log path. A span attribute is a log line with
   a different destination; it must obey the same policy.

3. **Exporter setup never raises.** A misconfigured OTLP endpoint must not
   take down request serving. Failures are logged once and the process
   continues on the local recorder - an observability outage is not an
   availability incident.

4. **No global TracerProvider mutation by default.** Installing a global
   provider changes the behaviour of every library in the process,
   including Chatwoot's client if it ever shares a process, and cannot be
   undone. We hold our own provider and pass it explicitly.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

from observability import ALLOWED_LOG_FIELDS, redact_value

logger = logging.getLogger(__name__)

# Span attribute keys we permit. Extends the log allowlist with the
# span-specific fields the pipeline genuinely needs; anything else is
# dropped rather than redacted, because a span attribute name is chosen by
# the caller and an unvetted name is a name we have not agreed to store.
ALLOWED_SPAN_ATTRIBUTES = ALLOWED_LOG_FIELDS | {
    "span.kind",
    "queue.age_ms",
    "retrieval.candidates",
    "retrieval.rerank_degraded",
    "flag.rerank",
    "citation.count",
    "lease.version",
    "worker.queue",
    "http.route",
    "http.status_code",
    "db.system",
}

# How many finished spans the in-process recorder keeps. Bounded because an
# unbounded trace buffer is a memory leak that only shows up in production,
# when traffic is high and the collector is down - the worst possible time.
_LOCAL_SPAN_CAPACITY = 500


def safe_attributes(**attributes: Any) -> dict[str, Any]:
    """Filter and redact attributes before they reach a span.

    Same contract as `TraceContext.log_fields`: allowlist, then redact.
    """
    kept: dict[str, Any] = {}
    for key, value in attributes.items():
        if key in ALLOWED_SPAN_ATTRIBUTES and value is not None:
            kept[key] = value
    result: dict[str, Any] = redact_value(kept)
    return result


@dataclass(frozen=True)
class RecordedSpan:
    """One finished span, as seen by the local recorder."""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    start_time_unix_nano: int
    end_time_unix_nano: int
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "unset"
    error: str = ""

    @property
    def duration_ms(self) -> float:
        return (self.end_time_unix_nano - self.start_time_unix_nano) / 1_000_000.0


class SpanRecorder:
    """In-process ring buffer of finished spans.

    This is not a replacement for a collector; it is what makes traces
    observable when no collector is configured, and what lets the test
    suite assert that spans were actually produced instead of asserting
    that a brace of `with` statements ran.
    """

    def __init__(self, capacity: int = _LOCAL_SPAN_CAPACITY) -> None:
        self._spans: deque[RecordedSpan] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self.dropped = 0

    def add(self, span: RecordedSpan) -> None:
        with self._lock:
            if len(self._spans) == self._spans.maxlen:
                self.dropped += 1
            self._spans.append(span)

    def spans(self, *, trace_id: str | None = None) -> list[RecordedSpan]:
        with self._lock:
            snapshot = list(self._spans)
        if trace_id is None:
            return snapshot
        return [s for s in snapshot if s.trace_id == trace_id]

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()
            self.dropped = 0


_recorder = SpanRecorder()


def get_span_recorder() -> SpanRecorder:
    return _recorder


class SpanLike(Protocol):
    """Minimal span surface used by the trace context."""

    def set_attribute(self, key: str, value: Any) -> None: ...

    def set_status(self, status: str, description: str = "") -> None: ...

    def record_exception(self, exc: BaseException) -> None: ...

    def end(self) -> None: ...


@dataclass(frozen=True)
class SpanOutcome:
    """Coarse result of a span, mapped onto OTel's status vocabulary."""

    OK = "ok"
    ERROR = "error"
    UNSET = "unset"


def to_w3c_trace_id(raw: uuid.UUID | str) -> str:
    """Normalise a uuid-shaped value into a 32-char lowercase hex trace id.

    The platform generates trace ids as UUIDv4 strings today. OTel requires
    32 hex characters with no dashes, and `TraceContext.trace_id` is
    persisted on AgentRun - so the conversion has to be lossless in both
    directions. A UUID string always is: strip dashes, keep hex.
    """
    text = str(raw).replace("-", "").lower()
    if len(text) != 32:
        # Not a UUID: hash it so the trace id stays 32 hex chars and remains
        # derived deterministically from whatever was supplied.
        import hashlib

        text = hashlib.sha256(str(raw).encode()).hexdigest()[:32]
    return text


class Tracer:
    """Span factory bound to a provider (OTel SDK, or the local recorder).

    A single object is handed to `TraceContext` so the context can open
    spans without importing OTel at module scope.
    """

    def __init__(self, *, service_name: str = "platform", otel_tracer: Any = None) -> None:
        self._service_name = service_name
        self._otel = otel_tracer

    @property
    def service_name(self) -> str:
        return self._service_name

    @property
    def exports_to_collector(self) -> bool:
        return self._otel is not None

    def start_span(self, name: str, *, attributes: dict[str, Any] | None = None) -> _Span:
        otel_span = None
        if self._otel is not None:
            otel_span = self._otel.start_span(name)
        span = _Span(name=name, otel_span=otel_span, recorder=_recorder)
        if attributes:
            for key, value in attributes.items():
                span.set_attribute(key, value)
        return span


class _Span:
    """A span that writes to OTel when available and always to the recorder.

    Dual-writing is deliberate: the recorder is the only way to inspect
    traces in-process (tests, `scripts/`), and it also means a collector
    outage loses export, not the record of what happened locally.
    """

    def __init__(self, *, name: str, otel_span: Any, recorder: SpanRecorder) -> None:
        self.name = name
        self._otel = otel_span
        self._recorder = recorder
        self._attributes: dict[str, Any] = {}
        self._status = SpanOutcome.UNSET
        self._error = ""
        self._ended = False
        self._start_ns = time.time_ns()
        self.trace_id = to_w3c_trace_id(uuid.uuid4())
        self.span_id = uuid.uuid4().hex[:16]
        self._parent_span_id: str | None = None
        if otel_span is not None:
            ctx = getattr(otel_span, "get_span_context", None)
            if callable(ctx):
                span_context = ctx()
                self.trace_id = format(span_context.trace_id, "032x")
                self.span_id = format(span_context.span_id, "016x")
                if getattr(span_context, "is_remote", False):
                    self._parent_span_id = None

    @property
    def parent_span_id(self) -> str | None:
        return self._parent_span_id

    def set_parent(self, parent_span_id: str | None) -> None:
        self._parent_span_id = parent_span_id

    def set_attribute(self, key: str, value: Any) -> None:
        kept = safe_attributes(**{key: value})
        if key not in kept:
            return
        cleaned = kept[key]
        self._attributes[key] = cleaned
        if self._otel is not None:
            self._otel.set_attribute(key, cleaned)

    def set_attributes(self, **attributes: Any) -> None:
        for key, value in attributes.items():
            self.set_attribute(key, value)

    def set_status(self, status: str, description: str = "") -> None:
        self._status = status
        if status == SpanOutcome.ERROR:
            self._error = description
        if self._otel is not None:
            self._otel.set_status(status, description)

    def record_exception(self, exc: BaseException) -> None:
        # Only the exception TYPE reaches the span. A provider exception
        # message can echo the request body, and the request body is
        # customer content.
        self._error = type(exc).__name__
        self._status = SpanOutcome.ERROR
        if self._otel is not None:
            self._otel.record_exception(exc)

    def end(self) -> None:
        if self._ended:
            return
        self._ended = True
        if self._otel is not None:
            self._otel.end()
        self._recorder.add(
            RecordedSpan(
                name=self.name,
                trace_id=self.trace_id,
                span_id=self.span_id,
                parent_span_id=self._parent_span_id,
                start_time_unix_nano=self._start_ns,
                end_time_unix_nano=time.time_ns(),
                attributes=dict(self._attributes),
                status=self._status,
                error=self._error,
            )
        )

    def __enter__(self) -> _Span:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc is not None:
            self.record_exception(exc)
        self.end()


# --- Provider construction -------------------------------------------------

_provider_lock = threading.Lock()
_tracer_singleton: Tracer | None = None


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def build_tracer(*, service_name: str | None = None) -> Tracer:
    """Build a tracer, wiring the OTLP exporter only when it is configured.

    Returns a tracer that always records locally. The OTel SDK is imported
    lazily and every failure path falls back to local-only, so this function
    cannot raise on a host without the SDK or without a reachable collector.
    """
    svc = service_name or os.environ.get("OTEL_SERVICE_NAME", "platform")
    endpoint = (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()

    if not endpoint:
        return Tracer(service_name=svc, otel_tracer=None)

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set but the OpenTelemetry SDK is not "
            "installed; spans will be recorded in-process only"
        )
        return Tracer(service_name=svc, otel_tracer=None)

    try:
        provider = TracerProvider(resource=Resource.create({"service.name": svc}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        atexit.register(_shutdown_provider, provider)
        return Tracer(service_name=svc, otel_tracer=provider.get_tracer("platform_core"))
    except Exception as exc:  # noqa: BLE001 - observability must never break serving
        logger.warning(
            "failed to configure the OTLP exporter (%s); spans will be recorded in-process only",
            type(exc).__name__,
        )
        return Tracer(service_name=svc, otel_tracer=None)


def _shutdown_provider(provider: Any) -> None:
    """Flush buffered spans at interpreter exit.

    Without this, `BatchSpanProcessor` drops whatever it is still holding
    when a short-lived process (a worker tick, a CLI) exits - which is
    exactly the process that most needs its traces exported.
    """
    try:
        provider.shutdown()
    except Exception:  # noqa: BLE001 - shutdown must not raise
        logger.debug("span processor shutdown failed", exc_info=True)


def get_tracer() -> Tracer:
    """Process-wide tracer, built once from the environment."""
    global _tracer_singleton
    if _tracer_singleton is None:
        with _provider_lock:
            if _tracer_singleton is None:
                _tracer_singleton = build_tracer()
    return _tracer_singleton


def reset_tracer(tracer: Tracer | None = None) -> None:
    """Replace the process tracer. Test-only."""
    global _tracer_singleton
    _tracer_singleton = tracer


def is_tracing_enabled() -> bool:
    """True when spans are exported to a collector rather than kept locally."""
    return get_tracer().exports_to_collector or _env_flag("OTEL_TRACES_ENABLED")
