"""Unit tests: evaluation runner, release gates, metrics, PII, queues
(tickets 33-37)."""

import uuid

import pytest

from platform_core.agent_runtime.qa_path import DraftAnswer, RetrievedChunk
from platform_core.evaluation.gates import (
    DEFAULT_THRESHOLDS,
    ReadToolOutcome,
    evaluate_release_gates,
    release_allowed,
)
from platform_core.evaluation.pii import (
    Sensitivity,
    classify_field,
    minimize_payload,
    redact_text,
)
from platform_core.evaluation.queues import (
    DEFAULT_QUEUE_CONFIG,
    PriorityQueueManager,
    Queue,
    QueueFull,
    backoff_for,
)
from platform_core.evaluation.runner import EvalCase, EvalReport, EvaluationRunner


def _chunk(text: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_version_id=uuid.uuid4(),
        title="Policy",
        section_path=[],
        excerpt=text,
        source_uri="minio://x",
        score=0.5,
    )


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# --- Evaluation runner (ticket 33) ---


def test_runner_passes_answerable_case() -> None:
    async def answer(question, evidence):
        return DraftAnswer(
            text="The refund window is 30 days.",
            claims={0: [evidence[0].chunk_id]},
        )

    async def retrieve(question, scope):
        return [_chunk("The refund window is 30 days for annual plans.")]

    runner = EvaluationRunner(answer, retrieve)
    report = _run(
        runner.run(
            [
                EvalCase(question="refund window?", required_claims=("30 days",)),
            ]
        )
    )
    assert report.passed == 1 and report.failed == 0
    assert report.citation_coverage == 1.0


def test_runner_flags_phantom_citation() -> None:
    async def answer(question, evidence):
        return DraftAnswer(text="x", claims={0: [uuid.uuid4()]})

    async def retrieve(question, scope):
        return [_chunk("The refund window is 30 days.")]

    runner = EvaluationRunner(answer, retrieve)
    report = _run(runner.run([EvalCase(question="refund window?")]))
    assert report.failed == 1
    assert report.citation_violations == 1


def test_runner_must_abstain_passes_on_handoff() -> None:
    async def answer(question, evidence):
        raise AssertionError("should not generate for abstained case")

    async def retrieve(question, scope):
        return []  # no evidence -> abstain + handoff

    runner = EvaluationRunner(answer, retrieve)
    report = _run(
        runner.run(
            [
                EvalCase(
                    question="what is their secret pricing?",
                    must_abstain=True,
                    expected_handoff=True,
                ),
            ]
        )
    )
    assert report.passed == 1
    assert report.abstention_correct == 1


def test_runner_false_abstention_fails() -> None:
    async def answer(question, evidence):
        raise AssertionError("should not generate")

    async def retrieve(question, scope):
        return []  # retrieval broken -> abstains though answerable

    runner = EvaluationRunner(answer, retrieve)
    report = _run(runner.run([EvalCase(question="refund window?")]))
    assert report.failed == 1
    assert report.abstention_false == 1


def test_runner_forbidden_claim_detected() -> None:
    async def answer(question, evidence):
        return DraftAnswer(
            text="We guarantee 100% refunds forever.",
            claims={0: [evidence[0].chunk_id]},
        )

    async def retrieve(question, scope):
        return [_chunk("The refund window is 30 days for annual plans.")]

    runner = EvaluationRunner(answer, retrieve)
    report = _run(
        runner.run(
            [
                EvalCase(question="refund?", forbidden_claims=("100% refunds forever",)),
            ]
        )
    )
    assert report.failed == 1
    assert report.forbidden_claim_hits == 1


# --- Release gates (ticket 34) ---


def _report(**kw) -> EvalReport:
    defaults = dict(
        run_id="r",
        started_at=0,
        finished_at=1,
        total=100,
        passed=95,
        failed=5,
        abstention_correct=95,
        abstention_false=5,
        citation_violations=3,
        forbidden_claim_hits=1,
    )
    defaults.update(kw)
    return EvalReport(**defaults)


# Healthy read-tool telemetry. Every gate call needs it because the
# read-tool gate fails closed on a missing measurement; supplying it here
# keeps these tests about what they name.
HEALTHY_READ_TOOLS = ReadToolOutcome(succeeded=1000, failed=1)
ZERO_VIOLATIONS = {
    "cross_tenant_violations": 0,
    "unauthorized_writes": 0,
    "duplicate_replies": 0,
}


def _gates(report: EvalReport | None = None, **kw):
    """`evaluate_release_gates` with the two non-eval inputs supplied."""
    return evaluate_release_gates(
        report if report is not None else _report(),
        security=kw.pop("security", dict(ZERO_VIOLATIONS)),
        read_tools=kw.pop("read_tools", HEALTHY_READ_TOOLS),
        **kw,
    )


