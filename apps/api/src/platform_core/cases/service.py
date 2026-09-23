"""Case command service: the only way Cases change state (ticket 21).

Every command: validates transition + optimistic version, accrues SLA
time, recomputes deadlines, and writes an audit event in the SAME
transaction. The outbox enqueue rides along, so integrations observe
case.events without any extra writes from the caller.
"""

import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.cases.models import (
    DEFAULT_SLA,
    Case,
    CaseConversation,
    CaseEscalation,
    CaseStatus,
    check_transition,
    check_version,
    sla_deadline,
)
from platform_core.cases.sla_service import resolve_sla_policy


class CaseError(Exception):
    """A Case command that must be refused with a caller-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


async def cases_for_conversation(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_ref_id: uuid.UUID
) -> list[uuid.UUID]:
    """Cases linked to this conversation. A reader, so callers outside `cases`
    do not have to import `CaseConversation` (AGENTS.md: no cross-module model
    imports).

    More than one is possible and not an error - a conversation can spawn a
    second case (an escalation, a separate request), and the reply path has to
    satisfy the first-response clock on every case it answers.
    """
    return list(
        (
            await session.execute(
                select(CaseConversation.case_id).where(
                    CaseConversation.tenant_id == tenant_id,
                    CaseConversation.conversation_ref_id == conversation_ref_id,
                )
            )
        )
        .scalars()
        .all()
    )


class CaseService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_case(
        self,
        *,
        tenant_id: uuid.UUID,
        subject: str,
        description: str = "",
        priority: str = "p2",
        category: str = "general",
        actor_id: uuid.UUID | None = None,
        enterprise_account_id: uuid.UUID | None = None,
        conversation_ref_id: uuid.UUID | None = None,
    ) -> Case:
        """Open a Case, deriving its SLA clocks from the account's contract.

        `enterprise_account_id` was a column nothing could write before this:
        the API did not accept it and this method never set it, so a Case could
        not be attached to the account it was about. The FK added in migration
        0028 makes a cross-tenant account unrepresentable, and because RLS
        cannot see the other tenant's row, an unknown id and a foreign id both
        report `ACCOUNT_NOT_FOUND` - so this cannot be used to discover which
        accounts exist elsewhere.

        `conversation_ref_id` is the same defect one table over, and the reason
        it is fixed here rather than in a linking endpoint: `CaseConversation`
        had a reader (`inbox_consumer.case_conversation_ref`) and **no writer
        anywhere**, so the join it feeds could only ever be empty. That join is
        what priority claiming filters on, which means
        `worker.priority_claim_enabled` silently did nothing when it was on -
        the flag changed no behaviour and reported no error. Passing the origin
        conversation at creation is the only moment the platform reliably
        knows the link.
        """
        now = int(time.time())

        tier: str | None = None
        contract_status: str | None = None
        if enterprise_account_id is not None:
            # Imported here rather than at module import: `cases` depends on one
            # narrow identity read, and a module-level import of the identity
            # package would make the dependency look like a cycle to anyone
            # reading the graph.
            from platform_core.identity.org import account_sla_facts

            facts = await account_sla_facts(
                self._session, tenant_id=tenant_id, account_id=enterprise_account_id
            )
            if facts is None:
                raise CaseError("ACCOUNT_NOT_FOUND", str(enterprise_account_id))
            tier, contract_status = facts

        # Configured targets if the tenant set any, else the code default -
        # `resolve_sla_policy` delegates to `sla_policy_for_tier` when there is
        # no row, so a tenant with no configuration is unaffected.
        policy = await resolve_sla_policy(
            self._session,
            tenant_id=tenant_id,
            tier=tier,
            contract_status=contract_status,
        )

        case = Case(
            tenant_id=tenant_id,
            subject=subject,
            description=description,
            priority=priority,
            category=category,
            status=CaseStatus.NEW.value,
            version=1,
            opened_at=now,
            last_state_changed_at=now,
            enterprise_account_id=enterprise_account_id,
            # Snapshotted, not resolved later: the deadline is recomputed on a
            # priority change, and re-reading the account then would let a
            # mid-Case contract change move a clock that is already running.
            sla_tier=tier,
        )
        self._session.add(case)
        await self._session.flush()
        case.first_response_due_at = sla_deadline(
            policy,
            priority=priority,
            opened_at=now,
            elapsed_running_seconds=0,
            first_response=True,
        )
        case.resolution_due_at = sla_deadline(
            policy,
            priority=priority,
            opened_at=now,
            elapsed_running_seconds=0,
            first_response=False,
        )
        if conversation_ref_id is not None:
            # `origin` rather than `follow_up`: this conversation is where the
            # case came from. A later link (a customer reopening the subject in
            # a new thread) is a different relationship and a different call.
            self._session.add(
                CaseConversation(
                    tenant_id=tenant_id,
                    case_id=case.id,
                    conversation_ref_id=conversation_ref_id,
                    relationship="origin",
                )
            )
            await self._session.flush()
        return case

    async def apply_command(
        self,
        *,
        tenant_id: uuid.UUID,
        case_id: uuid.UUID,
        command: str,
        expected_version: int | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> Case:
        """Dispatch a case command: transition / change_priority / assign.

        Raises TransitionNotAllowed / VersionConflict; the caller rolls
        back and returns CASE_TRANSITION_NOT_ALLOWED / 409.
        """
        row = (
            await self._session.execute(
                select(Case)
                .where(Case.tenant_id == tenant_id, Case.id == case_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise LookupError("case not found")

        check_version(row.version, expected_version)
        now = int(time.time())
        params = parameters or {}

        if command == "transition":
            target = CaseStatus(params["target"])
            check_transition(CaseStatus(row.status), target)
            self._accrue_sla_time(row, now)
            row.status = target.value
            if target == CaseStatus.RESOLVED:
                row.resolved_at = now
            if target == CaseStatus.CLOSED:
                row.closed_at = now
        elif command == "change_priority":
            row.priority = params["priority"]
            # The tier recorded at open, not the account's current one. A
            # priority change is a decision about this Case; a contract edit
            # that happened in between is not, and letting it through here
            # would mean the same Case had a different contractual window
            # depending on when the deadline happened to be recomputed.
            policy = await resolve_sla_policy(self._session, tenant_id=tenant_id, tier=row.sla_tier)
            row.first_response_due_at = sla_deadline(
                policy,
                priority=row.priority,
                opened_at=row.opened_at,
                elapsed_running_seconds=row.elapsed_running_seconds,
                first_response=True,
            )
            row.resolution_due_at = sla_deadline(
                policy,
                priority=row.priority,
                opened_at=row.opened_at,
                elapsed_running_seconds=row.elapsed_running_seconds,
                first_response=False,
            )
        elif command == "assign":
            row.assignee_ref = params.get("assignee_ref")
            row.team_ref = params.get("team_ref")
        elif command == "record_first_response":
            if row.first_responded_at is None:
                row.first_responded_at = now
        else:
            raise ValueError(f"unknown case command: {command}")

        row.version += 1
        row.last_state_changed_at = now
        return row

    @staticmethod
    def _accrue_sla_time(case: Case, now: int) -> None:
        """Add time since the last state change if the clock was running."""
        status = CaseStatus(case.status)
        if status in DEFAULT_SLA.running_states and case.last_state_changed_at:
            case.elapsed_running_seconds += max(now - case.last_state_changed_at, 0)


# How many recent cases in the tenant are scored for relatedness. The score is
# computed in Python (see `_term_set`), so this bounds the work per call;
# the alternative - scoring in SQL - cannot express the CJK case at all.
#
# The consequence, stated because it is real: a related case older than the
# window is not found. Raising this is the knob until the term extraction is
# expressible in the database (a `pg_bigm`-style index, or a materialised term
# column), which is a schema change and therefore its own decision.
RELATED_WINDOW = 500

# Bound on how many rows the workbench will assemble. The panel is a reference
# strip, not a search result page.
RELATED_CASE_LIMIT = 20

# How many rows whose only signal is the category may take slots on the panel.
# A bucket match is the weaker signal - the two subjects share no wording - so
# it gets a couple of slots, never the whole strip. Without the cap a case in
# the dominant bucket (`general` by default) showed a full strip of rows that
# shared nothing but a label, which is the defect this function exists to
# remove, merely renamed.
RELATED_CATEGORY_ONLY_MAX = 2

# Function words only: they carry no subject matter in any business, so they
# are listed. **Content words are deliberately NOT listed here** - which words
# are generic depends on the tenant ("order" is in nearly every subject for a
# component distributor, and rare for a payroll team), and a hand-maintained
# list of them would be wrong for the second customer and stale for the first.
# That job is done by `_ubiquitous_terms`, measured from the tenant's own
# subjects.
RELATED_STOP_TERMS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "this",
        "that",
        "how",
        "what",
        "when",
        "的",
        "了",
        "吗",
        "呢",
        "是",
        "在",
        "我",
        "你",
        "请",
        "问",
    }
)

# Document-frequency cut. A term that appears in more than half of the tenant's
# case subjects describes the *business*, not the case, and cannot distinguish
# two of them - so it stops counting as shared evidence for a related pair.
# Measured rather than listed (see above), and only applied once the sample is
# large enough for the frequency to mean anything: below `RELATED_DF_MIN_SAMPLE`
# subjects, one repeated word is not evidence of anything.
RELATED_UBIQUITY = 0.5
RELATED_DF_MIN_SAMPLE = 30


@dataclass(frozen=True)
class RelatedCase:
    """A case in the same tenant that looks like another one.

    `match` says which signal put it here, and `shared_terms` says what the two
    subjects actually have in common. A panel whose rows cannot answer "why is
    this in front of me" gets ignored, so the evidence travels with the row
    instead of being asserted once in a heading.
    """

    case_id: uuid.UUID
    subject: str
    status: str
    category: str
    opened_at: int
    # "subject" when the two subjects share wording, "category" when only the
    # bucket is shared.
    match: str
    # Dice coefficient over the two term sets, 0..1. Ordering only - it is not
    # a gate, because a gate would have to be language-tuned (see below).
    score: float
    # The terms the two subjects share, most useful first, capped for display.
    shared_terms: tuple[str, ...]


def _term_set(text: str) -> set[str]:
    """The meaningful terms in a case subject. Language-agnostic by design.

    **Why not `pg_trgm`.** Measured on this database: `similarity()` returns
    **0.000** for every Chinese pair tried, including "能不能加急" vs
    "加急打样多久" - two subjects a person would call the same question. The
    reason is structural rather than a tuning problem: pg_trgm works on
    three-character windows, so the shared term 加急 appears as "能加急" in one
    subject and "加急打" in the other and never matches. Trigram similarity is
    a working signal for Latin text (0.613 for "Short circuit claim" vs "Short
    circuit claim on batch 42", 0.222 for "... vs Earlier claim") and a blind
    one for Chinese, which is the language this product actually runs in. A
    trigram gate would have produced a permanently empty panel for the pilot
    and read as "no related cases exist".

    So the unit is a term:

    - Latin runs of three or more characters, lowercased;
    - CJK **bigrams** - the shortest unit that carries meaning in Chinese, and
      the one that still matches when a two-character term sits inside longer
      words.
    """
    found: set[str] = set()
    lowered = text.lower()
    for word in re.findall(r"[a-z0-9]{3,}", lowered):
        if word not in RELATED_STOP_TERMS:
            found.add(word)
    for run in re.findall(r"[㐀-䶿一-鿿豈-﫿]+", lowered):
        for index in range(len(run) - 1):
            bigram = run[index : index + 2]
            if bigram not in RELATED_STOP_TERMS:
                found.add(bigram)
    return found


def _ubiquitous_terms(term_sets: list[set[str]]) -> frozenset[str]:
    """Terms too common in this tenant's subjects to identify any of them.

    Document frequency over the candidate window, not a hand-written list: the
    words that carry no information differ per tenant and per language, and a
    list maintained by hand is wrong for the next customer.

    Returns an empty set below `RELATED_DF_MIN_SAMPLE` subjects. With five
    cases, "appears in three of them" is noise, and a rule that fires on noise
    is worse than one that stays quiet.
    """
    if len(term_sets) < RELATED_DF_MIN_SAMPLE:
        return frozenset()
    counts: dict[str, int] = {}
    for terms in term_sets:
        for term in terms:
            counts[term] = counts.get(term, 0) + 1
    ceiling = RELATED_UBIQUITY * len(term_sets)
    return frozenset(term for term, seen in counts.items() if seen > ceiling)


def _compare(
    case_terms: set[str], other_terms: set[str], ubiquitous: frozenset[str]
) -> tuple[tuple[str, ...], float]:
    """Terms the two subjects share, plus their Dice coefficient.

    The terms are returned, not just the count, because the caller shows them:
    "these two share 加急" answers "why is this row in front of me", and a bare
    score does not. Capped at three - a row listing every shared bigram is
    unreadable. Longer terms sort first, since a shared phrase says more about
    a subject than a shared particle does.
    """
    mine = case_terms - ubiquitous
    theirs = other_terms - ubiquitous
    shared = mine & theirs
    ordered = tuple(sorted(shared, key=lambda term: (-len(term), term))[:3])
    total = len(mine) + len(theirs)
    return ordered, (round(2 * len(shared) / total, 3) if total else 0.0)


async def find_related_cases(
    session: AsyncSession, *, tenant_id: uuid.UUID, case: Case, limit: int = 5
) -> list[RelatedCase]:
    """Cases that look like `case`, ranked, within one tenant.

    Deterministic - no model, no embedding, nothing to build or backfill.

    **Eligibility** is one of two signals, and every row says which:

    1. the subjects share a term (`match: "subject"`, `shared_terms` lists them);
    2. the category matches (`match: "category"`).

    Category alone was the whole query before, and it was the defect: `category`
    defaults to `general`, so for a `general` case it returned the N most recent
    cases in the tenant - unrelated tickets whose presence implied a relevance
    nothing had computed. Category still selects rows, because a same-bucket
    case with different wording ("Short circuit claim" / "过孔烧毁") is worth
    showing, but it is ranked below every wording match and **labelled**, so
    the panel never presents a bucket peer as a topical one.

    **Order** is wording matches first, then score, then recency, then category
    match. Deliberately not "category first": a bucket is not evidence of
    relevance, and this tenant's dominant bucket is the default one.

    `tenant_id` is filtered **explicitly** as well as by RLS. RLS alone is
    sufficient (and is what the negative test exercises); the explicit
    predicate mirrors `export_cases`, because leaning on the implicit scope is
    what produced the cross-tenant read in
    `FINDINGS-2026-09-21-CARD-AND-RLS.md` §1 - there the scope silently became
    another tenant instead of an error.
    """
    limit = max(1, min(int(limit), RELATED_CASE_LIMIT))
    window = (
        await session.execute(
            select(Case)
            .where(Case.tenant_id == tenant_id, Case.id != case.id)
            .order_by(Case.opened_at.desc(), Case.id)
            .limit(RELATED_WINDOW)
        )
    ).scalars()
    candidates = list(window)

    case_terms = _term_set(case.subject)
    other_terms = {other.id: _term_set(other.subject) for other in candidates}
    ubiquitous = _ubiquitous_terms(list(other_terms.values()) + [case_terms])

    scored: list[tuple[int, float, int, RelatedCase]] = []
    for other in candidates:
        shared, dice = _compare(case_terms, other_terms[other.id], ubiquitous)
        same_category = other.category == case.category
        if not shared and not same_category:
            continue
        scored.append(
            (
                # Wording matches first, then the coefficient, then freshness.
                0 if shared else 1,
                -dice,
                -int(other.opened_at or 0),
                RelatedCase(
                    case_id=other.id,
                    subject=other.subject,
                    status=other.status,
                    category=other.category,
                    opened_at=int(other.opened_at or 0),
                    match="subject" if shared else "category",
                    score=dice,
                    shared_terms=shared,
                ),
            )
        )

    scored.sort(key=lambda row: row[:3])
    ordered = [row[3] for row in scored]
    # A bucket match is the weaker signal, so it gets a couple of slots rather
    # than the panel. Without this, a case in the dominant bucket (`general` by
    # default) would show a full strip of rows that share nothing but a label -
    # which is the defect this whole function exists to remove, merely renamed.
    strong = [item for item in ordered if item.match == "subject"]
    weak = [item for item in ordered if item.match != "subject"]
    return (strong + weak[:RELATED_CATEGORY_ONLY_MAX])[:limit]


async def export_cases(
    session: AsyncSession, *, tenant_id: uuid.UUID, since: int, until: int, limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """A bounded extract of this tenant's Cases and their escalation ledger.

    Returns `(cases, escalations, truncated)`. Two lists rather than nested
    objects: a compliance extract is read by a person or a spreadsheet, and a
    flat ledger keyed by `case_id` is easier to check than a tree.

    Opening the export window on `opened_at` rather than on the last update is
    deliberate. "Show me everything from Q3" means Cases that *arose* in Q3; a
    window on `last_state_changed_at` would hide a Case opened in Q2 and
    resolved in Q3, which is exactly the one an auditor is looking for.
    """
    rows = (
        await session.execute(
            select(Case)
            .where(
                Case.tenant_id == tenant_id,
                Case.opened_at >= since,
                Case.opened_at <= until,
            )
            .order_by(Case.opened_at, Case.id)
            .limit(limit + 1)
        )
    ).scalars()
    cases = list(rows)
    truncated = len(cases) > limit
    cases = cases[:limit]

    case_ids = [c.id for c in cases]
    escalations: list[dict[str, Any]] = []
    if case_ids:
        escalation_rows = (
            await session.execute(
                select(CaseEscalation)
                .where(
                    CaseEscalation.tenant_id == tenant_id,
                    CaseEscalation.case_id.in_(case_ids),
                )
                .order_by(CaseEscalation.escalated_at, CaseEscalation.id)
            )
        ).scalars()
        escalations = [
            {
                "case_id": str(e.case_id),
                "clock": e.clock,
                "level": int(e.level),
                "reason_code": e.reason_code,
                "breach_seconds": int(e.breach_seconds),
                "routed_to": e.team_ref,
                "escalated_at": e.escalated_at,
            }
            for e in escalation_rows
        ]

    return (
        [
            {
                "case_id": str(c.id),
                "subject": c.subject,
                "description": c.description,
                "category": c.category,
                "priority": c.priority,
                "status": c.status,
                "enterprise_account_id": (
                    str(c.enterprise_account_id) if c.enterprise_account_id else None
                ),
                "sla_tier": c.sla_tier,
                "assignee_ref": c.assignee_ref,
                "team_ref": c.team_ref,
                "opened_at": c.opened_at,
                "first_response_due_at": c.first_response_due_at,
                "resolution_due_at": c.resolution_due_at,
                "first_responded_at": c.first_responded_at,
                "resolved_at": c.resolved_at,
                "closed_at": c.closed_at,
            }
            for c in cases
        ],
        escalations,
        truncated,
    )
