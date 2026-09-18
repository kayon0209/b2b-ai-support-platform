"""Evaluation dataset + runner (ticket 33, docs/testing-and-evaluation.md).

Dataset case shape per the documented schema:
  question, actor (role/enterprise account), authorized knowledge versions,
  expected route, required and forbidden claims, expected citations,
  answer rubric, must-abstain flag, allowed tools, expected handoff reason.

The runner is LLM-agnostic: it drives the QA path (retrieve -> decide ->
draft -> validate) with a pluggable AnswerGenerator and scores deterministic
metrics (abstention correctness, citation support, unsafe-action refusal).
LLM-judged rubric scoring plugs in at the same seam.
"""

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from platform_core.agent_runtime.qa_path import (
    DraftAnswer,
    claim_contradiction_candidates,
    decide_abstention,
    validate_citations,
)

# RetrievedChunk is defined in retrieval.hybrid; qa_path re-exports it for
# its own signature. Import it from the defining module so the dependency is
# explicit rather than riding on someone else's re-export.
from platform_core.retrieval.hybrid import PrincipalScope, RetrievedChunk


class AnswerFn(Protocol):
    """pluggable generation: evidence + question -> DraftAnswer."""

    async def __call__(self, question: str, evidence: list[RetrievedChunk]) -> DraftAnswer: ...


# The retrieval seam: a question plus the caller's ACL scope -> evidence.
RetrieveFn = Callable[[str, PrincipalScope], Awaitable[list[RetrievedChunk]]]


@dataclass
class EvalCase:
    question: str
    role: str = "support_agent"
    principal_groups: tuple[str, ...] = ()
    expected_route: str = "knowledge_qa"
    # claims that MUST appear in the answer (substring match on canonical text)
    required_claims: tuple[str, ...] = ()
    # claims that must NOT appear
    forbidden_claims: tuple[str, ...] = ()
    must_abstain: bool = False
    restricted_query: bool = False
    expected_handoff: bool = False
    allowed_tools: tuple[str, ...] = ()
    case_id: str = ""

    def __post_init__(self) -> None:
        if not self.case_id:
            self.case_id = f"eval-{uuid.uuid4().hex[:10]}"


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    abstained: bool = False
    handoff: bool = False
    route: str = ""
    reason_codes: list[str] = field(default_factory=list)
    unsupported_claims: list[int] = field(default_factory=list)
    citation_ok: bool = True
    # ADR 0005: claims whose text negates a term their cited excerpt affirms.
    # A *metric*, not a verdict - it has known false positives, so it is
    # reported and never enforced. It exists so its precision can be measured
    # on the dataset before it is ever promoted to a guard.
    contradicted_claims: list[int] = field(default_factory=list)
    forbidden_hit: bool = False
    required_missing: bool = False
    latency_ms: int = 0


@dataclass
class EvalReport:
    run_id: str
    started_at: int
    finished_at: int
    total: int
    passed: int
    failed: int
    abstention_correct: int
    abstention_false: int
    citation_violations: int
    forbidden_claim_hits: int
    # Sum over cases of `contradicted_claims`. Reported, never gated.
    contradiction_candidates: int = 0
    unsafe_action_attempts: int = 0
    results: list[CaseResult] = field(default_factory=list)

    @property
    def abstention_correct_rate(self) -> float:
        n = self.abstention_correct + self.abstention_false
        return self.abstention_correct / n if n else 1.0

    @property
    def citation_coverage(self) -> float:
        """Fraction of non-abstaining answers with fully supported citations."""
        answered = sum(1 for r in self.results if not r.abstained)
        good = sum(1 for r in self.results if not r.abstained and r.citation_ok)
        return good / answered if answered else 1.0


class EvaluationRunner:
    def __init__(self, answer_fn: AnswerFn, retrieve_fn: RetrieveFn) -> None:
        """retrieve_fn(question, principal_scope) -> list[RetrievedChunk]."""
        self._answer_fn = answer_fn
        self._retrieve_fn = retrieve_fn

    async def run_case(self, case: EvalCase) -> CaseResult:
        started = time.monotonic()
        scope = PrincipalScope(
            principal_types=("role", "department"),
            principal_ids=(case.role, *case.principal_groups),
        )
        evidence = await self._retrieve_fn(case.question, scope)

        decision = decide_abstention(
            case.question,
            evidence,
            restricted_query=case.restricted_query,
        )
        result = CaseResult(
            case_id=case.case_id,
            passed=True,
            abstained=decision.abstain,
            handoff=decision.handoff,
            reason_codes=[decision.reason_code] if decision.reason_code else [],
        )

        if case.must_abstain:
            result.passed = decision.abstain
            if decision.abstain:
                result.passed = decision.handoff == case.expected_handoff
        elif decision.abstain:
            # false abstention: answerable case rejected
            result.passed = False
            result.abstained = True
        else:
            draft = await self._answer_fn(case.question, evidence)
            validation = validate_citations(draft, evidence)
            result.citation_ok = validation.ok
            result.unsupported_claims = validation.unsupported_claims
            # Measured regardless of whether the citations resolved: a claim
            # that contradicts its excerpt is worth seeing even when it cites
            # something real, which is the case validation cannot see.
            result.contradicted_claims = claim_contradiction_candidates(draft, evidence)
            if not validation.ok:
                result.passed = False
                result.reason_codes.append(validation.reason_code)

            text = draft.text.lower()
            missing = [c for c in case.required_claims if c.lower() not in text]
            hit = [c for c in case.forbidden_claims if c.lower() in text]
            result.required_missing = bool(missing)
            result.forbidden_hit = bool(hit)
            if missing:
                result.passed = False
                result.reason_codes.append("REQUIRED_CLAIM_MISSING")
            if hit:
                result.passed = False
                result.forbidden_hit = True
                result.reason_codes.append("FORBIDDEN_CLAIM_PRESENT")

        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result

    async def run(self, cases: list[EvalCase]) -> EvalReport:
        run_id = str(uuid.uuid4())
        started = int(time.time())
        results: list[CaseResult] = []
        for case in cases:
            results.append(await self.run_case(case))
        report = EvalReport(
            run_id=run_id,
            started_at=started,
            finished_at=int(time.time()),
            total=len(results),
            passed=sum(1 for r in results if r.passed),
            failed=sum(1 for r in results if not r.passed),
            abstention_correct=sum(
                1
                for r, c in zip(results, cases, strict=False)
                if (c.must_abstain and r.abstained) or (not c.must_abstain and not r.abstained)
            ),
            abstention_false=sum(
                1
                for r, c in zip(results, cases, strict=False)
                if (c.must_abstain and not r.abstained) or (not c.must_abstain and r.abstained)
            ),
            citation_violations=sum(1 for r in results if not r.citation_ok),
            forbidden_claim_hits=sum(1 for r in results if r.forbidden_hit),
            contradiction_candidates=sum(len(r.contradicted_claims) for r in results),
            results=results,
        )
        return report
