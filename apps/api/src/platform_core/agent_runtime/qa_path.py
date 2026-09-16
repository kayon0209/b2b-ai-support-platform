"""Knowledge QA path: citation validator + answer/abstain decision
(tickets 17-18).

Safety contract (docs/agent.md):
- Every enterprise factual claim needs a citation to a version that was in
  the runtime context (never an invented reference).
- Missing/conflicting/expired evidence -> abstain or handoff, never guess.
- The model call itself is pluggable; this module owns validation and the
  decision logic deterministically (LLM proposes, code disposes).
"""

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from platform_core.retrieval.hybrid import RetrievedChunk


class CitationError(Exception):
    pass


@dataclass
class DraftAnswer:
    """Model-proposed answer plus per-claim citation references."""

    text: str
    # claim_index -> referenced chunk ids
    claims: dict[int, list[uuid.UUID]] = field(default_factory=dict)
    route: str = "knowledge_qa"


@dataclass
class ValidationResult:
    ok: bool
    reason_code: str = ""
    unsupported_claims: list[int] = field(default_factory=list)


class AnswerGenerator(Protocol):
    """Pluggable LLM boundary. Implementations receive redacted, minimized
    context and return a DraftAnswer. Never executed synchronously in the
    webhook path."""

    async def generate(self, question: str, evidence: list[RetrievedChunk]) -> DraftAnswer: ...


def validate_citations(draft: DraftAnswer, evidence: list[RetrievedChunk]) -> ValidationResult:
    """Citation validator (ticket 17).

    Rules:
    1. Every claim must cite at least one chunk.
    2. Cited chunks must be from the provided evidence set — citing a
       document version that was not in context is a hard failure.
    3. The draft must have at least one claim (empty answers are not
       publishable).
    """
    if not draft.claims:
        return ValidationResult(ok=False, reason_code="NO_CLAIMS", unsupported_claims=[])
    evidence_ids = {c.chunk_id for c in evidence}
    unsupported: list[int] = []
    for claim_index, cited in draft.claims.items():
        if not cited:
            unsupported.append(claim_index)
            continue
        if not evidence_ids.issuperset(cited):
            unsupported.append(claim_index)
    if unsupported:
        return ValidationResult(
            ok=False,
            reason_code="UNSUPPORTED_CLAIM",
            unsupported_claims=unsupported,
        )
    return ValidationResult(ok=True)


# --- Abstention decision (ticket 18) ---

ABSTAIN_NO_EVIDENCE = "NO_AUTHORIZED_EVIDENCE"
ABSTAIN_CONFLICT = "CONFLICTING_SOURCES"
ABSTAIN_LOW_RELEVANCE = "EVIDENCE_BELOW_THRESHOLD"
ABSTAIN_RESTRICTED = "RESTRICTED_REQUEST"


@dataclass
class AbstentionDecision:
    abstain: bool
    reason_code: str = ""
    handoff: bool = False


MIN_EXCERPT_OVERLAP = 0.12


def _stem(term: str) -> str:
    """Crude singular/stem fold so refund~refunds, day~days match.

    Good enough for a conservative proxy; not a real stemmer.
    """
    for suffix in ("s", "es", "ing", "ed"):
        if term.endswith(suffix) and len(term) > len(suffix) + 2:
            return term[: -len(suffix)]
    return term


def _term_overlap(query: str, excerpt: str) -> float:
    """Cheap lexical overlap: fraction of query terms present in excerpt.

    A real semantic confidence model arrives with the LLM integration;
    per docs/agent.md we treat similarity scores as ranking signals, not
    confidence, so abstention uses this conservative groundedness proxy.
    """
    q_terms = {_stem(t) for t in re.findall(r"\w+", query.lower()) if len(t) > 2}
    if not q_terms:
        return 0.0
    e_terms = {_stem(t) for t in re.findall(r"\w+", excerpt.lower())}
    return len(q_terms & e_terms) / len(q_terms)


def decide_abstention(
    query: str,
    evidence: list[RetrievedChunk],
    *,
    min_results: int = 1,
    min_overlap: float = MIN_EXCERPT_OVERLAP,
    restricted_query: bool = False,
) -> AbstentionDecision:
    """Deterministic abstention gate (ticket 18).

    Never guesses when: no evidence, evidence is topically unrelated, or
    the request touches restricted data. Unrelated evidence with zero
    relevant candidates triggers handoff (not just clarification) because
    repeatedly probing retrieval wastes the customer's time.
    """
    if restricted_query:
        return AbstentionDecision(abstain=True, reason_code=ABSTAIN_RESTRICTED, handoff=True)
    if not evidence:
        return AbstentionDecision(abstain=True, reason_code=ABSTAIN_NO_EVIDENCE, handoff=True)
    if len(evidence) < min_results:
        return AbstentionDecision(abstain=True, reason_code=ABSTAIN_NO_EVIDENCE, handoff=False)
    best_overlap = max(_term_overlap(query, c.excerpt) for c in evidence)
    if best_overlap < min_overlap:
        return AbstentionDecision(abstain=True, reason_code=ABSTAIN_LOW_RELEVANCE, handoff=True)
    return AbstentionDecision(abstain=False)


def safe_abstention_text(reason_code: str) -> str:
    """Customer-safe abstention reply: state what cannot be verified,
    offer handoff, never invent an explanation (docs/agent.md)."""
    if reason_code == ABSTAIN_RESTRICTED:
        return (
            "I can't provide that information. Let me connect you with a "
            "human colleague who can help."
        )
    return (
        "I couldn't verify an answer from our authorized knowledge base. "
        "I can connect you with a human colleague, or you can rephrase the "
        "question."
    )


def excerpt_hash(excerpt: str) -> str:
    return hashlib.sha256(excerpt.encode()).hexdigest()
