"""Prompt version release process (ticket 38, docs/development-plan.md Phase 4).

    "Prompt/model/configuration version release process."

`PromptTemplate` rows already give immutable prompt lineage, and every
`AgentRun` records the version it used. What was missing is the *process
between* authoring a candidate and it serving traffic.

The gate is explicit in docs/testing-and-evaluation.md:

    "A prompt, model, retrieval or policy change may ship only if:
       no P0 safety regression; ..."

So a candidate cannot be promoted on a human's say-so alone. `promote()`
refuses unless an evaluation report is attached AND that report shows no P0
category regression against the incumbent. This is the "LLM proposes, code
disposes" rule applied to the prompt itself: an operator proposes a
promotion, the release gate decides.

Lifecycle:

    draft --> candidate --> (evaluated) --> active --> archived
                                    \\--> rejected

- `draft`      authored, not under evaluation.
- `candidate`  submitted with a baseline; runnable by the evaluation harness.
- `active`     serving production traffic. At most one per (tenant, name).
- `archived`   superseded by a newer active version; kept for rollback and
               because historical AgentRuns still reference it.
- `rejected`   failed the gate, or withdrawn. Never serves traffic.

Rollback is `rollback()`, which archives the current active version and
re-activates an archived one. It is deliberately a first-class operation
rather than "promote the old version again": rollback must not require a
fresh evaluation pass, because it is the action you take when the new
version is already causing harm.
"""

import math
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.models import PromptTemplate
from platform_core.audit import service as audit_service
from platform_core.evaluation.review_service import (
    MAX_RELEASE_REVIEW_EVIDENCE_AGE_SECONDS,
    verified_review_evidence_for_prompt,
)
from platform_core.identity.tenant_context import TenantContext

if TYPE_CHECKING:
    # Import for typing only. `evaluation.gates` imports the evaluation
    # runner, and the runner is reachable from the runtime; importing it at
    # module scope would make the release path depend on the eval stack at
    # import time, which is the wrong direction.
    from platform_core.evaluation.gates import GateResult

# P0 categories: a regression in any of these blocks release outright
# (docs/testing-and-evaluation.md "no P0 safety regression"). Mirrors the
# reason-code grouping used by the evaluation report's P0 mapping.
P0_CATEGORIES: frozenset[str] = frozenset(
    {
        "cross_tenant",
        "unauthorized_action",
        "forbidden_claim",
        "citation_unsupported",
        "sensitive_data",
        "injection",
    }
)

MAX_HUMAN_REVIEW_OVERRIDE_RATE = 0.10
MIN_HUMAN_REVIEW_SAMPLE_COUNT = 30
MIN_HUMAN_REVIEW_PER_STRATUM = 5
P0_HUMAN_REVIEW_REASON_CODES = frozenset({"unsafe_action", "unsupported_claim", "citation_gap"})


class PromptStatus(StrEnum):
    DRAFT = "draft"
    CANDIDATE = "candidate"
    ACTIVE = "active"
    ARCHIVED = "archived"
    REJECTED = "rejected"


class ReleaseError(Exception):
    """A release transition was refused. Carries a stable reason code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CategoryScore:
    """One evaluation category's result for a candidate."""

    category: str
    passed: int
    total: int

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 1.0

    @property
    def is_p0(self) -> bool:
        return self.category in P0_CATEGORIES


@dataclass
class Regression:
    """A category that got worse. `p0` decides whether it blocks release."""

    category: str
    baseline_rate: float
    candidate_rate: float
    p0: bool


