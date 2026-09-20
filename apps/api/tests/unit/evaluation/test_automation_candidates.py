"""Leak analysis: which handoffs are ours to automate, and which are not.

A handoff histogram tells an operator nothing actionable on its own - 30%
handoffs that are all complaints and 30% that are all missing documents need
opposite responses. `automatable` is the field that carries that distinction,
and it is the one worth testing, because getting it wrong is worse than not
having the analysis at all: it would put the red lines on the automation list.
"""

from platform_core.evaluation.metrics import QualityMetrics, automation_candidates

_RED_LINES = (
    "COMPLAINT_REQUIRES_HUMAN",
    "STRATEGIC_ACCOUNT_REQUIRES_HUMAN",
    "REDLINE_COMMERCIAL_COMMITMENT",
    "SENSITIVE_REQUEST",
    "EQ_CONFIRMATION_REQUIRES_HUMAN",
)

_EVIDENCE_GAPS = ("NO_AUTHORIZED_EVIDENCE", "EVIDENCE_BELOW_THRESHOLD", "CONFLICTING_SOURCES")


def _metrics(counts: dict[str, int]) -> QualityMetrics:
    m = QualityMetrics(window_seconds=3600)
    m.handoff_reason_counts = dict(counts)
    return m


def test_a_missing_document_is_ours_to_fix() -> None:
    for reason in _EVIDENCE_GAPS:
        items = automation_candidates(_metrics({reason: 5}))
        assert items[0]["automatable"] is True, reason
        assert "document" in str(items[0]["rationale"])


def test_a_red_line_is_never_automatable() -> None:
    """The assertion this file exists for.

    These handoffs are produced by controls - the complaint gate, the
    commercial-commitment red line, the sensitive-request route. Recommending
    them for automation would be recommending that the control be undone, and
    it would look like an ordinary efficiency suggestion on a dashboard.
    """
    for reason in _RED_LINES:
        items = automation_candidates(_metrics({reason: 5}))
        assert items[0]["automatable"] is False, reason
        assert "policy" in str(items[0]["rationale"])


def test_an_unknown_reason_is_not_guessed_at() -> None:
    """Unclassified is reported as needing review, never as automatable.

    A new reason code appears the moment someone adds a gate; silently filing
    it under "automate" is how a red line gets automated before anyone reads
    it.
    """
    items = automation_candidates(_metrics({"SOME_NEW_REASON": 9}))

    assert items[0]["automatable"] is False
    assert "unclassified" in str(items[0]["rationale"])


def test_the_queue_is_ranked_by_volume() -> None:
    items = automation_candidates(
        _metrics({"NO_AUTHORIZED_EVIDENCE": 3, "CONFLICTING_SOURCES": 17, "OUT_OF_SCOPE": 8})
    )

    assert [item["count"] for item in items] == [17, 8, 3]
    assert items[0]["reason"] == "CONFLICTING_SOURCES"
