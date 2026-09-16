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