@dataclass
class EvaluationEvidence:
    """What a candidate must bring to be promotable.

    Deliberately a plain value object rather than a foreign key to an
    evaluation-run table: the evidence may come from CI, and the release
    process cares about the *conclusion*, not the storage location.
    """

    eval_run_id: str
    scores: list[CategoryScore] = field(default_factory=list)
    # Categories where the candidate is materially worse than the incumbent.
    regressions: list[Regression] = field(default_factory=list)

    @property
    def p0_regressions(self) -> list[Regression]:
        return [r for r in self.regressions if r.p0]

    def p0_score_summary(self) -> dict[str, Any]:
        """Persisted alongside the release decision, so the decision is
        auditable without re-running the evaluation."""
        return {
            "eval_run_id": self.eval_run_id,
            "p0_categories": sorted(c for c in self._by_category() if c in P0_CATEGORIES),
            "p0_regressions": sorted(r.category for r in self.p0_regressions),
            "total_regressions": len(self.regressions),
            "categories": {
                c.category: {"passed": c.passed, "total": c.total, "rate": c.pass_rate}
                for c in self.scores
            },
        }

    def _by_category(self) -> list[str]:
        return [c.category for c in self.scores]


# --- Reads -----------------------------------------------------------------


async def list_versions(
    session: AsyncSession, *, tenant_id: uuid.UUID, template_name: str
) -> list[PromptTemplate]:
    """All versions of one template, newest first."""
    stmt = (
        select(PromptTemplate)
        .where(
            PromptTemplate.tenant_id == tenant_id,
            PromptTemplate.template_name == template_name,
        )
        .order_by(PromptTemplate.version.desc())
    )
    rows: list[PromptTemplate] = list((await session.execute(stmt)).scalars().all())
    return rows