def test_gates_pass_on_healthy_report() -> None:
    assert release_allowed(_gates())


def test_gates_block_cross_tenant_violation() -> None:
    gates = _gates(
        security={"cross_tenant_violations": 1, "unauthorized_writes": 0, "duplicate_replies": 0}
    )
    assert not release_allowed(gates)
    assert any(not g.passed and g.gate == "zero_cross_tenant" for g in gates)


def test_gates_block_low_citation_coverage() -> None:
    citation_gate = next(
        g for g in _gates(_report(citation_violations=20)) if g.gate == "citation_coverage"
    )
    assert not citation_gate.passed


def test_gates_block_low_abstention_accuracy() -> None:
    abst = next(
        g
        for g in _gates(_report(abstention_correct=70, abstention_false=30))
        if g.gate == "abstention_correct_rate"
    )
    assert not abst.passed


def test_gates_block_forbidden_claims() -> None:
    gates = _gates(_report(forbidden_claim_hits=10))
    g = next(g for g in gates if g.gate == "forbidden_claims")
    assert not g.passed


def test_default_thresholds_match_documented_floors() -> None:
    assert DEFAULT_THRESHOLDS.cross_tenant_violations == 0
    assert DEFAULT_THRESHOLDS.unauthorized_writes == 0
    assert DEFAULT_THRESHOLDS.duplicate_replies == 0
    assert DEFAULT_THRESHOLDS.max_citation_violation_rate <= 0.05


# --- PII + retention (ticket 36) ---


def test_classify_field_levels() -> None:
    assert classify_field("email") == Sensitivity.RESTRICTED
    assert classify_field("customer_name") == Sensitivity.CONFIDENTIAL
    assert classify_field("case_status") == Sensitivity.INTERNAL


def test_redact_text_patterns() -> None:
    text, n = redact_text("contact alice@example.com or +1 415 555 0100")
    assert "alice@example.com" not in text
    assert "[EMAIL]" in text
    assert n >= 1


def test_minimize_for_model_drops_restricted_redacts_confidential() -> None:
    out, report = minimize_payload(
        {"email": "a@b.test", "customer_name": "Alice alice@b.test", "status": "open"},
        destination="model",
    )
    assert "email" not in out  # restricted dropped
    assert "alice@b.test" not in str(out.get("customer_name"))  # value redacted
    assert out["status"] == "open"
    assert report.redaction_count >= 2


def test_minimize_for_log_drops_all_sensitive() -> None:
    out, report = minimize_payload(
        {"email": "a@b.test", "customer_name": "Alice", "status": "open"},
        destination="log",
    )
    assert "email" not in out and "customer_name" not in out
    assert out == {"status": "open"}


# --- Priority queues + backpressure (ticket 37) ---


def test_interactive_served_before_ingestion() -> None:
    mgr = PriorityQueueManager()
    mgr.submit(Queue.INGESTION, "doc-1")
    mgr.submit(Queue.INTERACTIVE, "conv-1")
    job = mgr.next_job()
    assert job.queue == Queue.INTERACTIVE


def test_backpressure_sheds_bulk_but_not_interactive() -> None:
    cfg = dict(DEFAULT_QUEUE_CONFIG)
    cfg[Queue.INTERACTIVE] = cfg[Queue.INTERACTIVE].__class__(high_watermark=10)
    mgr = PriorityQueueManager(config=cfg)
    for i in range(9):
        mgr.submit(Queue.INTERACTIVE, f"c{i}")
    # 80% watermark pressure: bulk submissions now shed
    with pytest.raises(QueueFull):
        mgr.submit(Queue.INGESTION, "doc-x")
    # interactive still admitted at 9/10
    mgr.submit(Queue.INTERACTIVE, "c-late")
    assert mgr.depth(Queue.INTERACTIVE) == 10


def test_interactive_own_watermark_admits_until_full() -> None:
    cfg = dict(DEFAULT_QUEUE_CONFIG)
    cfg[Queue.INTERACTIVE] = cfg[Queue.INTERACTIVE].__class__(high_watermark=2)
    mgr = PriorityQueueManager(config=cfg)
    mgr.submit(Queue.INTERACTIVE, "a")
    mgr.submit(Queue.INTERACTIVE, "b")
    with pytest.raises(QueueFull):
        mgr.submit(Queue.INTERACTIVE, "c")


def test_dead_letter_after_max_attempts() -> None:
    job = Queue.Job if False else None  # placeholder; use real Job
    from platform_core.evaluation.queues import Job

    job = Job(job_id="j1", queue=Queue.INGESTION, payload_ref="p")
    job.attempts = 5
    assert backoff_for(job) is None  # dead-letter
    job2 = Job(job_id="j2", queue=Queue.INGESTION, payload_ref="p")
    assert backoff_for(job2) is not None
