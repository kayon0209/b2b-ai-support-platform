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

from platform_core.agent_runtime.intent import classify
from platform_core.agent_runtime.qa_path import (
    ABSTAIN_NO_EVIDENCE,
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
    # The routing class `intent.classify` must produce for this question, or
    # empty for "no expectation declared".
    #
    # Empty is the default because the alternative was actively misleading: it
    # used to default to `"knowledge_qa"`, which nothing read, so every case
    # that never set it claimed to expect the knowledge path while seven of
    # them in fact route to `sensitive`, `human_required`, `business_read` or
    # `business_write`. Anyone auditing the dataset would have concluded the
    # classifier was badly broken. A declared route is now asserted by
    # `EvalRunner.run_case`, which is what makes this a taxonomy guard rather
    # than a comment - and it is the guard that has to exist before a verb can
    # be added to `ACTION_VERBS`, because the failure mode of that change is
    # a question moving to a route nobody intended.
    expected_route: str = ""
    # claims that MUST appear in the answer (substring match on canonical text)
    required_claims: tuple[str, ...] = ()
    # claims that must NOT appear
    forbidden_claims: tuple[str, ...] = ()
    must_abstain: bool = False
    restricted_query: bool = False
    expected_handoff: bool = False
    # The corpus exists in one language and this question is asked in another
    # (ADR 0009). Set only on cases that are answerable in principle but whose
    # evidence the retriever cannot reach *because of the language gap*. It
    # buys exactly one thing: an abstention caused by `NO_AUTHORIZED_EVIDENCE`
    # on this case is counted in `cross_lingual_unreachable` instead of in the
    # `abstention_correct_rate` denominator. It does NOT excuse the case from
    # `passed`/`failed`, from `expected_route`, or from any other gate, and
    # `cross_lingual_unreachable` is itself asserted, so the gap stays visible.
    cross_lingual: bool = False
    allowed_tools: tuple[str, ...] = ()
    # Corpus keys this question SHOULD retrieve (iteration plan 4.1). Empty
    # for abstention/adversarial cases - the gate must not credit a retrieval
    # miss on a question that should never have retrieved at all.
    expected_version_keys: tuple[str, ...] = ()
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
    # claim_index -> that claim's sentence, so a reported candidate can be
    # judged by eye instead of re-run. Stored rather than inferred: the whole
    # point is to be able to say which sentence the rule objected to.
    claim_texts: dict[int, str] = field(default_factory=dict)
    forbidden_hit: bool = False
    required_missing: bool = False
    latency_ms: int = 0
    # Attribution (plan 4.1): PASS / RETRIEVAL_MISS / GENERATION_ERROR /
    # ABSTENTION_ERROR. The whole point of the eval is to stop reporting a
    # bare boolean that cannot tell "the index missed it" from "the model
    # ignored it".
    attribution: str = ""
    recall_at_k: float | None = None
    # ADR 0009. Defaults to True, i.e. fail-closed: an abstention counts
    # against the rate unless this case *and* its reason code both say the
    # cause was the language gap. Reversed, every new case would silently
    # exempt itself, which is the failure this field is most likely to cause.
    abstention_attributable: bool = True
    retrieved_keys: list[str] = field(default_factory=list)


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
    # ADR 0009: abstentions excluded from `abstention_correct_rate` because the
    # only cause was the language gap between question and corpus. Counted, not
    # forgotten - `gates.py` asserts this equals the number of declared
    # `cross_lingual` cases, so a gap can never be excluded *and* unreported.
    cross_lingual_unreachable: int = 0
    # How many cases *declared* `cross_lingual`. Reported for visibility: a
    # `must_abstain` cross-lingual case is declared but never excluded (its
    # abstention is already correct), so this number is legitimately larger
    # than `cross_lingual_unreachable`.
    declared_cross_lingual: int = 0
    # Declarations the exemption could actually apply to - answerable cases
    # that declared `cross_lingual`. This is what `gates.py` compares against
    # `cross_lingual_unreachable`, because comparing against every declaration
    # would fail a correct run that merely contains a must_abstain one.
    exemptible_cross_lingual: int = 0
    results: list[CaseResult] = field(default_factory=list)

    @property
    def recall_measured(self) -> int:
        return sum(1 for r in self.results if r.recall_at_k is not None)

    @property
    def retrieval_recall_mean(self) -> float:
        values = [r.recall_at_k for r in self.results if r.recall_at_k is not None]
        return sum(values) / len(values) if values else 1.0

    @property
    def attribution_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.results:
            if r.attribution:
                counts[r.attribution] = counts.get(r.attribution, 0) + 1
        return counts

    @property
    def abstention_correct_rate(self) -> float:
        """Correct abstention decisions, over the cases the rate applies to.

        `n == 0` returns 1.0 because there would be nothing to be wrong about.
        That is only defensible while `cross_lingual_unreachable` cannot grow
        to swallow the whole dataset - `gates.py` asserts it stays a strict
        minority, so this branch cannot be used to buy a perfect score.
        """
        n = self.abstention_correct + self.abstention_false
        return self.abstention_correct / n if n else 1.0

    @property
    def citation_coverage(self) -> float:
        """Fraction of non-abstaining answers with fully supported citations."""
        answered = sum(1 for r in self.results if not r.abstained)
        good = sum(1 for r in self.results if not r.abstained and r.citation_ok)
        return good / answered if answered else 1.0


class EvaluationRunner:
    def __init__(
        self,
        answer_fn: AnswerFn,
        retrieve_fn: RetrieveFn,
        *,
        key_of: Callable[[RetrievedChunk], str] | None = None,
    ) -> None:
        """retrieve_fn(question, principal_scope) -> list[RetrievedChunk].

        `key_of` maps a retrieved chunk to its corpus version key, so
        attribution can compare what came back against what SHOULD have come
        back. Defaults to the chunk title; the eval harness supplies the
        real key mapping because only it knows the corpus it built.
        """
        self._answer_fn = answer_fn
        self._retrieve_fn = retrieve_fn
        self._key_of = key_of or (lambda chunk: chunk.title)

    async def run_case(self, case: EvalCase) -> CaseResult:
        started = time.monotonic()
        # Classified first and recorded on the result regardless: `CaseResult.route`
        # was a field nothing ever wrote, so a report could not say which route a
        # case had taken - the one fact needed to explain a routing regression.
        route = classify(case.question).route.value
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
            route=route,
            reason_codes=[decision.reason_code] if decision.reason_code else [],
        )

        if (
            decision.abstain
            and not case.must_abstain
            and case.cross_lingual
            and decision.reason_code == ABSTAIN_NO_EVIDENCE
        ):
            # ADR 0009. Every clause is load-bearing:
            #
            # - `not case.must_abstain`: this case was expected to abstain, so
            #   its abstention is already a *correct* one. Exempting it would
            #   quietly delete a passing case from the denominator, which can
            #   only ever help the score - the exemption exists to stop a
            #   language gap from being punished, not to stop correct behaviour
            #   from being counted.
            # - `case.cross_lingual`: the declaration alone does not prove the
            #   cause, so it is never consulted on its own.
            # - `reason_code == ABSTAIN_NO_EVIDENCE`: nothing came back at all.
            #   `ABSTAIN_LOW_RELEVANCE` deliberately does NOT qualify - evidence
            #   was retrieved and scored too low, which is a retriever-tuning
            #   problem rather than a language gap, and exempting it would hide
            #   the more actionable of the two.
            result.abstention_attributable = False

        if case.expected_route and route != case.expected_route:
            # Asserted, not reported. A declared route is a statement about the
            # taxonomy, and the taxonomy is what decides whether a question is
            # answered, handed off, or sent to the write gateway - so a case
            # that silently changes route is the regression this field exists
            # to catch. Empty means the case declares nothing, which is honest
            # and is why the default is empty rather than `knowledge_qa`.
            result.passed = False
            result.reason_codes.append("ROUTE_MISMATCH")
            return result

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
            result.claim_texts = dict(draft.claim_texts)
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

        # --- Attribution (plan 4.1). ---
        # Computed for every case so a regression report can be sorted by
        # WHERE the pipeline broke, not just THAT it broke.
        retrieved = [self._key_of(chunk) for chunk in evidence]
        result.retrieved_keys = sorted({k for k in retrieved if k})
        if case.expected_version_keys:
            hits = sum(1 for k in case.expected_version_keys if k in result.retrieved_keys)
            result.recall_at_k = hits / len(case.expected_version_keys)
        if case.must_abstain:
            result.attribution = (
                "PASS"
                if result.passed
                else ("ABSTENTION_ERROR" if not decision.abstain else "GENERATION_ERROR")
            )
        elif case.expected_version_keys and result.recall_at_k == 0.0:
            result.attribution = "RETRIEVAL_MISS"
        elif not result.passed:
            result.attribution = "ABSTENTION_ERROR" if result.abstained else "GENERATION_ERROR"
        else:
            result.attribution = "PASS"

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
                if r.abstention_attributable
                and (
                    (c.must_abstain and r.abstained)
                    or (not c.must_abstain and not r.abstained)
                )
            ),
            abstention_false=sum(
                1
                for r, c in zip(results, cases, strict=False)
                if r.abstention_attributable
                and (
                    (c.must_abstain and not r.abstained)
                    or (not c.must_abstain and r.abstained)
                )
            ),
            cross_lingual_unreachable=sum(1 for r in results if not r.abstention_attributable),
            # Counted from the cases, not the results, so the gate comparing
            # the two is comparing an intention against an outcome rather than
            # a number against itself.
            declared_cross_lingual=sum(1 for c in cases if c.cross_lingual),
            exemptible_cross_lingual=sum(
                1 for c in cases if c.cross_lingual and not c.must_abstain
            ),
            citation_violations=sum(1 for r in results if not r.citation_ok),
            forbidden_claim_hits=sum(1 for r in results if r.forbidden_hit),
            contradiction_candidates=sum(len(r.contradicted_claims) for r in results),
            results=results,
        )
        return report
