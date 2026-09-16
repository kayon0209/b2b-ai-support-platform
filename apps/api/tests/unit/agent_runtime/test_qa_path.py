"""Unit tests: citation validator + abstention gate (tickets 17-18)."""

import uuid

from platform_core.agent_runtime.qa_path import (
    ABSTAIN_LOW_RELEVANCE,
    ABSTAIN_NO_EVIDENCE,
    ABSTAIN_RESTRICTED,
    DraftAnswer,
    RetrievedChunk,
    decide_abstention,
    excerpt_hash,
    safe_abstention_text,
    validate_citations,
)


def _chunk(text: str = "The refund window is 30 days after purchase.") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_version_id=uuid.uuid4(),
        title="Policy",
        section_path=["Refunds"],
        excerpt=text,
        source_uri="minio://x",
        score=0.5,
    )


def _draft(cited_chunk_ids: list[uuid.UUID], claims: dict | None = None) -> DraftAnswer:
    return DraftAnswer(
        text="The refund window is 30 days.",
        claims=claims if claims is not None else {0: cited_chunk_ids},
    )


def test_valid_citations_pass() -> None:
    chunk = _chunk()
    result = validate_citations(_draft([chunk.chunk_id]), [chunk])
    assert result.ok is True


def test_claim_without_citation_fails() -> None:
    result = validate_citations(_draft([]), [_chunk()])
    assert result.ok is False
    assert result.reason_code == "UNSUPPORTED_CLAIM"
    assert 0 in result.unsupported_claims


def test_citation_to_chunk_not_in_context_fails() -> None:
    """The model must never cite a version that was not in its context."""
    phantom = uuid.uuid4()
    result = validate_citations(_draft([phantom]), [_chunk()])
    assert result.ok is False
    assert result.reason_code == "UNSUPPORTED_CLAIM"


def test_mixed_valid_and_phantom_citations_fail_per_claim() -> None:
    real = _chunk()
    draft = DraftAnswer(
        text="x",
        claims={
            0: [real.chunk_id],  # supported
            1: [uuid.uuid4()],  # phantom
        },
    )
    result = validate_citations(draft, [real])
    assert result.unsupported_claims == [1]


def test_empty_draft_fails() -> None:
    result = validate_citations(DraftAnswer(text=""), [_chunk()])
    assert result.ok is False
    assert result.reason_code == "NO_CLAIMS"


def test_abstain_when_no_evidence() -> None:
    decision = decide_abstention("how do refunds work?", [])
    assert decision.abstain is True
    assert decision.reason_code == ABSTAIN_NO_EVIDENCE
    assert decision.handoff is True


def test_no_abstain_with_relevant_evidence() -> None:
    decision = decide_abstention("how do refunds work?", [_chunk()])
    assert decision.abstain is False


def test_abstain_when_evidence_unrelated() -> None:
    unrelated = _chunk("Our shipping hours are 9am to 5pm local time.")
    decision = decide_abstention("how do refunds work?", [unrelated])
    assert decision.abstain is True
    assert decision.reason_code == ABSTAIN_LOW_RELEVANCE
    assert decision.handoff is True


def test_restricted_request_always_hands_off() -> None:
    decision = decide_abstention("export all customer data", [_chunk()], restricted_query=True)
    assert decision.abstain is True
    assert decision.reason_code == ABSTAIN_RESTRICTED
    assert decision.handoff is True


def test_abstention_text_never_invents_explanation() -> None:
    text = safe_abstention_text(ABSTAIN_RESTRICTED)
    assert "cannot" in text or "can't" in text
    assert len(text) < 300  # concise; no fabricated detail


def test_excerpt_hash_stable() -> None:
    assert excerpt_hash("abc") == excerpt_hash("abc")
    assert excerpt_hash("abc") != excerpt_hash("abd")


# --- Regression: stopwords must not make an unrelated question look grounded ---
#
# Before the stopword filter, `_term_overlap` counted function words, so the
# single shared token "the" was enough to clear MIN_EXCERPT_OVERLAP (0.12):
#   "who won the world cup in 1998?"  -> 0.167  (passed)
#   "what is the capital of the moon?" -> 0.250  (passed)
#   "the the the the"                  -> 1.000  (passed)
# An off-topic question would therefore reach the model on irrelevant evidence,
# defeating the abstention contract in docs/agent.md.


def test_stopword_only_query_abstains() -> None:
    """A query of function words carries no topic and must abstain."""
    decision = decide_abstention("the the the the", [_chunk()])
    assert decision.abstain is True
    assert decision.reason_code == ABSTAIN_LOW_RELEVANCE


def test_shared_stopword_does_not_imply_relevance() -> None:
    """Off-topic questions that happen to share "the"/"who"/"what" with the
    excerpt must still abstain."""
    excerpt = _chunk("The refund window is 30 days for annual plans.")
    for query in (
        "what is the capital of the moon?",
        "who won the world cup in 1998?",
        "how is the weather today?",
    ):
        decision = decide_abstention(query, [excerpt])
        assert decision.abstain is True, query
        assert decision.reason_code == ABSTAIN_LOW_RELEVANCE, query


def test_on_topic_query_still_passes_after_stopword_filter() -> None:
    """Tightening must not cause false abstention on a real question."""
    excerpt = _chunk("The refund window is 30 days for annual plans.")
    for query in (
        "how long is the refund window?",
        "what is the refund policy?",
        "refund window",
    ):
        decision = decide_abstention(query, [excerpt])
        assert decision.abstain is False, query


def test_content_terms_drops_stopwords_and_short_tokens() -> None:
    from platform_core.agent_runtime.qa_path import _content_terms

    terms = _content_terms("What is the refund window?")
    assert "refund" in terms
    assert "window" in terms
    assert "the" not in terms
    assert "what" not in terms
    assert "is" not in terms
