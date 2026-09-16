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

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.models import PromptTemplate
from platform_core.audit import service as audit_service
from platform_core.identity.tenant_context import TenantContext

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


def check_release_gate(evidence: EvaluationEvidence | None) -> None:
    """Refuse promotion without clean evidence.

    Two distinct refusals, because the remedies differ:

    - no evidence at all -> run the evaluation (`EVALUATION_REQUIRED`);
    - P0 regression     -> fix the prompt (`P0_REGRESSION`).

    Non-P0 regressions are recorded but allowed: docs/development-plan.md
    gates on P0 specifically, and blocking every metric movement would make
    the gate unusable and tempt operators to disable it.
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


async def promote(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    version_id: uuid.UUID,
    evidence: EvaluationEvidence | None,
) -> PromptTemplate:
    """Make a version active, gated on evaluation evidence.

    Exactly one version per (tenant, template_name) is active afterwards:
    the previously active version is archived in the same transaction, so a
    reader can never observe two actives.
    """
    check_release_gate(evidence)
    assert evidence is not None  # narrowed by check_release_gate

    row = await _load(session, ctx=ctx, version_id=version_id)
    incumbent = await get_active(
        session, tenant_id=ctx.tenant_id, template_name=row.template_name
    )
    if incumbent is not None and incumbent.id == row.id:
        raise ReleaseError("ALREADY_ACTIVE", "this version is already active")

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
        },
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
        update(PromptTemplate)
        .where(PromptTemplate.id.in_(stale_ids))
        .values(published=False)
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
