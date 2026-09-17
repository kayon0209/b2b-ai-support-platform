"""Unit tests for the OTel tracing layer (observability_tracing.py).

Covers the properties that are easy to break:

- Spans always record locally, even when no collector is configured.
- Parent/child relationships are preserved via TraceContext.
- safe_attributes filter out non-allowlisted keys.
- Trace id normalization is lossless for UUIDs.
- The tracer singleton is resettable for tests.
- to_w3c_trace_id handles UUIDs and arbitrary strings.
"""

from __future__ import annotations

import uuid

import pytest

from observability import new_trace_context, trace_context_from_id
from observability_tracing import (
    SpanRecorder,
    Tracer,
    _Span,
    get_span_recorder,
    get_tracer,
    reset_tracer,
    safe_attributes,
    to_w3c_trace_id,
)

# --- Trace id normalization ------------------------------------------------


def test_to_w3c_trace_id_normalizes_uuid_string() -> None:
    raw = "01234567-89ab-cdef-0123-456789abcdef"
    result = to_w3c_trace_id(raw)
    assert result == "0123456789abcdef0123456789abcdef"
    assert len(result) == 32
    assert "-" not in result


def test_to_w3c_trace_id_normalizes_uuid_object() -> None:
    raw = uuid.UUID("01234567-89ab-cdef-0123-456789abcdef")
    result = to_w3c_trace_id(raw)
    assert result == "0123456789abcdef0123456789abcdef"


def test_to_w3c_trace_id_non_uuid_is_hashed_to_32_hex() -> None:
    raw = "trace-from-some-system-123"
    result = to_w3c_trace_id(raw)
    assert len(result) == 32
    assert all(c in "0123456789abcdef" for c in result)
    assert to_w3c_trace_id(raw) == result  # deterministic


def test_to_w3c_trace_id_already_w3c_passes_through() -> None:
    raw = "abcdef0123456789abcdef0123456789"
    result = to_w3c_trace_id(raw)
    assert result == raw


# --- Safe attributes -------------------------------------------------------


def test_safe_attributes_drops_non_allowlisted_keys() -> None:
    result = safe_attributes(
        secret_token="sk-abc123",
        span_kind="server",
    )
    assert "secret_token" not in result
    assert "span_kind" not in result


def test_safe_attributes_keeps_allowlisted_keys() -> None:
    result = safe_attributes(**{"span.kind": "server", "http.status_code": 200})
    assert result.get("span.kind") == "server"
    assert result.get("http.status_code") == 200


def test_safe_attributes_drops_none_values() -> None:
    result = safe_attributes(**{"span.kind": None})
    assert result == {}


# --- SpanRecorder ----------------------------------------------------------


def test_span_recorder_truncates_at_capacity() -> None:
    recorder = SpanRecorder(capacity=3)
    for i in range(5):
        span = _make_test_span(name=f"span-{i}", recorder=recorder)
        recorder.add(span)
    spans = recorder.spans()
    assert len(spans) == 3
    assert recorder.dropped == 2
    assert spans[-1].name == "span-4"


def test_span_recorder_filters_by_trace_id() -> None:
    recorder = SpanRecorder()
    recorder.add(_make_test_span(trace_id="trace-a", name="a1", recorder=recorder))
    recorder.add(_make_test_span(trace_id="trace-b", name="b1", recorder=recorder))
    recorder.add(_make_test_span(trace_id="trace-a", name="a2", recorder=recorder))
    a_spans = recorder.spans(trace_id="trace-a")
    assert len(a_spans) == 2
    assert {s.name for s in a_spans} == {"a1", "a2"}


def test_span_recorder_clear_resets_dropped() -> None:
    recorder = SpanRecorder(capacity=2)
    recorder.add(_make_test_span("s1", recorder=recorder))
    recorder.add(_make_test_span("s2", recorder=recorder))
    recorder.add(_make_test_span("s3", recorder=recorder))
    assert recorder.dropped == 1
    recorder.clear()
    assert recorder.dropped == 0
    assert recorder.spans() == []


# --- TraceContext span propagation -----------------------------------------


def test_trace_context_propagates_trace_id_to_spans() -> None:
    """Spans opened via TraceContext must share the context's trace id."""
    global_recorder = get_span_recorder()
    global_recorder.clear()
    ctx = new_trace_context(service_name="test")
    with ctx.span("parent"):
        with ctx.span("child"):
            pass
    spans = global_recorder.spans(trace_id=ctx.trace_id.replace("-", ""))
    assert len(spans) == 2
    expected_id = ctx.trace_id.replace("-", "")
    assert all(s.trace_id == expected_id for s in spans)
    global_recorder.clear()