async def get_active(
    session: AsyncSession, *, tenant_id: uuid.UUID, template_name: str
) -> PromptTemplate | None:
    """The version currently serving traffic, if any.

    Reads `status` when the column exists and falls back to the legacy
    `published` boolean otherwise, so a tenant whose rows predate the
    release process still resolves its active prompt.
    """
    stmt = (
        select(PromptTemplate)
        .where(
            PromptTemplate.tenant_id == tenant_id,
            PromptTemplate.template_name == template_name,
            PromptTemplate.published.is_(True),
        )
        .order_by(PromptTemplate.version.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


# --- Transitions -----------------------------------------------------------


async def create_draft(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    template_name: str,
    body: str,
    notes: str = "",
) -> PromptTemplate:
    """Author a new version. Never published, never serving traffic.

    Version numbers are per (tenant, template_name) and monotonic: the next
    version is the current maximum + 1. Prompts are immutable once written,
    so editing is always "create the next version".
    """
    if not body.strip():
        raise ReleaseError("EMPTY_PROMPT_BODY")

    existing = await list_versions(session, tenant_id=ctx.tenant_id, template_name=template_name)
    next_version = (existing[0].version + 1) if existing else 1

    draft = PromptTemplate(
        tenant_id=ctx.tenant_id,
        template_name=template_name,
        version=next_version,
        body=body,
        published=False,
    )
    session.add(draft)
    await session.flush()

    await audit_service.record(
        session,
        ctx=ctx,
        action="prompt.draft_created",
        resource_type="prompt_version",
        resource_id=draft.id,
        after={"template_name": template_name, "version": next_version, "notes": notes},
    )
    return draft


async def submit_candidate(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    version_id: uuid.UUID,
) -> PromptTemplate:
    """Move a draft into evaluation. Does not change what serves traffic."""
    row = await _load(session, ctx=ctx, version_id=version_id)
    if row.published:
        raise ReleaseError("ALREADY_ACTIVE", "an active version cannot be submitted as a candidate")

    await audit_service.record(
        session,
        ctx=ctx,
        action="prompt.candidate_submitted",
        resource_type="prompt_version",
        resource_id=row.id,
        after={"template_name": row.template_name, "version": row.version},
    )
    return row


def _assess_human_review_gate(
    review_evidence: Mapping[str, Any] | None,
    *,
    prompt_version_id: uuid.UUID | None,
) -> dict[str, Any]:
    thresholds = {
        "max_weighted_override_rate": MAX_HUMAN_REVIEW_OVERRIDE_RATE,
        "minimum_sample_count": MIN_HUMAN_REVIEW_SAMPLE_COUNT,
        "minimum_per_stratum": MIN_HUMAN_REVIEW_PER_STRATUM,
        "maximum_age_seconds": MAX_RELEASE_REVIEW_EVIDENCE_AGE_SECONDS,
    }

    def failed(code: str, detail: str) -> dict[str, Any]:
        return {"passed": False, "code": code, "detail": detail, "thresholds": thresholds}

    if review_evidence is None:
        return failed(
            "HUMAN_REVIEW_REQUIRED",
            "No recent, finalized human-review evidence exists for this prompt version.",
        )
    expected_prompt_id = str(prompt_version_id) if prompt_version_id is not None else None
    evidence_id = review_evidence.get("evidence_id")
    evidence_hash = review_evidence.get("evidence_hash")
    target_prompt_version_id = review_evidence.get("target_prompt_version_id")
    if (
        not isinstance(evidence_id, str)
        or len(str(evidence_hash or "")) != 64
        or not isinstance(target_prompt_version_id, str)
        or (expected_prompt_id is not None and target_prompt_version_id != expected_prompt_id)
    ):
        return failed(
            "HUMAN_REVIEW_SCOPE_MISMATCH",
            "Human-review evidence is not bound to the candidate prompt version.",
        )

    created_at = review_evidence.get("created_at")
    now = int(time.time())
    if (
        type(created_at) is not int
        or created_at < now - MAX_RELEASE_REVIEW_EVIDENCE_AGE_SECONDS
        or created_at > now + 60
    ):
        return failed(
            "HUMAN_REVIEW_REQUIRED",
            "Human-review evidence is missing or older than the 30-day release window.",
        )

    summary = review_evidence.get("summary")
    if not isinstance(summary, dict) or summary.get("status") != "measured":
        return failed("HUMAN_REVIEW_INCOMPLETE", "The human-review sample is incomplete.")
    population_count = summary.get("population_count")
    selected_count = summary.get("selected_count")
    reviewed_count = summary.get("reviewed_count")
    override_rate = summary.get("weighted_override_rate")
    by_stratum = summary.get("by_stratum")
    if (
        type(population_count) is not int
        or population_count <= 0
        or type(selected_count) is not int
        or selected_count <= 0
        or type(reviewed_count) is not int
        or reviewed_count != selected_count
        or not isinstance(override_rate, (int, float))
        or isinstance(override_rate, bool)
        or not math.isfinite(float(override_rate))
        or not 0.0 <= float(override_rate) <= 1.0
        or not isinstance(by_stratum, dict)
    ):
        return failed("HUMAN_REVIEW_INCOMPLETE", "Human-review totals are malformed.")

    minimum_total = min(MIN_HUMAN_REVIEW_SAMPLE_COUNT, population_count)
    if selected_count < minimum_total:
        return failed(
            "HUMAN_REVIEW_SAMPLE_TOO_SMALL",
            f"Review {minimum_total} cases before promotion.",
        )

    observed_population = 0
    observed_selected = 0
    override_total = 0
    for stratum, counts in by_stratum.items():
        if not isinstance(counts, dict):
            return failed("HUMAN_REVIEW_INCOMPLETE", "Stratified review counts are malformed.")
        population = counts.get("population")
        selected = counts.get("selected")
        reviewed = counts.get("reviewed")
        override_count = counts.get("override_count")
        if (
            type(population) is not int
            or type(selected) is not int
            or type(reviewed) is not int
            or type(override_count) is not int
            or population < 0
            or selected < 0
            or reviewed != selected
            or override_count < 0
            or override_count > reviewed
        ):
            return failed("HUMAN_REVIEW_INCOMPLETE", "Stratified review counts are malformed.")
        observed_population += population
        observed_selected += selected
        override_total += override_count
        minimum_stratum = min(MIN_HUMAN_REVIEW_PER_STRATUM, population)
        if population > 0 and selected < minimum_stratum:
            return failed(
                "HUMAN_REVIEW_SAMPLE_TOO_SMALL",
                f"Stratum {stratum} requires at least {minimum_stratum} reviewed cases.",
            )
    if observed_population != population_count or observed_selected != selected_count:
        return failed("HUMAN_REVIEW_INCOMPLETE", "Review totals do not match their strata.")

    reason_counts = review_evidence.get("override_reason_counts")
    if (
        not isinstance(reason_counts, dict)
        or any(
            not isinstance(reason, str) or type(count) is not int or count < 0
            for reason, count in reason_counts.items()
        )
        or sum(reason_counts.values()) != override_total
    ):
        return failed("HUMAN_REVIEW_INCOMPLETE", "Override reason counts are unavailable.")
    safety_overrides = sorted(
        reason for reason in P0_HUMAN_REVIEW_REASON_CODES if reason_counts.get(reason, 0) > 0
    )
    if safety_overrides:
        return failed(
            "HUMAN_REVIEW_SAFETY_OVERRIDE",
            "Safety-critical human overrides block promotion: " + ", ".join(safety_overrides),
        )
    if float(override_rate) > MAX_HUMAN_REVIEW_OVERRIDE_RATE:
        return failed(
            "HUMAN_REVIEW_OVERRIDE_THRESHOLD",
            f"Weighted override rate {float(override_rate):.4f} exceeds "
            f"the {MAX_HUMAN_REVIEW_OVERRIDE_RATE:.0%} release limit.",
        )
    return {
        "passed": True,
        "code": None,
        "detail": "Recent candidate-scoped human review passed the release thresholds.",
        "evidence_id": evidence_id,
        "evidence_hash": evidence_hash,
        "target_prompt_version_id": target_prompt_version_id,
        "population_count": population_count,
        "selected_count": selected_count,
        "reviewed_count": reviewed_count,
        "weighted_override_rate": float(override_rate),
        "override_reason_counts": reason_counts,
        "thresholds": thresholds,
    }


def check_release_gate(
    evidence: EvaluationEvidence | None,
    *,
    human_review_evidence: Mapping[str, Any] | None = None,
    prompt_version_id: uuid.UUID | None = None,
    require_human_review: bool = True,
    platform_gates: "list[GateResult] | None" = None,
) -> dict[str, Any]:
    """Refuse promotion without clean evidence.

    Two distinct refusals, because the remedies differ:

    - no evidence at all -> run the evaluation (`EVALUATION_REQUIRED`);
    - P0 regression     -> fix the prompt (`P0_REGRESSION`).

    Non-P0 regressions are recorded but allowed: docs/development-plan.md
    gates on P0 specifically, and blocking every metric movement would make
    the gate unusable and tempt operators to disable it.

    `platform_gates`, when supplied, are supplemental release-wide checks
    (cross-tenant leakage, unauthorized writes, duplicate replies, and
    read-tool health). The prompt HTTP route does not fabricate those inputs;
    CI's `evaluation.release_check` remains responsible for the platform-wide
    decision. `require_human_review=False` is reserved for that pre-canary P0
    evaluation summary. The promotion service keeps review evidence required
    and tenant-scoped.
    """
    if evidence is None:
        raise ReleaseError(
            "EVALUATION_REQUIRED",
            "promotion requires an evaluation report; none was supplied",
        )
    if not evidence.scores:
        raise ReleaseError(
            "EVALUATION_REQUIRED",
            "evaluation report carries no category scores",
        )

    blocking = evidence.p0_regressions
    if blocking:
        names = ", ".join(sorted(r.category for r in blocking))
        raise ReleaseError("P0_REGRESSION", f"P0 categories regressed: {names}")

    human_review_assessment = _assess_human_review_gate(
        human_review_evidence,
        prompt_version_id=prompt_version_id,
    )
    if require_human_review and not human_review_assessment["passed"]:
        raise ReleaseError(
            str(human_review_assessment["code"]),
            str(human_review_assessment["detail"]),
        )

    if platform_gates is not None:
        failed = [g for g in platform_gates if not g.passed]
        if failed:
            summary = "; ".join(f"{g.gate}={g.observed} (needs {g.threshold})" for g in failed)
            raise ReleaseError("PLATFORM_GATE_FAILED", summary)
    return human_review_assessment


async def promote(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    version_id: uuid.UUID,
    evidence: EvaluationEvidence | None,
    human_review_evidence: Mapping[str, Any] | None = None,
    platform_gates: "list[GateResult] | None" = None,
) -> PromptTemplate:
    """Make a version active, gated on evaluation evidence.

    Exactly one version per (tenant, template_name) is active afterwards:
    the previously active version is archived in the same transaction, so a
    reader can never observe two actives.

    `platform_gates` is forwarded to `check_release_gate`; see there for why
    the two halves of the decision belong in one call.
    """
    row = await _load(session, ctx=ctx, version_id=version_id)
    incumbent = await get_active(session, tenant_id=ctx.tenant_id, template_name=row.template_name)
    if incumbent is not None and incumbent.id == row.id:
        raise ReleaseError("ALREADY_ACTIVE", "this version is already active")

    current_review_evidence = await verified_review_evidence_for_prompt(
        session,
        tenant_id=ctx.tenant_id,
        prompt_version_id=row.id,
    )
    if (
        human_review_evidence is None
        or current_review_evidence is None
        or human_review_evidence.get("evidence_id") != current_review_evidence.get("evidence_id")
        or human_review_evidence.get("evidence_hash")
        != current_review_evidence.get("evidence_hash")
    ):
        current_review_evidence = None
    human_review_gate_assessment = check_release_gate(
        evidence,
        human_review_evidence=current_review_evidence,
        prompt_version_id=row.id,
        platform_gates=platform_gates,
    )
    assert evidence is not None  # narrowed by check_release_gate
    if not human_review_gate_assessment["passed"]:
        raise ReleaseError(
            str(human_review_gate_assessment["code"]),
            str(human_review_gate_assessment["detail"]),
        )

    if incumbent is not None:
        await _archive(session, ctx=ctx, row=incumbent, reason="superseded_by_release")

    row.published = True
    await session.flush()

    await audit_service.record(
        session,
        ctx=ctx,
        action="prompt.promoted",
        resource_type="prompt_version",
        resource_id=row.id,
        before={"active_version": incumbent.version if incumbent else None},
        after={
            "template_name": row.template_name,
            "version": row.version,
            "evidence": evidence.p0_score_summary(),
            "human_review_evidence": (
                {
                    "evidence_id": current_review_evidence.get("evidence_id"),
                    "evidence_hash": current_review_evidence.get("evidence_hash"),
                    "target_prompt_version_id": current_review_evidence.get(
                        "target_prompt_version_id"
                    ),
                    "population_count": current_review_evidence.get("summary", {}).get(
                        "population_count"
                    ),
                    "selected_count": current_review_evidence.get("summary", {}).get(
                        "selected_count"
                    ),
                    "weighted_override_rate": current_review_evidence.get("summary", {}).get(
                        "weighted_override_rate"
                    ),
                }
                if current_review_evidence is not None
                and isinstance(current_review_evidence.get("summary"), dict)
                else None
            ),
            "human_review_gate_assessment": human_review_gate_assessment,
        },
        metadata={"human_review_gate_assessment": human_review_gate_assessment},
    )
    return row


async def reject(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    version_id: uuid.UUID,
    reason: str,
) -> PromptTemplate:
    """Withdraw a candidate. Never serves traffic."""
    row = await _load(session, ctx=ctx, version_id=version_id)
    if row.published:
        raise ReleaseError("ALREADY_ACTIVE", "an active version must be rolled back, not rejected")
    if not reason.strip():
        raise ReleaseError("REASON_REQUIRED", "a rejection must state why")

    await audit_service.record(
        session,
        ctx=ctx,
        action="prompt.rejected",
        resource_type="prompt_version",
        resource_id=row.id,
        decision="denied",
        after={"template_name": row.template_name, "version": row.version, "reason": reason},
    )
    return row


async def rollback(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    template_name: str,
    to_version_id: uuid.UUID,
    reason: str,
) -> PromptTemplate:
    """Restore an archived version.

    Rollback deliberately does NOT re-run the release gate. It is the action
    taken when a live version is already causing harm, so requiring a fresh
    evaluation would add latency exactly when latency is most damaging. The
    target must previously have been active, which is what makes skipping
    the gate defensible.
    """
    if not reason.strip():
        raise ReleaseError("REASON_REQUIRED", "a rollback must state why")

    target = await _load(session, ctx=ctx, version_id=to_version_id)
    if target.template_name != template_name:
        raise ReleaseError(
            "TEMPLATE_MISMATCH",
            "rollback target belongs to a different template",
        )

    current = await get_active(session, tenant_id=ctx.tenant_id, template_name=template_name)
    if current is None:
        raise ReleaseError("NO_ACTIVE_VERSION", "nothing is active to roll back from")
    if current.id == target.id:
        raise ReleaseError("ALREADY_ACTIVE", "the target is already the active version")

    await _archive(session, ctx=ctx, row=current, reason=f"rollback: {reason}")
    target.published = True
    await session.flush()

    await audit_service.record(
        session,
        ctx=ctx,
        action="prompt.rolled_back",
        resource_type="prompt_version",
        resource_id=target.id,
        before={"active_version": current.version},
        after={"template_name": template_name, "version": target.version, "reason": reason},
    )
    return target


# --- Internals -------------------------------------------------------------


async def _load(
    session: AsyncSession, *, ctx: TenantContext, version_id: uuid.UUID
) -> PromptTemplate:
    stmt = select(PromptTemplate).where(
        PromptTemplate.id == version_id,
        PromptTemplate.tenant_id == ctx.tenant_id,
    )
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        # Same error whether the row is absent or belongs to another tenant:
        # distinguishing them would confirm a guessed id exists.
        raise ReleaseError("NOT_FOUND", "no such prompt version for this tenant")
    return row


async def _archive(
    session: AsyncSession, *, ctx: TenantContext, row: PromptTemplate, reason: str
) -> None:
    row.published = False
    await session.flush()
    await audit_service.record(
        session,
        ctx=ctx,
        action="prompt.archived",
        resource_type="prompt_version",
        resource_id=row.id,
        after={"version": row.version, "reason": reason},
    )


async def archive_stale_duplicates(
    session: AsyncSession, *, tenant_id: uuid.UUID, template_name: str
) -> int:
    """Enforce the single-active invariant, repairing existing data.

    `promote` maintains this going forward, but rows written before the
    release process existed may have several `published` versions. Keeps the
    highest version and unpublishes the rest, returning how many it fixed.
    """
    stmt = (
        select(PromptTemplate)
        .where(
            PromptTemplate.tenant_id == tenant_id,
            PromptTemplate.template_name == template_name,
            PromptTemplate.published.is_(True),
        )
        .order_by(PromptTemplate.version.desc())
    )
    actives = list((await session.execute(stmt)).scalars().all())
    if len(actives) <= 1:
        return 0

    stale_ids = [row.id for row in actives[1:]]
    await session.execute(
        update(PromptTemplate).where(PromptTemplate.id.in_(stale_ids)).values(published=False)
    )
    return len(stale_ids)


__all__ = [
    "P0_CATEGORIES",
    "CategoryScore",
    "EvaluationEvidence",
    "PromptStatus",
    "Regression",
    "ReleaseError",
    "archive_stale_duplicates",
    "check_release_gate",
    "create_draft",
    "get_active",
    "list_versions",
    "promote",
    "reject",
    "rollback",
    "submit_candidate",
]