def test_trace_context_links_parent_and_child_spans() -> None:
    """Child spans opened while a parent is active inherit the parent id."""
    global_recorder = get_span_recorder()
    global_recorder.clear()
    ctx = new_trace_context(service_name="test")
    parent = ctx.span("parent")
    parent.set_attributes(**{"span.kind": "server"})
    parent_id = parent.span_id
    child = ctx.span("child")
    child.end()
    parent.end()
    spans = global_recorder.spans(trace_id=child.trace_id)
    assert len(spans) == 2
    child_rec = [s for s in spans if s.name == "child"][0]
    assert child_rec.parent_span_id == parent_id
    global_recorder.clear()


# --- Tracer (no collector) ------------------------------------------------


def test_tracer_without_collector_still_records_locally() -> None:
    global_recorder = get_span_recorder()
    global_recorder.clear()
    tracer = Tracer(service_name="test")
    assert tracer.exports_to_collector is False
    span = tracer.start_span("test_span")
    span.set_attributes(**{"span.kind": "server"})
    span.set_attribute("http.status_code", 200)
    span.end()
    recorded = global_recorder.spans(trace_id=span.trace_id)
    assert len(recorded) == 1
    assert recorded[0].name == "test_span"
    assert recorded[0].attributes.get("span.kind") == "server"
    assert recorded[0].attributes.get("http.status_code") == 200
    global_recorder.clear()


def test_trace_context_from_id_continues_existing_trace() -> None:
    """A worker receiving a trace id must propagate it, not start fresh."""
    global_recorder = get_span_recorder()
    global_recorder.clear()
    existing_id = str(uuid.uuid4())
    ctx = trace_context_from_id(existing_id, service_name="worker")
    with ctx.span("worker_span"):
        pass
    spans = global_recorder.spans(trace_id=existing_id.replace("-", ""))
    assert len(spans) == 1
    assert spans[0].name == "worker_span"
    global_recorder.clear()


# --- Span lifecycle --------------------------------------------------------


def test_span_status_ok_and_error() -> None:
    span = _make_test_span()
    span.set_status("ok")
    assert span._status == "ok"
    span.set_status("error", "TIMEOUT")
    assert span._status == "error"
    assert span._error == "TIMEOUT"
    span.end()


def test_span_record_exception_stores_type_only() -> None:
    """Exception messages must not reach the span - they can echo customer
    content from the request body."""
    span = _make_test_span()
    exc = ValueError("customer message here")
    span.record_exception(exc)
    assert span._error == "ValueError"
    assert "customer message" not in span._error
    span.end()


def test_span_end_is_idempotent() -> None:
    global_recorder = get_span_recorder()
    global_recorder.clear()
    tracer = Tracer(service_name="test")
    span = tracer.start_span("idempotent")
    span.end()
    span.end()
    recorded = global_recorder.spans(trace_id=span.trace_id)
    assert len(recorded) == 1
    global_recorder.clear()


def test_span_drops_non_allowlisted_attribute_via_set_attributes() -> None:
    """set_attributes must filter through safe_attributes, so a non-allowlisted
    key cannot be smuggled onto a span."""
    span = _make_test_span()
    span.set_attributes(**{"span.kind": "client", "secret": "sk-123"})
    assert "secret" not in span._attributes
    assert span._attributes.get("span.kind") == "client"
    span.end()


def test_span_context_manager_records_exception_on_exit() -> None:
    """A span used as a context manager must capture the exception type."""
    global_recorder = get_span_recorder()
    global_recorder.clear()
    tracer = Tracer(service_name="test", otel_tracer=None)

    with pytest.raises(RuntimeError, match="boom"):
        with tracer.start_span("guarded") as span:
            raise RuntimeError("boom")

    recorded = global_recorder.spans(trace_id=span.trace_id)
    assert len(recorded) == 1
    assert recorded[0].status == "error"
    assert recorded[0].error == "RuntimeError"
    global_recorder.clear()


# --- get_tracer singleton ---------------------------------------------------


def test_get_tracer_is_singleton() -> None:
    reset_tracer()
    first = get_tracer()
    second = get_tracer()
    assert first is second


def test_reset_tracer_replaces_provider() -> None:
    first = get_tracer()
    reset_tracer()
    second = get_tracer()
    assert first is not second


# --- Helpers ----------------------------------------------------------------


def _make_test_span(
    name: str = "test",
    trace_id: str = "abcd1234abcd1234abcd1234abcd1234",
    recorder: SpanRecorder | None = None,
) -> _Span:
    rec = recorder or SpanRecorder()
    span = _Span(name=name, otel_span=None, recorder=rec)
    if trace_id:
        span.trace_id = trace_id
    return span
