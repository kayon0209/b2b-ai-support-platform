"""Agent runtime orchestrator (tickets 17-18, docs/agent.md).

Assembles the documented request pipeline into one deterministic flow:

  resolve lease -> minimize -> classify -> route -> retrieve authorized
  evidence -> generate draft -> validate citations -> RECHECK LEASE
  -> send through channel adapter or persist web turn -> record audit/metrics

Design rules:
- Deterministic code owns every gate. The model proposes; this module
  disposes. No LLM output reaches a customer without passing validation.
- The pre-send lease re-check is the safety gate for the human/AI race:
  a human takeover between generation and dispatch must abort the send.
- Persistence happens inside one transaction per unit of work, and the
  outbox carries side effects (transactional outbox pattern).
"""

import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from observability import JsonLogger, TraceContext, new_trace_context
from observability_metrics import get_metrics
from platform_core.agent_runtime.complaint import is_complaint_claim
from platform_core.agent_runtime.confirmation import is_confirmation
from platform_core.agent_runtime.conversation import (
    CompactedContext,
    ConversationMemory,
    Turn,
    needs_clarification,
    normalize_colloquial,
    rewrite_query,
)
from platform_core.agent_runtime.emotion import detect_emotion
from platform_core.agent_runtime.generator import MAX_EXCERPT_CHARS, LlmAnswerGenerator
from platform_core.agent_runtime.handoff import hand_off_to_human_queue
from platform_core.agent_runtime.hours import is_open, offline_notice
from platform_core.agent_runtime.intent import (
    NON_ANSWERABLE_ROUTES,
    PRE_RETRIEVAL_ROUTES,
    IntentAction,
    IntentDetection,
    Route,
    Scene,
    classify,
)
from platform_core.agent_runtime.models import (
    MODE_CUSTOMER_REPLY,
    MODE_INTERNAL_DRAFT,
    AgentRun,
    Citation,
    RunStatus,
)
from platform_core.agent_runtime.models import (
    PromptTemplate as PromptVersionRow,
)
from platform_core.agent_runtime.qa_path import (
    ABSTAIN_CLARIFICATION,
    ABSTAIN_COMPLAINT_REQUIRES_HUMAN,
    ABSTAIN_CONFLICT,
    ABSTAIN_EMOTION_ESCALATION,
    ABSTAIN_HUMAN_REQUIRED,
    ABSTAIN_OUT_OF_SCOPE,
    ABSTAIN_SENSITIVE_REQUEST,
    ABSTAIN_STRATEGIC_ACCOUNT_REQUIRES_HUMAN,
    SYSTEM_OUTAGE_REASONS,
    UNVERIFIED_READ_REASONS,
    AbstentionDecision,
    DraftAnswer,
    claim_contradiction_candidates,
    decide_abstention,
    excerpt_hash,
    redline_violations,
    safe_abstention_text,
    system_outage_notice,
    unverified_read_notice,
    validate_citations,
)
from platform_core.agent_runtime.queue_status import queue_notice, queue_status
from platform_core.agent_runtime.routing import routing_note, team_for
from platform_core.agent_runtime.tool_card import glossary_for
from platform_core.audit import service as audit_service
from platform_core.identity import lease_service
from platform_core.identity.control_lease import LeaseConflict
from platform_core.identity.org import ContactAccountFacts
from platform_core.identity.tenant_context import TenantContext

# Flags are read through the service, never by touching its tables: the
# rollout maths, the tenant-target override and the kill switch all live there,
# and a second reading of the same rows would drift from it.
from platform_core.knowledge import flag_service
from platform_core.llm.provider import ModelError
from platform_core.outbox_service import enqueue
from platform_core.retrieval.hybrid import (
    Embedder,
    MetadataFilter,
    PrincipalScope,
    RetrievedChunk,
    build_metadata_filter,
)
from platform_core.retrieval.reranker import Reranker

logger = JsonLogger("platform.agent_runtime")

CODE_VERSION = "0.1.0"
POLICY_VERSION = "v1"

# The feature flag that decides whether the answer path reranks.
#
# `Reranker` existed, was tested, and was used in exactly one place: the
# retrieval diagnostics endpoint. The production answer path never reranked -
# the same "built but never wired" shape this repository keeps finding. Wiring
# it behind a flag rather than unconditionally is the point of a canary: the
# reranker changes which evidence a customer-facing answer is built from, so
# its blast radius is every answer, and rolling it out per tenant is how you
# find that out before it is every tenant's answers.
#
# Default False, so an undefined flag means "keep the fused order".
RERANK_FLAG_KEY = "agent.rerank_enabled"


@dataclass
class RunOutcome:
    """Result of one orchestrated run, safe to log and to assert on."""

    run_id: uuid.UUID
    status: RunStatus
    route: str
    answer_text: str = ""
    abstain_reason: str = ""
    handoff: bool = False
    citation_count: int = 0
    send_blocked_reason: str = ""
    latency_ms: int = 0
    trace_id: str = ""


@dataclass
class OrchestratorDeps:
    """Injected collaborators. Keeps the orchestrator testable without a
    live provider or Redis."""

    embedder: Embedder | None = None
    generator: LlmAnswerGenerator | None = None
    # Delivers an answer back over the channel it arrived on (ADR 0014). `None`
    # means every channel is receive-only, which is a deployment state and is
    # recorded as one rather than being reported as a successful delivery.
    channel_sender: object | None = None  # ChannelSender-compatible send_message
    # Applied only when `RERANK_FLAG_KEY` resolves true for the tenant, so the
    # rollout is a per-tenant decision rather than a deployment-wide switch.
    reranker: Reranker | None = None
    # How much evidence a run builds its answer from. Was a literal `8` at the
    # call site, which meant it could not be tuned per tenant or per scene
    # without a code change — and "Top-K 怎么确定" is a question you can only
    # answer by measuring, which needs it to be configurable.
    top_k: int = 8
    # Adapter factories for the tool paths, injected for the same reason the
    # generator is: without it the branch that *executes* a low-risk write can
    # only be exercised against a live external system, so in practice it
    # would be exercised by nothing. `None` means the shipped adapters.
    tool_factories: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


async def retrieve_evidence(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    query: str,
    principal: PrincipalScope,
    embedder: Embedder | None,
    top_k: int = 8,
    trace: TraceContext | None = None,
    reranker: Reranker | None = None,
    metadata_filter: Any | None = None,
    enabled_paths: tuple[str, ...] | None = None,
    rerank_cap: int | None = None,
) -> list[RetrievedChunk]:
    """Authorized retrieval. Evidence never crosses tenants: the tenant
    filter and ACL narrowing are applied inside hybrid_search before any
    candidate is scored.

    `reranker` is applied **after** fusion and **after** the ACL pre-filter,
    which is the only safe order: reranking reorders what survived
    authorization, it never decides what is visible. The reranker is passed in
    rather than built here so a caller can canary it per tenant, and so tests
    can supply one without a provider. `rerank_cap` bounds how many candidates
    are reranked (plan 1.7): rerank cost is per candidate, and candidates past
    the second page of results almost never reach the model anyway.

    Degradation is the reranker's own contract (`RerankOutcome.degraded`), and
    it is surfaced rather than swallowed: docs/architecture.md permits falling
    back to the fused order only when evaluation allows it, so the fact that
    the primary path did not run has to be observable.
    """
    from platform_core.config import get_settings
    from platform_core.retrieval.hybrid import hybrid_search

    settings = get_settings()
    started = time.monotonic()
    span = trace.span("retrieval", **{"span.kind": "internal"}) if trace else None
    try:
        chunks = await hybrid_search(
            session,
            tenant_id=tenant_id,
            query=query,
            top_k=max(top_k, rerank_cap or 0),
            principal=principal,
            embedder=embedder,
            fts_candidates=settings.retrieval_fts_candidates,
            vector_candidates=settings.retrieval_vector_candidates,
            trigram_candidates=settings.retrieval_trigram_candidates,
            alias_candidates=settings.retrieval_alias_candidates,
            metadata_filter=metadata_filter,
            enabled_paths=enabled_paths,
            rrf_k=settings.retrieval_rrf_k,
        )
        if reranker is not None and len(chunks) > 1:
            cap = min(rerank_cap or len(chunks), len(chunks))
            outcome = await reranker.rerank(query, chunks[:cap], top_k=top_k)
            if outcome.degraded:
                get_metrics().retrieval_degraded_total.labels(
                    reason_code=outcome.reason_code or "RERANK_DEGRADED"
                ).inc()
            else:
                chunks = outcome.chunks
            if span is not None:
                span.set_attributes(**{"retrieval.rerank_degraded": outcome.degraded})
    except Exception as exc:
        # Retrieval failure is measured as well as logged: "how often is
        # retrieval unavailable" is an operational fact that must not have
        # to be reconstructed from error logs.
        if span is not None:
            span.record_exception(exc)
        raise
    else:
        elapsed = time.monotonic() - started
        get_metrics().observe_retrieval(latency_seconds=elapsed, candidate_count=len(chunks))
        if span is not None:
            span.set_attributes(
                latency_ms=int(elapsed * 1000), **{"retrieval.candidates": len(chunks)}
            )
        return chunks
    finally:
        if span is not None:
            span.end()


async def reranker_for_tenant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    reranker: Reranker | None,
) -> tuple[Reranker | None, str]:
    """Decide whether this tenant's runs should rerank.

    Returns `(reranker_or_None, reason)` - the reason is the flag decision's
    own reason code (`UNKNOWN_FLAG`, `DISABLED`, `TENANT_TARGET`, `ROLLOUT`,
    `NOT_IN_ROLLOUT`), which is what makes a rollout auditable after the fact:
    "why did tenant X rerank yesterday" has an answer that is not a guess.

    Extracted from the pipeline so the gate is testable without standing up a
    run: the interesting behaviour is the decision, not the pipeline.

    **Rollout scope, stated because it is not obvious.** Flags are tenant-owned
    and RLS-scoped, and this evaluates inside the running tenant's own session,
    so the flag a run sees is *that tenant's own*. Combined with
    `flag_service.target_tenant` refusing self-targeting, the levers available
    are the kill switch and the rollout percentage - a tenant can roll the
    reranker out to itself, but the platform cannot canary it across tenants on
    their behalf, because under RLS the platform's flag is invisible here.
    Making that expressible needs a platform-owned flag read through a narrow
    SECURITY DEFINER resolver (the same pattern the tenant bootstrap uses),
    which is a decision worth making deliberately rather than inferring from
    this function.
    """
    if reranker is None:
        # No provider configured. Not a flag outcome - the capability is
        # absent, and calling the flag would imply it was a rollout decision.
        return None, "NO_RERANKER"
    decision = await flag_service.evaluate(
        session,
        flag_key=RERANK_FLAG_KEY,
        tenant_id=tenant_id,
        default=False,
    )
    return (reranker if decision.enabled else None), decision.reason


async def _get_or_create_prompt(
    session: AsyncSession, tenant_id: uuid.UUID, gen: object
) -> uuid.UUID:
    """Resolve the immutable prompt version row, creating it on first use.

    Prompts are immutable after publication: a template change must arrive
    as a new version, which is why this looks up (name, version) exactly.
    """
    template = gen.template  # type: ignore[attr-defined]
    stmt = (
        select(PromptVersionRow)
        .where(
            PromptVersionRow.tenant_id == tenant_id,
            PromptVersionRow.template_name == template.name,
            PromptVersionRow.version == template.version,
        )
        .limit(1)
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing.id
    row = PromptVersionRow(
        tenant_id=tenant_id,
        template_name=template.name,
        version=template.version,
        body=template.body,
        published=True,
    )
    session.add(row)
    await session.flush()
    return row.id


async def _persist_citations(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    draft: DraftAnswer,
    evidence: list[RetrievedChunk],
) -> int:
    """Persist one Citation row per claim, resolving the model's chunk ids
    back to the real evidence. Only evidence-backed claims are stored."""
    by_id = {c.chunk_id: c for c in evidence}
    written = 0
    for claim_index, chunk_ids in sorted(draft.claims.items()):
        for chunk_id in chunk_ids:
            chunk = by_id.get(chunk_id)
            if chunk is None:
                continue
            session.add(
                Citation(
                    tenant_id=tenant_id,
                    agent_run_id=run_id,
                    # Tool receipts (plan 3.4) cite source_uri, not a document.
                    document_version_id=(
                        chunk.document_version_id
                        if not chunk.source_uri.startswith("tool://")
                        else None
                    ),
                    chunk_id=chunk.chunk_id,
                    excerpt_hash=excerpt_hash(chunk.excerpt),
                    source_uri=chunk.source_uri,
                    claim_index=claim_index,
                    retrieval_score=float(chunk.score),
                )
            )
            written += 1
            break  # one citation row per claim (uq_citation_claim)
    await session.flush()
    return written


def _extract_tool_args(tool_name: str, question: str) -> dict[str, str] | None:
    """Deterministic argument extraction for read tools.

    The required identifier is the FIRST entity-shaped token in the question
    ("order 88123" -> 88123; "case #12345" -> 12345). Entities are protected
    from stemming (plan 1.4), so the token that would retrieve is the token
    that identifies. No entity, no argument - and the caller hands off rather
    than guessing an id.
    """
    from platform_core.agent_runtime.conversation import _entity_terms

    entities = _entity_terms(question)
    if not entities:
        return None
    if tool_name == "case.read":
        return {"case_ref": entities[0]}
    if tool_name == "order.get_status":
        return {"order_id": entities[0]}
    if tool_name == "shipment.track":
        return {"shipment_id": entities[0]}
    if tool_name == "billing.get_invoice":
        return {"invoice_id": entities[0]}
    if tool_name == "inventory.check_stock":
        return {"part_number": entities[0]}
    return None


def _extract_write_args(
    tool_name: str, question: str, defaults: dict[str, str]
) -> dict[str, str] | None:
    """Deterministic argument extraction for write tools.

    A write tool's arguments split in two halves, and only one of them is in
    the conversation. The customer supplies the subject — the sentence they
    just wrote. The rest (which Jira project, which Slack channel) is the
    tenant's configuration, resolved by the caller from the connector and
    passed in as `defaults`.

    No LLM is involved for the same reason it is not involved in tool
    selection: this decides what gets written to an external system, and a
    non-deterministic chooser there would make the audit trail describe a
    coin flip. Both halves must be present — a proposal missing either is one
    a human has to rewrite from scratch, so handing off is the honest result.

    Returns None when the tool has no deterministic extraction at all. That is
    a real answer, not a gap: `crm.update_account` takes a free-form field
    patch, which cannot be derived from an utterance without a model, so a
    customer asking for one is handed to a person rather than guessed at.
    """
    subject = " ".join(question.split())
    if not subject:
        return None
    if tool_name == "jira.create_issue":
        # `summary` is the schema's required free-text field; the project
        # arrives from configuration. Deliberately no `description`: the
        # customer's raw message is not copied into a third-party system
        # without a human choosing to do so.
        return {**defaults, "summary": subject[:200]}
    if tool_name == "linear.create_issue":
        return {**defaults, "title": subject[:200]}
    if tool_name == "im.send_notification":
        return {**defaults, "text": subject[:500]}
    return None


def _clarify_streak(history: list[Turn]) -> int:
    """Trailing consecutive clarification notices by the agent.

    Counted from the loaded history: every AGENT turn whose ref marks it as
    a clarification resets only when some other agent turn (an answer, a
    handoff notice) follows it. This is what makes "asked twice already"
    knowable on the third run.
    """
    streak = 0
    for turn in reversed(history):
        if turn.role.value == "agent" and turn.ref.startswith("clarify"):
            streak += 1
            continue
        if turn.role.value == "agent":
            break
    return streak


_MODEL_MENTION = re.compile(r"\bmodel\s+([a-z0-9][a-z0-9-]{2,31})", re.IGNORECASE)
_FIRMWARE_MENTION = re.compile(r"\bfirmware\s+v?([a-z0-9][a-z0-9.-]{1,31})", re.IGNORECASE)


async def _load_aliases(
    session: AsyncSession, tenant_id: uuid.UUID
) -> list[tuple[str, str, float]]:
    """Tenant alias rows for query expansion, best-effort.

    A load failure degrades to "no aliases" — expansion is an enrichment,
    and refusing to answer because the alias table is unreadable inverts the
    priority between a feature and the product.
    """
    from platform_core.retrieval.hybrid import load_aliases

    try:
        return await load_aliases(session, tenant_id)
    except Exception as exc:  # noqa: BLE001 - enrichment, not a dependency
        logger.warning("alias_load_failed", error_code=type(exc).__name__)
        return []


def _non_answerable_reason(
    route: str, restricted_query: bool, emotion_reason: str | None = None
) -> str:
    """The abstention reason for a route that never reaches retrieval.

    Distinguishing the reasons is what makes the handoff explainable
    afterwards. They are all "do not answer" but they are not the same event:
    a customer asking for a person is a routing request being honoured, while a
    sensitive request is a disclosure being refused, and an operator reading
    the audit log needs to tell them apart.

    An emotion escalation wins over the route-derived reason (7.1 trigger 7):
    "handed off because the customer threatened legal action" is a more
    specific answer to "why is this in the queue?" than "handed off because a
    human was required", and it is the one that decides who picks it up next.
    """
    if emotion_reason:
        return emotion_reason
    if restricted_query or route == Route.SENSITIVE.value:
        return ABSTAIN_SENSITIVE_REQUEST
    if route == Route.HUMAN_REQUIRED.value:
        return ABSTAIN_HUMAN_REQUIRED
    if route == Route.OUT_OF_SCOPE.value:
        return ABSTAIN_OUT_OF_SCOPE
    return ABSTAIN_HUMAN_REQUIRED


CLARIFICATION_KEEP_LEASE = "clarification"

# The contract tiers whose complaints go to the account team rather than the
# general queue (research report 难点 5). Plain strings rather than the
# `AccountTier` enum because AGENTS.md forbids importing another module's
# models, and `org.account_facts_for_contact` already projects to strings.
#
# `contract_status` is checked alongside: a churned or suspended contract no
# longer buys the dedicated route, which is the same rule
# `sla_policy_for_tier` applies to the SLA clock - one contract attribute
# should not mean two different things in two places.
PRIORITY_TIERS = frozenset({"strategic", "enterprise"})
ACTIVE_CONTRACT = "active"


class AgentOrchestrator:
    """One run = one customer question through the documented pipeline."""

    def __init__(
        self,
        session: AsyncSession,
        deps: OrchestratorDeps,
        *,
        code_version: str = CODE_VERSION,
        policy_version: str = POLICY_VERSION,
    ) -> None:
        self._session = session
        self._deps = deps
        self._code_version = code_version
        self._policy_version = policy_version
        self._top_k = deps.top_k
        self._target_team: str | None = None
        self._attachment_types: list[str] = []
        # Earlier conversation turns, for the customer-visible notices: a
        # notice's language must follow the conversation, not the last message
        # (a bare `SO-9001` carries no script). Set per run in `_run_pipeline`;
        # see `language.conversation_is_chinese` for the measured failure.
        self._history_texts: tuple[str, ...] = ()
        # Which channel this run answers on (ADR 0014), stored rather than
        # threaded for the same reason `_target_team` is: there is one value per
        # run, and *three* places dispatch - the answer, the abstention notice
        # and the clarification. Threading it means one of the three is missed,
        # and the one that is missed is the notice: the most common outcome in
        # the system is "we could not answer", so a missed notice is the
        # customer waiting for a reply that never arrives.
        self._channel_system: str | None = None
        self._channel_address: str | None = None
        # Read from the run's own `model_config` at adoption. Defaulted here so
        # an orchestrator driven directly (tests, probes) behaves as before.
        self._mode: str = MODE_CUSTOMER_REPLY
        # The A/B arms this run is in, and the generator they imply. Both are
        # per-run state, resolved once at the start of the pipeline: looking
        # them up again mid-run could disagree with what the run recorded if an
        # experiment were edited between the two reads.
        self._experiments: dict[str, Any] = {}
        self._generator_override: object | None = None
        # Set per run in `_run_pipeline`; defaulted so a handoff reached
        # without a classification cannot raise on the way out.
        self._business_line_note: str | None = None

    async def run(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_ref_id: uuid.UUID,
        question: str,
        principal: PrincipalScope,
        trace: TraceContext | None = None,
        channel_conversation_key: str | None = None,
        expected_lease_version: int | None = None,
        restricted_query: bool = False,
        history: list[Turn] | None = None,
        context_budget_chars: int | None = None,
        known_facts: list[tuple[str, str]] | None = None,
        contact_id: str | None = None,
        # Content types the customer attached (1.3) - types only, never URLs or
        # content. Used so a handoff can say evidence was already supplied.
        attachment_types: list[str] | None = None,
        # Feature 2.5. Three states, and the difference matters:
        #   None          - not a visitor run (operator surfaces): no gate,
        #                   because the caller is authorised by policy.
        #   ""            - an anonymous visitor: the read tools must not run at
        #                   all, or the surface serves *somebody's* order data.
        #   "acme"        - a visitor who proved ownership of that account: the
        #                   tools run, and each receipt is checked against it.
        verified_account: str | None = None,
        # ADR 0014. Which channel the question arrived on, and where to answer.
        # `None` means a conversation the platform itself carries (`/support`, an
        # operator run), for which `_dispatch`'s existing branches are correct.
        # A value means an email or WeChat conversation that has no Chatwoot
        # account behind it and must be delivered by its own transport.
        channel_system: str | None = None,
        # The destination: the customer's address for email, the openid for
        # WeChat. It arrives as the event's `contact_id`.
        channel_address: str | None = None,
    ) -> RunOutcome:
        """Execute the pipeline for one inbound customer message.

        `expected_lease_version` is the version observed when the run was
        queued. When supplied, the pre-send gate refuses to dispatch if the
        lease has moved — this is what prevents an AI reply racing a human
        takeover.

        `history` is the prior turns of this conversation, oldest first. It is
        what makes the run multi-turn: the question is rewritten against it
        (anaphora resolution) and compressed context from it is placed in front
        of the model. Omitting it reproduces the previous single-turn
        behaviour, which is what the evaluation harness and the unit tests
        rely on — so this is additive, not a behaviour change.
        """
        started = time.monotonic()
        ctx = trace or new_trace_context()
        self._channel_system = channel_system
        self._channel_address = channel_address
        # One root span per run. Everything the pipeline does hangs off it, so
        # a trace shows the whole journey rather than a scatter of spans that
        # have to be stitched together by timestamp.
        run_span = ctx.span("agent_run", **{"span.kind": "server"})
        try:
            outcome = await self._run_pipeline(
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                question=question,
                principal=principal,
                ctx=ctx,
                started=started,
                run_span=run_span,
                channel_conversation_key=channel_conversation_key,
                expected_lease_version=expected_lease_version,
                restricted_query=restricted_query,
                history=history,
                context_budget_chars=context_budget_chars,
                known_facts=known_facts,
                contact_id=contact_id,
                attachment_types=attachment_types,
                verified_account=verified_account,
                channel_system=channel_system,
                channel_address=channel_address,
            )
        except Exception as exc:
            # An unexpected failure still has to be visible in metrics and in
            # the trace: an unmeasured crash is indistinguishable from no
            # traffic at all.
            elapsed = time.monotonic() - started
            run_span.record_exception(exc)
            get_metrics().observe_run(
                outcome="failed", route="knowledge_qa", latency_seconds=elapsed
            )
            raise
        else:
            run_span.set_attributes(status=outcome.status.value)
            if outcome.status is RunStatus.ABSTAINED:
                run_span.set_status("ok", outcome.abstain_reason)
            run_span.end()
            return outcome

    async def _account_facts(
        self, *, tenant_id: uuid.UUID, contact_id: str
    ) -> "ContactAccountFacts | None":
        """The account this contact is bound to, or None when unbound.

        Goes through `identity.org` rather than querying the table: AGENTS.md
        forbids importing another module's ORM models, and this is the same
        narrow-projection seam `cases` already uses for `account_sla_facts`.

        A lookup failure degrades to "unbound" on purpose. The tier only
        sharpens a handoff that happens anyway, so an unreadable binding must
        never fail the run - the customer would lose the handoff entirely over
        an enrichment.
        """
        from platform_core.identity import org

        try:
            return await org.account_facts_for_contact(
                self._session, tenant_id=tenant_id, external_contact_id=contact_id
            )
        except Exception as exc:  # noqa: BLE001 - enrichment, not a dependency
            logger.warning("account_lookup_failed", error_code=type(exc).__name__)
            return None

    async def _run_pipeline(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_ref_id: uuid.UUID,
        question: str,
        principal: PrincipalScope,
        ctx: TraceContext,
        started: float,
        run_span: Any,
        channel_conversation_key: str | None,
        expected_lease_version: int | None,
        restricted_query: bool,
        history: list[Turn] | None,
        context_budget_chars: int | None,
        known_facts: list[tuple[str, str]] | None,
        contact_id: str | None,
        attachment_types: list[str] | None,
        # Feature 2.5 ownership gate - threaded from run() (see that parameter's
        # docstring for the three states).
        verified_account: str | None = None,
        # ADR 0014 - threaded from run(); see there for the two states.
        channel_system: str | None = None,
        channel_address: str | None = None,
    ) -> RunOutcome:

        # --- 1. Acquire/observe the control lease. ---
        lease = await lease_service.acquire_or_get(
            self._session, tenant_id=tenant_id, conversation_ref_id=conversation_ref_id
        )
        if expected_lease_version is None:
            expected_lease_version = int(lease.lease_version)

        # --- 1b. Classify intent on both axes (scene + kind). ---
        #
        # Runs before retrieval because for three of the seven routing classes
        # there is nothing to retrieve: a request for a human, a sensitive
        # request and an out-of-scope turn are settled by *who asked*, not by
        # what the corpus says. Classifying first is what stops the platform
        # from spending an embedding and a model call to produce an answer it
        # would then have to suppress.
        detection = classify(question)
        route = detection.route.value
        # Which team this conversation belongs to (7.3), decided once where
        # the scene is known and carried on the handoff. Stored on the run's
        # orchestrator rather than threaded through every call site: there is
        # one detection per run and six places that can hand off.
        #
        # The business line refines the scene when a deployment configures a
        # (scene, line) team; unfilled, this is exactly `team_for_scene`.
        self._target_team = team_for(detection.scene, detection.business_line)
        # A PCB complaint and a complaint are different jobs for whoever reads
        # the queue, so the line travels with the handoff even when it does not
        # change the destination.
        self._business_line_note = routing_note(detection.scene, detection.business_line)
        # One place, set from the caller: the minimiser already decided what is
        # safe to keep, so this only carries it.
        self._attachment_types = list(attachment_types or [])
        # Conversation texts for the notice language, set once here because
        # `_finish_abstain` and `_finish_clarify` are reached from many call
        # sites and threading a parameter through all of them is how one gets
        # missed; every one of them is downstream of this line.
        self._history_texts = tuple(t.text for t in (history or []) if t.text)
        # Feature 2.5 ownership gate - three states, see the parameter.
        self._verified_account = verified_account

        # --- 1b-2. Emotion (7.2), which closes 7.1's seventh handoff trigger. ---
        #
        # Checked here, alongside the other pre-retrieval routes, so an
        # escalation costs no embedding and no model call: a customer who
        # threatened legal action is not waiting for a knowledge answer, and
        # spending one to produce it is how an angry conversation gets a
        # confident, unhelpful reply. Only ANGRY and above escalate - see
        # `EmotionSignal.should_handoff` for why impatience does not.
        self._emotion = detect_emotion(question)
        emotion_reason: str | None = None
        emotion_context: str | None = None
        if self._emotion.should_handoff:
            route = Route.HUMAN_REQUIRED.value
            emotion_reason = ABSTAIN_EMOTION_ESCALATION
            # The matched terms travel with the handoff. A queue entry that
            # says "escalation risk" without saying which words triggered it
            # leaves the agent to re-read the whole conversation to find out.
            emotion_context = (
                f"emotion={self._emotion.level.value} terms={','.join(self._emotion.terms)}"
            )
        run_span.set_attributes(
            route=route,
            intent_scene=detection.scene.value,
            intent_kind=detection.primary_kind.value,
            intent_confidence=round(detection.confidence, 3),
            intent_multi=detection.multi_intent,
            # 3.2/7.3: the line is what a queue triages on; recording it on the
            # span is what makes "which line generates the complaints" answerable
            # without re-classifying history.
            intent_business_line=detection.business_line.value,
            **{"lease.version": expected_lease_version},
        )

        # --- 1c. Multi-turn context. ---
        #
        # Compressed before retrieval, because retrieval needs the rewritten
        # query (step 3) and the rewrite needs the same history. Building the
        # memory once here means the context the model sees and the query the
        # index sees are derived from the same turns — two views that cannot
        # disagree.
        memory = ConversationMemory(list(history or ()))
        context = memory.snapshot(question=question)
        if known_facts:
            # Cross-conversation facts fill the gaps; a fact stated in THIS
            # conversation always wins (it is the more recent statement, and
            # "latest wins" is the whole conflict policy).
            from dataclasses import replace as _dc_replace

            from platform_core.agent_runtime.conversation import DurableFact

            merged: dict[str, str] = dict(known_facts)
            for fact in context.durable_facts:
                merged[fact.key] = fact.value
            context = _dc_replace(
                context,
                durable_facts=tuple(
                    DurableFact(key=k, value=v, source_turn=-1, ts=0)
                    for k, v in sorted(merged.items())
                ),
            )
        get_metrics().context_turns_kept.observe(len(context.recent))
        get_metrics().context_turns_summarized.observe(context.dropped_turns)
        get_metrics().context_pinned.observe(len(context.pinned))
        retrieval_query, rewritten = rewrite_query(question, memory.turns)

        # --- 1d. Retrieval shape (plan 1.3/1.4/1.5/1.7). ---
        # Everything here is config- or flag-driven and defaults to the
        # pre-iteration behaviour: no aliases applied, no metadata filter,
        # no score floor, no per-scene top-k.
        settings = self._settings()
        top_k = self._top_k_for_scene(detection.scene)
        enabled_paths: tuple[str, ...] | None = (
            tuple(p.strip() for p in settings.retrieval_enabled_paths.split(",") if p.strip())
            or None
        )
        aliases = await _load_aliases(self._session, tenant_id)
        normalization_reason = "DISABLED"
        if aliases and await self._flag_enabled(settings.flag_query_normalization, tenant_id):
            retrieval_query, applied = normalize_colloquial(retrieval_query, aliases)
            normalization_reason = "APPLIED" if applied else "NO_MATCH"
        run_span.set_attributes(**{"flag.query_normalization": normalization_reason})

        metadata_filter = None
        metadata_reason = "OFF"
        if await self._flag_enabled(settings.flag_metadata_filter, tenant_id):
            metadata_filter = self._metadata_filter_for(question)
            metadata_reason = "APPLIED" if metadata_filter is not None else "NO_ENTITY"
        run_span.set_attributes(**{"flag.metadata_filter": metadata_reason})
        if rewritten:
            run_span.set_attributes(**{"retrieval.query_rewritten": True})
        run_span.set_attributes(
            **{
                "context.turns_kept": len(context.recent),
                "context.turns_summarized": context.dropped_turns,
                "context.pinned": len(context.pinned),
                "context.topic_shift": context.topic_shift,
            }
        )

        # Resolved before adoption so the arms land in `model_config` in the
        # same write as the rest of the lineage, rather than being patched in
        # afterwards - a run whose recorded arms disagreed with the prompt it
        # actually used would make the results unreadable.
        from platform_core.evaluation.ab_service import (
            assign_experiments,
            resolve_prompt_override,
        )

        self._experiments = await assign_experiments(
            self._session, tenant_id=tenant_id, conversation_ref_id=conversation_ref_id
        )
        if self._experiments and self._deps.generator is not None:
            # Sorted so two experiments that both name a prompt resolve the same
            # way on every run; otherwise which arm wins depends on dict order.
            for _key in sorted(self._experiments):
                _template = await resolve_prompt_override(
                    self._session, tenant_id=tenant_id, arm=self._experiments[_key]
                )
                if _template is not None:
                    self._generator_override = self._deps.generator.with_template(_template)
                    break

        run = await self._adopt_or_create_run(
            tenant_id=tenant_id,
            conversation_ref_id=conversation_ref_id,
            route=route,
            ctx=ctx,
            context=context,
            detection=detection,
            rewritten=rewritten,
            retrieval_query=retrieval_query,
            question=question,
        )

        # The mode lives on the run, so it is read from the run rather than
        # threaded through every call site - and an execution path that forgets
        # to pass it cannot silently ignore it. See the shadow check below.
        self._mode = str((run.model_config or {}).get("mode") or MODE_CUSTOMER_REPLY)

        # --- 1b-3. Is the AI still the one who may speak? ---
        #
        # Placed after the run is adopted (so the run row exists to carry the
        # outcome) and before retrieval and the model call, which is the whole
        # point: a conversation parked in the handoff queue can never receive an
        # AI reply, because the pre-send lease gate refuses one. Reaching that
        # gate is still the authoritative check and still runs - but a run that
        # gets there has already paid for a retrieval and a generation to
        # produce a draft that is then thrown away, while the customer is told
        # nothing. Measured 2026-09-23: after a handoff into the queue, every
        # following question in that conversation did exactly that,
        # indefinitely. `lease_service.current_owner` records the same finding.
        #
        # **The queue specifically, not "anything that is not the AI."** A
        # specific agent's takeover (`owner_type == "human"`, `transfer_to_human`)
        # is left to the pre-send gate, which keeps the draft for that agent -
        # see `_finish_answer`'s LeaseConflict branch and
        # `test_human_takeover_mid_generation_blocks_outbound_send`. Skipping
        # generation for a conversation someone is actively working would throw
        # away the draft they were about to use; skipping it for one parked in
        # the queue loses nothing, because no one is there to read it.
        #
        # The customer is deliberately not told from here, and cannot be: the
        # same lease that stops the answer stops any notice this path could
        # send. The surface that accepts the question is what says so - see
        # `support_router._handed_off_notice`.
        #
        # `run` is bound above (the guard used to sit before adoption, which
        # crashed with UnboundLocalError the moment it fired - caught by
        # `test_orchestrator_lease_race.py`).
        if str(lease.owner_type) == "queue":
            run.route = route
            # SUPERSEDED, not HANDED_OFF. This run was accepted, never
            # executed, and the conversation is parked in a queue that no human
            # has claimed. `handed_off` asserts a person is on it, and every
            # list, replay and metric that reads the status then reports an
            # unanswered question as answered-by-a-colleague. Where the lease
            # moved while this run sat in the queue,
            # `chat_service.supersede_queued_runs` has usually already set
            # this status; arriving here means the run was claimed before the
            # move and is only now reaching the guard.
            run.status = RunStatus.SUPERSEDED.value
            run.latency_ms = int((time.monotonic() - started) * 1000)
            await self._session.flush()
            logger.info(
                "run_skipped_not_owner",
                ctx,
                reason_code=f"owner is {lease.owner_type}",
            )
            get_metrics().observe_run(
                outcome="superseded",
                route=route,
                latency_seconds=run.latency_ms / 1000.0,
                citation_count=0,
            )
            return RunOutcome(
                run_id=run.id,
                status=RunStatus.SUPERSEDED,
                route=route,
                answer_text="",
                send_blocked_reason=f"AI_NOT_OWNER: owner is {lease.owner_type}",
                citation_count=0,
                latency_ms=run.latency_ms,
                trace_id=ctx.trace_id,
            )

        # --- 2. Restricted and non-knowledge routes never reach the model. ---
        if restricted_query or route in PRE_RETRIEVAL_ROUTES:
            return await self._finish_abstain(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                decision=AbstentionDecision(
                    abstain=True,
                    reason_code=_non_answerable_reason(route, restricted_query, emotion_reason),
                    handoff=True,
                ),
                handoff_context=emotion_context,
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
            )

        # --- 2b. Clarification, before spending retrieval. ---
        #
        # Underspecified is not the same as unanswerable: the customer is
        # present and one detail unblocks the answer, so the right move is to
        # ask rather than to hand off. Decided before retrieval because a
        # question with no recoverable subject retrieves noise, and noise looks
        # like evidence.
        # --- 2b-pre. A confirmation the conversation owes. ---
        #
        # The one message whose meaning depends on what the conversation is
        # waiting for rather than on its own words. "确认" carries no verb and no
        # object, so it never classifies as a write request, and the QA path
        # would either answer it from the corpus or ask a pointless
        # clarification - both wrong for a customer who has just answered the
        # question the platform asked. Checked before the clarification gate for
        # that reason.
        #
        # What the agent does **not** do is record it. `case.eq_confirm` is
        # `human_approval`, the class the policy engine keeps deliberately
        # unreachable by the agent at every stage including propose, and the
        # research report is explicit that the AI relays and collects while the
        # release is a human decision: the case status is what the factory
        # reads, so recording a confirmation is one step from releasing
        # production. So the run hands off, with a reason that says what the
        # human is being asked to do - which is the AI's actual job in this
        # flow, not a failure to act.
        #
        # Gated on the write flag so a tenant with the EQ flow off sees exactly
        # the behaviour it saw before this existed.
        if await self._flag_enabled(self._settings().flag_business_write_tools, tenant_id):
            if (
                is_confirmation(question)
                and await self._pending_eq_confirmation(
                    tenant_id=tenant_id, conversation_ref_id=conversation_ref_id
                )
                is not None
            ):
                run_span.set_attributes(**{"route.override": "pending_eq_confirmation"})
                return await self._finish_abstain(
                    run=run,
                    tenant_id=tenant_id,
                    conversation_ref_id=conversation_ref_id,
                    expected_lease_version=expected_lease_version,
                    decision=AbstentionDecision(
                        abstain=True,
                        reason_code="EQ_CONFIRMATION_REQUIRES_HUMAN",
                        handoff=True,
                    ),
                    ctx=ctx,
                    started=started,
                    question=question,
                    channel_conversation_key=channel_conversation_key,
                )

        # --- 2b-complaint. A claim against the company (L6 争议归责). ---
        #
        # A customer demanding compensation, a refund, a return or an
        # escalation is not asking what the policy says - they are claiming
        # under it. The research report puts that at L6 (必须转人工) and forbids
        # the AI from any 归责表态 or 赔付承诺, so there is no answer for the QA
        # path to produce: the run hands off before retrieval, before the write
        # path and before the clarification gate.
        #
        # Before the clarification gate specifically because asking someone who
        # has just claimed compensation to "give me a little more detail" is
        # not information gathering, it is making them repeat a grievance.
        #
        # Deliberately **not** behind a feature flag, unlike the EQ branch
        # above. That branch adds a behaviour a tenant must opt into; this one
        # removes an answer the report classes as a red line, and gating a red
        # line behind a default-off flag is what left the complaint path
        # answering in the first place.
        if is_complaint_claim(question):
            reason_code = ABSTAIN_COMPLAINT_REQUIRES_HUMAN
            account_label = None
            if contact_id:
                facts = await self._account_facts(tenant_id=tenant_id, contact_id=contact_id)
                if facts is not None:
                    run_span.set_attributes(**{"account.tier": facts.tier})
                    if facts.tier in PRIORITY_TIERS and facts.contract_status == ACTIVE_CONTRACT:
                        # 难点 5: a key account's complaint goes to the people
                        # who own that relationship. The handoff is the same
                        # act; what changes is who is told and how the queue
                        # prioritises it, which is what tier is *for*.
                        reason_code = ABSTAIN_STRATEGIC_ACCOUNT_REQUIRES_HUMAN
                        account_label = f"account_id={facts.account_id} tier={facts.tier}"
            run_span.set_attributes(**{"route.override": "complaint_requires_human"})
            return await self._finish_abstain(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                decision=AbstentionDecision(
                    abstain=True,
                    reason_code=reason_code,
                    handoff=True,
                ),
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
                handoff_context=account_label,
            )

        needs_ask, ask_reason = needs_clarification(retrieval_query, memory.turns)
        if needs_ask and route not in NON_ANSWERABLE_ROUTES:
            if _clarify_streak(history or []) >= self._settings().clarification_max_streak:
                # Consecutive clarifications without progress: another ask is
                # a loop, not a conversation. Hand off instead (plan 2.8) -
                # the customer talks to a human who can untangle what two
                # rounds of "could you say more" could not.
                return await self._finish_abstain(
                    run=run,
                    tenant_id=tenant_id,
                    conversation_ref_id=conversation_ref_id,
                    expected_lease_version=expected_lease_version,
                    decision=AbstentionDecision(
                        abstain=True, reason_code="CLARIFICATION_LIMIT", handoff=True
                    ),
                    ctx=ctx,
                    started=started,
                    question=question,
                    channel_conversation_key=channel_conversation_key,
                )
            return await self._finish_clarify(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                reason_code=ask_reason,
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
            )

        # --- 2c. Business read tools (plan 3.2/3.4; flag off by default). ---
        #
        # Live data (order status, shipment tracking, invoices, case state)
        # is decided BEFORE the knowledge path: if a read tool answers it,
        # retrieval never runs, so a stale indexed copy cannot leak into the
        # answer. The attempt returns either customer-safe evidence (tool
        # receipts) or a finished RunOutcome (handoff) - never a guess.
        tool_evidence: list[RetrievedChunk] = []
        if route == Route.BUSINESS_READ.value:
            if await self._flag_enabled(settings.flag_business_read_tools, tenant_id):
                read_result = await self._attempt_business_read(
                    run=run,
                    tenant_id=tenant_id,
                    question=question,
                    detection=detection,
                    ctx=ctx,
                    started=started,
                    conversation_ref_id=conversation_ref_id,
                    expected_lease_version=expected_lease_version,
                    channel_conversation_key=channel_conversation_key,
                    history=history,
                )
                if isinstance(read_result, RunOutcome):
                    return read_result
                tool_evidence = read_result
            else:
                # A tenant that asks for live data and cannot serve it falls
                # through to the corpus, which answers "where is my order" out
                # of indexed prose or abstains. Both look identical to the
                # customer and to the metrics, and the two call for opposite
                # fixes - so the gate says which one happened. Silently
                # degrading here is how a disabled flag gets diagnosed as a
                # retrieval quality problem.
                #
                # The fields are the ones the log allowlist keeps: an earlier
                # version of this line passed `flag=` and the value was dropped
                # silently, which is the same class of problem one level down.
                logger.info(
                    "business_read_flag_off",
                    ctx,
                    route=route,
                    reason_code="BUSINESS_READ_FLAG_OFF",
                    tenant_id=str(tenant_id),
                )

        # --- 2d. Business write tools (plan 3.5; flag off by default). ---
        #
        # Decided before retrieval for the same reason a read is: nothing in
        # the corpus can answer "did the platform do this", and prose about
        # refunds must never be mistaken for a refund having been issued.
        #
        # What this branch may do is bounded by the tool's own risk class. It
        # can propose anything the AI's role permits, and it can execute a
        # `low_write` tool - the class the catalog defines as needing no
        # confirmation. It can never approve: a tool that requires
        # confirmation stops at the proposal and the run hands off, so a
        # person owns the outcome.
        if route == Route.BUSINESS_WRITE.value and await self._flag_enabled(
            settings.flag_business_write_tools, tenant_id
        ):
            if detection.action is IntentAction.CLARIFY:
                # `intent.py` degrades a write request below its confidence
                # threshold to CLARIFY, on the grounds that a write proposed
                # on a weak signal is one the customer never asked for.
                # Honour that rather than acting on it - and hand off once
                # asking has been tried, because a second identical question
                # is a loop, not a clarification (the limit step 2b applies).
                if _clarify_streak(history or []) >= settings.clarification_max_streak:
                    return await self._finish_abstain(
                        run=run,
                        tenant_id=tenant_id,
                        conversation_ref_id=conversation_ref_id,
                        expected_lease_version=expected_lease_version,
                        decision=AbstentionDecision(
                            abstain=True, reason_code="CLARIFICATION_LIMIT", handoff=True
                        ),
                        ctx=ctx,
                        started=started,
                        question=question,
                        channel_conversation_key=channel_conversation_key,
                    )
                return await self._finish_clarify(
                    run=run,
                    tenant_id=tenant_id,
                    conversation_ref_id=conversation_ref_id,
                    expected_lease_version=expected_lease_version,
                    reason_code="WRITE_INTENT_UNCERTAIN",
                    ctx=ctx,
                    started=started,
                    question=question,
                    channel_conversation_key=channel_conversation_key,
                )
            write_result = await self._attempt_business_write(
                run=run,
                tenant_id=tenant_id,
                question=question,
                detection=detection,
                ctx=ctx,
                started=started,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                channel_conversation_key=channel_conversation_key,
            )
            if isinstance(write_result, RunOutcome):
                return write_result
            tool_evidence = write_result

        # --- 3. Retrieve authorized evidence. ---
        # The reranker is gated per tenant. Evaluating the flag inside the
        # same transaction as everything else means a rollout change takes
        # effect on the next run rather than on the next deploy.
        reranker, rerank_reason = await reranker_for_tenant(
            self._session, tenant_id=tenant_id, reranker=self._deps.reranker
        )
        run_span.set_attributes(**{"flag.rerank": rerank_reason})
        filtered_empty = False
        score_floor_applied = False
        if tool_evidence:
            # Tool receipts replace retrieval for this run: the abstention
            # gate's relevance/conflict heuristics are calibrated for document
            # excerpts and would misfire on a receipt, which is grounded by
            # construction (the gateway verified it).
            evidence = tool_evidence
            run.retrieval_config = {**(run.retrieval_config or {}), "evidence_source": "tool"}
        else:
            try:
                evidence = await retrieve_evidence(
                    self._session,
                    tenant_id=tenant_id,
                    # The rewritten query, not the raw question: anaphora
                    # ("how about monthly?") has no subject for the index to
                    # match, and retrieval runs on the surface string for both
                    # the FTS and the vector leg. When nothing was rewritten
                    # this is identical to `question`.
                    query=retrieval_query,
                    principal=principal,
                    embedder=self._deps.embedder,
                    top_k=top_k,
                    trace=ctx,
                    reranker=reranker,
                    metadata_filter=metadata_filter,
                    enabled_paths=enabled_paths,
                    rerank_cap=settings.rerank_candidate_cap,
                )
                if metadata_filter is not None and not evidence:
                    # A filter that matches no metadata at all is
                    # indistinguishable from "wrong corpus". Fall back to the
                    # unfiltered search and record the degradation, so a
                    # metadata typo shows up as a reported narrowing loss
                    # instead of a confident abstention.
                    evidence = await retrieve_evidence(
                        self._session,
                        tenant_id=tenant_id,
                        query=retrieval_query,
                        principal=principal,
                        embedder=self._deps.embedder,
                        top_k=top_k,
                        trace=ctx,
                        reranker=reranker,
                        enabled_paths=enabled_paths,
                        rerank_cap=settings.rerank_candidate_cap,
                    )
                    filtered_empty = True
            except Exception as exc:  # noqa: BLE001 - degradation is a policy choice
                # Retrieval unavailable: never answer enterprise facts; hand off.
                logger.error("retrieval_failed", ctx, error_code=type(exc).__name__)
                return await self._finish_abstain(
                    run=run,
                    tenant_id=tenant_id,
                    conversation_ref_id=conversation_ref_id,
                    expected_lease_version=expected_lease_version,
                    decision=AbstentionDecision(
                        abstain=True, reason_code="RETRIEVAL_UNAVAILABLE", handoff=True
                    ),
                    ctx=ctx,
                    started=started,
                    question=question,
                    channel_conversation_key=channel_conversation_key,
                )

            # --- 3b. Relative score floor (plan 1.7, flag off by default). ---
            # Candidates below floor_ratio * top_score are not sent to the
            # model. The threshold is RELATIVE because RRF scores are tiny in
            # absolute terms - an absolute floor never fires, which is the
            # exact trap the conflict margin already fell into once. At least
            # one chunk always survives: the floor narrows evidence, it must
            # not empty it.
            if evidence and await self._flag_enabled(settings.flag_score_floor, tenant_id):
                top_score = max(chunk.score for chunk in evidence)
                kept = [
                    c
                    for c in evidence
                    if c.score >= settings.retrieval_score_floor_ratio * top_score
                ]
                if kept and len(kept) < len(evidence):
                    evidence = kept
                    score_floor_applied = True

        # Record the full retrieval lineage on the run (plan 1.7 acceptance:
        # every dial is auditable per run).
        if not tool_evidence:
            run.retrieval_config = {
                **(run.retrieval_config or {}),
                "top_k": top_k,
                "paths": list(enabled_paths or ("fts", "vector", "trigram", "alias")),
                "query_normalization": normalization_reason,
                "metadata_filter": metadata_filter.as_metadata() if metadata_filter else None,
                "metadata_filter_degraded": filtered_empty,
                "score_floor_applied": score_floor_applied,
            }

        # --- 4. Abstention gate before spending a model call. ---
        # Tool receipts bypass the gate: the receipt is the provider's own
        # verified answer, not a passage whose relevance needs judging.
        decision = (
            AbstentionDecision(abstain=False)
            if tool_evidence
            else decide_abstention(question, evidence, restricted_query=restricted_query)
        )
        if decision.abstain:
            return await self._finish_abstain(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                decision=decision,
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
            )

        # --- 5. Generate a draft. ---
        if self._deps.generator is None:
            return await self._finish_abstain(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                decision=AbstentionDecision(
                    abstain=True, reason_code="GENERATOR_UNAVAILABLE", handoff=True
                ),
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
            )
        # --- 4c. Evidence-first budget (plan 2.7). ---
        # Evidence and context share one prompt. When they contend, evidence
        # wins: a shrunken context degrades the answer gracefully, while
        # truncated evidence produces abstentions or hallucinations. The
        # reduction is recorded - an audit trail that says "context was
        # small" turns a quality regression into a diagnosable one.
        evidence_chars = sum(len(chunk.excerpt) for chunk in evidence)
        from platform_core.agent_runtime.generator import MAX_TOTAL_EVIDENCE_CHARS

        final_ctx_budget = max(
            settings.context_min_budget_chars,
            min(context.budget_chars, MAX_TOTAL_EVIDENCE_CHARS - evidence_chars),
        )
        if final_ctx_budget < context.budget_chars:
            context = memory.compact(budget_chars=final_ctx_budget)
            get_metrics().context_turns_kept.observe(len(context.recent))
            get_metrics().context_turns_summarized.observe(context.dropped_turns)
            run.model_config = {
                **(run.model_config or {}),
                "context_budget_reduced_for_evidence": True,
                "context_budget_chars": final_ctx_budget,
            }

        try:
            draft = await self._generate_with_telemetry(
                ctx, question, evidence, context=context, retrieval_query=retrieval_query
            )
            # Record the provider's token accounting on the run. The generator
            # boundary used to drop it, so `token_usage` was always `{}` even
            # though ChatResult carries it (and a malformed model response
            # still spends tokens, so this is set before validation).
            if draft.usage:
                run.token_usage = dict(draft.usage)
        except ModelError as exc:
            # Provider down: queue/handoff, never fabricate (availability table).
            logger.error("model_failed", ctx, error_code=exc.code)
            return await self._finish_abstain(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                decision=AbstentionDecision(
                    abstain=True, reason_code="MODEL_UNAVAILABLE", handoff=True
                ),
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
            )

        # --- 6. Validate citations. Unsupported output is not publishable. ---
        validation = validate_citations(draft, evidence)
        get_metrics().citation_validation_total.labels(
            status="supported" if validation.ok else "unsupported"
        ).inc()
        # Citation-supportability guard (plan 4.2, flag off): a claim whose
        # text negates a term its own cited excerpt affirms. Measured as a
        # metric for a long time on purpose - the rule has false positives,
        # and a false positive converts a right answer into an abstention.
        # Only flips to a guard when an operator turns the flag on per tenant.
        if (
            validation.ok
            and claim_contradiction_candidates(draft, evidence)
            and await self._flag_enabled(self._settings().flag_citation_guard, tenant_id)
        ):
            return await self._finish_abstain(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                decision=AbstentionDecision(
                    abstain=True, reason_code="UNSUPPORTED_CLAIM", handoff=True
                ),
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
            )
        # Red-line guard (华秋 research difficulty 4; feature list 6.1/6.2):
        # a draft that commits the company to a price, a delivery date, a
        # liability or a compensation amount is a commercial promise no one
        # authorised. Deterministic scan, so it is auditable.
        #
        # **No longer flag-gated.** It was, and that was the defect: a control
        # behind a default-off flag protects only the tenants that remember to
        # switch it on, and the requirement is a hard one - AI 不定价 is a risk
        # control, not an opt-in enrichment. The scan is deliberately narrow
        # (a commitment verb *and* a commercial object in the same sentence),
        # so it fires on "我们保证交期 7 天" and not on "交期以报价单为准".
        #
        # The detections are still counted as candidates by
        # `qa_path.redline_violations`, which only reports - this is what
        # blocks the send.
        if validation.ok and redline_violations(draft.text):
            return await self._finish_abstain(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                decision=AbstentionDecision(
                    abstain=True, reason_code="REDLINE_COMMERCIAL_COMMITMENT", handoff=True
                ),
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
            )
        if not validation.ok:
            # A validation failure on a **tool receipt** keeps the conversation.
            #
            # The receipt is the provider's own verified answer to the question
            # - that is why the abstention gate above is bypassed for it. So a
            # draft the validator rejects means the platform fetched the data
            # and the model failed to use it, which is our defect, not a reason
            # to take the conversation away from the customer and hand it to a
            # queue. Releasing the lease here is what made a successful order
            # lookup end the AI's involvement in the conversation.
            #
            # A validation failure on *document* evidence is unchanged: there
            # the answer may genuinely not be supported, and a person is the
            # right next step.
            return await self._finish_abstain(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                decision=AbstentionDecision(
                    abstain=True,
                    reason_code=validation.reason_code,
                    handoff=not tool_evidence,
                ),
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
            )

        # --- 7. Persist citations with the run still RUNNING. ---
        citation_count = await _persist_citations(
            self._session,
            tenant_id=tenant_id,
            run_id=run.id,
            draft=draft,
            evidence=evidence,
        )

        # --- 8. PRE-SEND LEASE RE-CHECK (safety-critical). ---
        try:
            await lease_service.assert_can_send(
                self._session,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_version=expected_lease_version,
            )
        except LeaseConflict as exc:
            # A human took over mid-generation. Drop the answer silently
            # from the customer's perspective; keep it for the agent.
            run.status = RunStatus.HANDED_OFF.value
            run.output_hash = None
            run.latency_ms = int((time.monotonic() - started) * 1000)
            await self._session.flush()
            logger.warning("send_blocked_lease_conflict", ctx, reason_code=str(exc))
            # This counter is the P0-adjacent signal from
            # docs/deployment-and-operations.md: "sustained duplicate reply
            # signal". A rising rate here means humans and the AI are
            # repeatedly racing, which is a control problem, not a model one.
            get_metrics().lease_conflicts_total.inc()
            get_metrics().observe_run(
                outcome="handed_off",
                route=route,
                latency_seconds=run.latency_ms / 1000.0,
                citation_count=citation_count,
            )
            return RunOutcome(
                run_id=run.id,
                status=RunStatus.HANDED_OFF,
                route=route,
                answer_text=draft.text,
                send_blocked_reason=str(exc),
                citation_count=citation_count,
                latency_ms=run.latency_ms,
                trace_id=ctx.trace_id,
            )

        # --- 8b. Take exclusive right to finish this run. ---
        #
        # Placed here, immediately before the dispatch, and that placement is the
        # point. A compare-and-set at the *terminal write* would be worthless:
        # the reply has already gone out by then, so the loser would discover it
        # had lost only after the customer had received two answers. The claim
        # has to come before the side effect it protects.
        #
        # `run.version` is what this worker observed when it loaded the run, so
        # the predicate is "still the run I started working on". A lease expiry
        # or a redelivered queue message gives a second worker the same row, and
        # without this both of them reach the dispatch.
        from platform_core.agent_runtime.terminal import claim_terminal

        claimed_version = await claim_terminal(
            self._session,
            run_id=run.id,
            expected_status=run.status,
            expected_version=run.version,
        )
        if claimed_version is None:
            # Someone else finished this run while we were thinking. Not an
            # error and not a retry: the work is done, by someone, and repeating
            # it would be the duplicate reply this check exists to prevent.
            get_metrics().lease_conflicts_total.inc()
            logger.info(
                "terminal_claim_lost",
                ctx,
                run_id=str(run.id),
                route=route,
            )
            return RunOutcome(
                run_id=run.id,
                status=RunStatus.SUPERSEDED,
                route=route,
                answer_text="",
                send_blocked_reason="TERMINAL_CLAIM_LOST",
                citation_count=0,
                latency_ms=int((time.monotonic() - started) * 1000),
                trace_id=ctx.trace_id,
            )
        # Kept in step with the row so the flush below cannot write a stale
        # value back over the bump.
        run.version = claimed_version

        # --- 9. Dispatch through Chatwoot with an idempotency key. ---
        #
        # Shadow mode (feature list 9.2): produce everything, send nothing.
        # This is how a newly-automated category goes live - the leak analysis
        # says "these 42 handoffs are evidence gaps", someone writes the
        # document, and this is the step where you watch what the platform
        # *would* have said before letting it talk to customers.
        # `internal_draft` joins shadow mode here rather than getting its own
        # suppression branch. The API has accepted this mode since it was
        # written and **nothing read it** - so an operator asking for a draft
        # got a message sent to the customer, which is the one outcome the mode
        # exists to prevent.
        shadow = self._mode == MODE_INTERNAL_DRAFT or await self._flag_enabled(
            self._settings().flag_shadow_mode, tenant_id
        )
        if shadow:
            # Logged rather than metered for now; the counter belongs with the
            # rest of the run metrics and is not worth a half-added one here.
            logger.info(
                "shadow_suppressed",
                ctx,
                run_id=str(run.id),
                route=route,
                output_hash=hashlib.sha256(draft.text.encode()).hexdigest()[:16],
            )
            send_error = "SHADOW_MODE"
        else:
            send_error = await self._dispatch(
                run=run,
                tenant_id=tenant_id,
                draft_text=draft.text,
                ctx=ctx,
                channel_conversation_key=channel_conversation_key,
                conversation_ref_id=conversation_ref_id,
                channel_system=channel_system,
                channel_address=channel_address,
            )
        # Deliberately not a failure: the answer exists and was reviewed by
        # every gate, only the delivery was withheld. Marking it FAILED would
        # make a shadow run indistinguishable from a broken one, which is the
        # opposite of what the observation is for.
        #
        # `OUTBOUND_NOT_CONFIGURED` belongs in the same bucket for a harder
        # reason: the agent turn is only written for a COMPLETED run, so failing
        # here would discard the answer *and* the platform's only record of it,
        # leaving a receive-only deployment unable to show what it produced.
        withheld_delivery = shadow or send_error == "OUTBOUND_NOT_CONFIGURED"
        if send_error and not withheld_delivery:
            run.status = RunStatus.FAILED.value
            run.latency_ms = int((time.monotonic() - started) * 1000)
            await self._session.flush()
            # A failed run used to leave exactly one thing behind: a status.
            # An operator's only view was a count in a dashboard, with no
            # reason on the row and no way to try again - the customer whose
            # question went unanswered had no path to an answer except somebody
            # noticing the number. Same dead-letter table as connector failures,
            # so there is one operator list rather than two.
            #
            # The error code travels; the message does not. This table is read
            # by an operational endpoint and copied into backups, and an
            # exception message can carry a connection string with a password.
            from platform_core.agent_runtime.rerun import record_run_failure

            await record_run_failure(
                self._session,
                run_id=run.id,
                tenant_id=tenant_id,
                error_code=send_error,
                attempts=1,
            )
            get_metrics().observe_run(
                outcome="failed",
                route=route,
                latency_seconds=run.latency_ms / 1000.0,
                citation_count=citation_count,
            )
            return RunOutcome(
                run_id=run.id,
                status=RunStatus.FAILED,
                route=route,
                answer_text=draft.text,
                send_blocked_reason=send_error,
                citation_count=citation_count,
                latency_ms=run.latency_ms,
                trace_id=ctx.trace_id,
            )

        # --- 10. Finalize the run. ---
        run.status = RunStatus.COMPLETED.value
        run.output_hash = hashlib.sha256(draft.text.encode()).hexdigest()
        run.latency_ms = int((time.monotonic() - started) * 1000)
        await self._session.flush()
        await audit_service.record(
            self._session,
            ctx=TenantContext(tenant_id=tenant_id, actor_id=None, actor_kind="service"),
            action="agent_run.completed",
            resource_type="agent_run",
            resource_id=run.id,
            decision="completed",
            reason_code="OK",
            after={
                "route": route,
                "citation_count": citation_count,
                "code_version": self._code_version,
            },
            trace_id=ctx.trace_id,
        )
        # Billing event: one terminal, billable outcome. Written in the same
        # transaction as the run's final state, so a consumer can never see a
        # billed run that did not complete (or a completed run that was not
        # billed). `run.token_usage` is populated from the provider.
        await enqueue(
            self._session,
            tenant_id=tenant_id,
            event_type="usage.recorded",
            aggregate_type="agent_run",
            aggregate_id=str(run.id),
            payload={
                "run_id": str(run.id),
                "route": route,
                "status": run.status,
                "prompt_tokens": int(run.token_usage.get("prompt_tokens", 0)),
                "completion_tokens": int(run.token_usage.get("completion_tokens", 0)),
            },
            trace_id=ctx.trace_id,
        )
        logger.info(
            "run_completed",
            ctx,
            route=route,
            status=run.status,
            latency_ms=run.latency_ms,
            chunk_count=citation_count,
        )
        get_metrics().observe_run(
            outcome="completed",
            route=route,
            latency_seconds=run.latency_ms / 1000.0,
            citation_count=citation_count,
        )
        return RunOutcome(
            run_id=run.id,
            status=RunStatus.COMPLETED,
            route=route,
            answer_text=draft.text,
            # Carried even on the success path so a withheld send is visible:
            # "completed but nothing went out" has to be distinguishable from
            # "delivered", or a shadow window reads as a silent outage.
            send_blocked_reason=send_error,
            citation_count=citation_count,
            latency_ms=run.latency_ms,
            trace_id=ctx.trace_id,
        )

    async def _generate_with_telemetry(
        self,
        ctx: TraceContext,
        question: str,
        evidence: list[RetrievedChunk],
        *,
        context: CompactedContext | None = None,
        retrieval_query: str = "",
    ) -> DraftAnswer:
        """Wrap the model call with a span and latency/token/error metrics.

        Kept separate from `run()` so the model boundary is measurable
        without threading timing variables through the pipeline body.

        `context` carries the compressed conversation. It is handed to the
        generator rather than concatenated into `question` because the question
        is what the model must answer and the context is what it must not
        contradict — merging them would make "what was asked" and "what was
        said earlier" indistinguishable in the prompt, and in the audit trail.

        Note on "first-token latency": the provider surface
        (`ChatProvider.complete`) is non-streaming, so there is no first
        token to timestamp - only a total. We therefore record the total
        model latency and label it as such, rather than reporting a
        first-token P95 that would in fact be a total-latency P95 under a
        metric name that documents a different promise.
        """
        assert self._deps.generator is not None  # guarded by the caller
        metrics = get_metrics()
        started = time.monotonic()
        span = ctx.span("model.generate", **{"span.kind": "client"})
        try:
            # Cast rather than `Any`: the override is either the injected
            # generator or a `with_template` copy of it, so it has the same
            # contract - and losing that would make this call untyped for every
            # future reader.
            _effective = cast(LlmAnswerGenerator, self._generator_override or self._deps.generator)
            draft = await _effective.generate(
                question,
                evidence,
                context=context,
                retrieval_query=retrieval_query,
            )
        except ModelError as exc:
            elapsed = time.monotonic() - started
            metrics.observe_model_call(
                operation="generate", outcome="error", latency_seconds=elapsed, error_code=exc.code
            )
            span.set_attributes(error_code=exc.code)
            span.set_status("error", exc.code)
            span.end()
            raise
        else:
            elapsed = time.monotonic() - started
            # Token counts are carried on the DraftAnswer (see `_usage_of` in
            # the generator) and persisted onto the run right after
            # generation, so this only records the model-call telemetry.
            metrics.observe_model_call(operation="generate", outcome="ok", latency_seconds=elapsed)
            span.set_attributes(latency_ms=int(elapsed * 1000))
            span.end()
            return draft

    async def _attempt_business_read(
        self,
        *,
        run: AgentRun,
        tenant_id: uuid.UUID,
        question: str,
        detection: IntentDetection,
        ctx: TraceContext,
        started: float,
        conversation_ref_id: uuid.UUID,
        expected_lease_version: int,
        channel_conversation_key: str | None,
        history: list[Turn] | None = None,
    ) -> RunOutcome | list[RetrievedChunk]:
        """Attempt the read-tool path for a BUSINESS_READ run (plan 3.2/3.4).

        Returns tool-receipt evidence on success, or a finished RunOutcome
        that hands off - the two outcomes, never a fallback that pretends the
        knowledge corpus holds live data (ADR 0006). Every decision point is
        audited: selection, proposal, execution.

        `history` is the loaded conversation, used only to count consecutive
        clarifications so an incomplete question escalates instead of being
        asked forever - see `_clarify_or_escalate_read`.
        """
        import json as _json

        from platform_core.tool_gateway.gateway import ToolGateway, ToolGatewayError
        from platform_core.tool_gateway.registry import ConnectorExecutorResolver
        from platform_core.tool_gateway.selector import select_read_tools
        from platform_policy import Action, Decision, PolicyEngine, Principal

        # Feature 2.5, gate 1 of 2: an *anonymous* visitor never reaches the
        # data. This fires before selection, before any connector call, and
        # before anything about the question is turned into parameters - there
        # is no version of "look up the order first, check ownership after"
        # that does not already have the data in hand. The customer is told
        # exactly what unblocks it (2.2), which is the difference between a
        # gate and a dead end.
        if self._verified_account == "":
            # Asks, and keeps the conversation.
            #
            # This used to `_handoff_for_tool`, which releases the lease to the
            # human queue - and the release is one-way (`lease_service` only
            # ever sets `owner_type="ai"` when it creates the lease). So an
            # anonymous visitor asking the most natural first question there is
            # ("where is my order?") was transferred to a person *before* they
            # had any chance to prove who they were, and every later question
            # in that conversation went unanswered, including the knowledge
            # questions the platform can answer.
            #
            # The message already names the one thing that unblocks it (2.2),
            # so it is a request for information, not a failure: the customer
            # can still act on it. Asking keeps the lease with the AI, which is
            # what makes acting on it worth anything.
            return await self._clarify_or_escalate_read(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                reason_code="IDENTITY_REQUIRED",
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
                history=history,
            )

        candidates = select_read_tools(detection, question)
        if not candidates:
            return await self._handoff_for_tool(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                reason_code="TOOL_NO_CANDIDATE",
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
            )

        resolver = ConnectorExecutorResolver(
            self._session,
            tenant_id=tenant_id,
            factories=self._deps.tool_factories,
            trace_id=ctx.trace_id,
        )
        # `case.read` used to be injected here by hand, because the registry
        # resolved executors only for connector-backed tools. The registry now
        # builds platform-internal tools itself, which is also what makes them
        # executable through the HTTP API - the hand-injection worked for this
        # one caller and left `POST /v1/tool-proposals/{id}/execute` answering
        # TOOL_EXECUTOR_MISSING for a tool the catalog advertises.
        executors = await resolver.executors_for([c.tool_name for c in candidates])
        usable = [c for c in candidates if c.tool_name in executors]
        if not usable:
            return await self._handoff_for_tool(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                reason_code="TOOL_UNAVAILABLE",
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
            )
        chosen = usable[0]
        arguments = _extract_tool_args(chosen.tool_name, question)
        if arguments is None:
            # Same reasoning as IDENTITY_REQUIRED above: the tool was found and
            # the question simply does not name the record yet, so the platform
            # asks for it and stays. Handing off here meant the customer
            # answered the platform's own question ("please give me the order
            # number") into a conversation that could no longer be answered by
            # anything - measured as 62s of silence, 2026-09-23.
            return await self._clarify_or_escalate_read(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                reason_code="TOOL_ARGUMENT_MISSING",
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
                history=history,
            )

        # The AI acts as the integration service for READS: TOOL_READ is the
        # only action it needs, and the gateway re-checks it (required_action)
        # so a policy change revoking the role stops the AI mid-path.
        service_actor = uuid.uuid5(tenant_id, "system:ai-agent")
        principal = Principal(
            tenant_id=str(tenant_id), actor_id=str(service_actor), role="integration_service"
        )
        allowed = PolicyEngine().check(principal, Action.TOOL_READ).decision == Decision.ALLOW
        idempotency_key = str(
            uuid.uuid5(
                tenant_id,
                "tool:"
                + str(run.id)
                + ":"
                + chosen.tool_name
                + ":"
                + _json.dumps(arguments, sort_keys=True, ensure_ascii=False),
            )
        )
        from platform_core.tool_gateway.registry import ensure_tool_definitions

        await ensure_tool_definitions(self._session, tenant_id=tenant_id)
        gateway = ToolGateway(self._session, {chosen.tool_name: executors[chosen.tool_name]})
        try:
            proposal = await gateway.propose(
                tenant_id=tenant_id,
                actor_id=service_actor,
                tool_name=chosen.tool_name,
                arguments=arguments,
                role=principal.role,
                idempotency_key=idempotency_key,
                permission_allowed=allowed,
                required_action=Action.TOOL_READ.value,
            )
            execution = await gateway.execute(
                tenant_id=tenant_id,
                actor_id=service_actor,
                proposal_id=proposal.id,
            )
        except ToolGatewayError as exc:
            await audit_service.record(
                self._session,
                ctx=TenantContext(tenant_id=tenant_id, actor_id=None, actor_kind="system"),
                action="tool_read.failed",
                resource_type="tool",
                resource_id=uuid.uuid5(tenant_id, f"tool:{chosen.tool_name}"),
                decision="denied",
                reason_code=exc.code[:63],
                trace_id=ctx.trace_id,
            )
            return await self._handoff_for_tool(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                reason_code="TOOL_EXECUTION_FAILED",
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
            )

        if execution.status not in ("executed", "verified"):
            # UNKNOWN is the honest answer for an ambiguous read: reporting
            # it as data would be fabrication by omission.
            return await self._handoff_for_tool(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                reason_code="TOOL_EXECUTION_UNVERIFIED",
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
            )

        output = execution.sanitized_output or {}

        # Feature 2.5, gate 2 of 2: the receipt itself says whose record it is.
        # Checked here, before the receipt is published or the answer is built,
        # because once the card is on the timeline the leak has already
        # happened. A record with no owner is not gated - stock levels and
        # process capability are nobody's private data; orders, shipments and
        # invoices are.
        receipt_account = output.get("account")
        if not receipt_account and isinstance(output.get("record"), dict):
            receipt_account = output["record"].get("account")
        if (
            self._verified_account not in (None, "")
            and receipt_account
            and str(receipt_account) != self._verified_account
        ):
            logger.warning(
                "read_ownership_refused",
                ctx,
                tool_name=chosen.tool_name,
                reason_code="IDENTITY_MISMATCH",
            )
            # Label names must match the counter's declared labelnames
            # exactly - prometheus_client raises ValueError on an unknown
            # label, and here that would abort the run *on the refusal path*:
            # the gate would fire, then crash before the customer was told
            # anything, which is the one outcome worse than no gate.
            get_metrics().policy_denials_total.labels(
                action=Action.TOOL_READ.value, reason_code="IDENTITY_MISMATCH"
            ).inc()
            return await self._handoff_for_tool(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                reason_code="IDENTITY_MISMATCH",
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
            )

        receipt_json = _json.dumps(output, sort_keys=True, ensure_ascii=False, default=str)
        ref_value = next(iter(arguments.values()), "")
        receipt_id = uuid.uuid5(tenant_id, f"receipt:{chosen.tool_name}:{idempotency_key}")
        # The excerpt is what the *model* grounds on; the card gets the whole
        # payload separately (see `published` below). These were two different
        # documents, and the difference was the defect.
        #
        # This used to be `receipt_json[:280]`. A receipt for an order with four
        # stages is ~380 characters, so the model was handed a JSON document cut
        # off mid-object - it ended mid-key, with unbalanced braces. The prompt
        # tells the model to answer only from the evidence and to say it cannot
        # verify when the evidence does not support the question, so the model
        # did exactly that: `{"claims": []}`. That became NO_CLAIMS, which
        # became an abstention and a handoff.
        #
        # Measured 2026-09-23: `tool_read_executed order.get_status` followed by
        # `run_abstained NO_CLAIMS`, while the card - built from the untruncated
        # payload - rendered all four stages correctly. The customer saw the data
        # and a sentence saying the platform could not verify it.
        #
        # The whole receipt goes to the model now. It is one record, not a
        # document, so `MAX_EXCERPT_CHARS` is the real bound - imported rather
        # than restated, so the check and the generator cannot drift apart.
        if len(receipt_json) > MAX_EXCERPT_CHARS:
            # Not truncated silently: a receipt too large to ground on is a
            # configuration problem (an adapter returning a whole table), and
            # the log is where that shows up instead of as an abstention.
            logger.warning(
                "receipt_exceeds_model_budget",
                ctx,
                tool_name=chosen.tool_name,
                reason_code="RECEIPT_TOO_LARGE",
                tenant_id=str(tenant_id),
            )
        receipt = RetrievedChunk(
            chunk_id=receipt_id,
            document_version_id=None,
            title=f"tool://{chosen.tool_name}",
            section_path=[],
            # The receipt, plus the label mapping for the state codes it
            # contains. Without the glossary the model reads `in_production` and
            # writes it back, while the card drawn from the same receipt says
            # 生产中 - the same record described in two vocabularies in one
            # turn. The mapping comes from `tool_card`, which is also what the
            # card renders, so the two cannot drift.
            excerpt=receipt_json + glossary_for(receipt_json),
            source_uri=f"tool://{chosen.tool_name}/{ref_value}",
            score=1.0,
            ranking={"tool": 1.0},
        )
        logger.info(
            "tool_read_executed",
            ctx,
            tool_name=chosen.tool_name,
            run_id=str(run.id),
            status=str(execution.status),
        )
        # Publish the receipt to the conversation as a TOOL turn.
        #
        # Without this the customer only ever sees the model's prose *about*
        # their order and never the data itself - the platform queries it,
        # cites it internally, and then describes it in a sentence. That is
        # feature list 4A.3 (results as cards, not free text) and 4A.4
        # (freshness), and neither can exist while the receipt stops here.
        #
        # The whole receipt is published, not the 280-char excerpt: the excerpt
        # exists to bound what reaches the model's context, and a card needs the
        # fields. It is the tool's *sanitized* output, already redacted by the
        # gateway before it ever got this far.
        # `source` is VARCHAR(15), so the tool name cannot go there - it goes
        # in the payload, which is where the card needs it anyway. Discovered
        # by a test rather than by reading the model, and it would have been a
        # run-breaking DataError for the longer tool names.
        published = _json.dumps(
            {**output, "tool": chosen.tool_name}, sort_keys=True, ensure_ascii=False, default=str
        )
        # Turns are stored through `evaluation.pii.redact_text`, which masks
        # phone-shaped runs - a 10-digit `fetched_at` becomes `[PHONE]` and the
        # receipt stops being valid JSON. Found by a test, not by reading the
        # model. Rather than show the customer a corrupted card, publish only
        # what survives the round trip; a receipt that cannot be trusted is
        # worse than no card, and the answer still stands on its own.
        if not self._survives_redaction(published):
            logger.warning(
                "receipt_publish_skipped",
                ctx,
                tool_name=chosen.tool_name,
                reason="redaction_corrupts_payload",
            )
            return [receipt]
        await self._publish_receipt(
            tenant_id=tenant_id,
            conversation_ref_id=conversation_ref_id,
            tool_name=chosen.tool_name,
            receipt_json=published,
            ts=int(time.time()),
        )
        return [receipt]

    @staticmethod
    def _survives_redaction(payload: str) -> bool:
        """True when the payload is still valid JSON after PII redaction.

        The turn store redacts on write, so the question is not whether the
        receipt is well-formed now but whether it will be when read back.
        Anything the redactor rewrites inside a value turns the document into
        something no parser will accept, and a card built from that would be
        showing the customer data this platform cannot vouch for.
        """
        import json as json_module

        from platform_core.evaluation.pii import redact_text

        try:
            # `redact_text` returns (text, replacement_count) - taking the
            # first element matters, and passing the tuple straight to the
            # parser made every receipt look corrupt.
            redacted, _count = redact_text(payload)
            json_module.loads(redacted)
        except Exception as exc:  # noqa: BLE001 - the check is "is it still JSON"
            # Logged, not swallowed. This branch is also what a NameError from
            # the imports above lands in, and an unlogged catch here would make
            # "no receipts published" look like "no receipts worth publishing".
            logger.warning("receipt_not_json_after_redaction", error_code=type(exc).__name__)
            return False
        return True

    async def _publish_receipt(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_ref_id: uuid.UUID,
        tool_name: str,
        receipt_json: str,
        ts: int,
    ) -> None:
        """Put a tool's result on the timeline where the customer can see it.

        Best-effort on purpose: the answer has already been produced, and a
        turn-store failure must not turn a completed answer into a failed run.
        The receipt is evidence the customer is owed, not a dependency of
        answering.
        """
        try:
            from platform_core.agent_runtime import conversation_store
            from platform_core.agent_runtime.conversation import Turn, TurnRole

            await conversation_store.append_turn(
                self._session,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                turn=Turn(role=TurnRole.TOOL, text=receipt_json, ts=ts),
                source="tool",
            )
        except Exception as exc:  # noqa: BLE001 - presentation, not a dependency
            logger.warning(
                "receipt_publish_failed",
                tool_name=tool_name,
                error_code=type(exc).__name__,
            )

    async def _pending_eq_confirmation(
        self, *, tenant_id: uuid.UUID, conversation_ref_id: uuid.UUID
    ) -> str | None:
        """The EQ case this conversation is waiting on, if there is exactly one.

        Read under the run's RLS-bound transaction, so another tenant's case is
        invisible rather than filtered afterwards. The explicit tenant filter is
        defence in depth, not the mechanism.

        **Ambiguity returns None, not the first row.** Two cases waiting on the
        same conversation means the link does not identify which confirmation
        the customer is giving, and confirming the wrong one releases
        production against a spec nobody agreed. Handing off is the honest
        outcome; picking one is a coin flip with a board order behind it.
        """
        from sqlalchemy import and_, select

        from platform_core.cases.models import (
            Case,
            CaseCategory,
            CaseConversation,
            CaseStatus,
        )

        rows = (
            (
                await self._session.execute(
                    select(Case.id)
                    .join(
                        CaseConversation,
                        and_(
                            CaseConversation.case_id == Case.id,
                            CaseConversation.tenant_id == Case.tenant_id,
                        ),
                    )
                    .where(
                        Case.tenant_id == tenant_id,
                        CaseConversation.conversation_ref_id == conversation_ref_id,
                        Case.category == CaseCategory.EQ_CONFIRMATION.value,
                        Case.status == CaseStatus.WAITING_CUSTOMER.value,
                    )
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        return str(rows[0]) if len(rows) == 1 else None

    async def _attempt_business_write(
        self,
        *,
        run: AgentRun,
        tenant_id: uuid.UUID,
        question: str,
        detection: IntentDetection,
        ctx: TraceContext,
        started: float,
        conversation_ref_id: uuid.UUID,
        expected_lease_version: int,
        channel_conversation_key: str | None,
    ) -> RunOutcome | list[RetrievedChunk]:
        """Attempt the write path for a BUSINESS_WRITE run (plan 3.5).

        Returns tool-receipt evidence when a write ran and verified, or a
        finished RunOutcome that hands off. There is no third outcome: a write
        is never assumed to have happened, and UNKNOWN is never reported as
        success.

        The safety property this method exists to uphold is that **the agent
        proposes and never approves**. For a tool whose catalog entry requires
        confirmation the run ends at the proposal - the row is left
        `authorized` for a human to confirm in the admin console, and the
        conversation hands off so a person owns the result. The agent holds no
        code path to `confirm`, and the gateway refuses a confirmation from the
        proposing actor regardless, so this is enforced in two places rather
        than assumed in one.

        Every decision point is audited: selection, proposal, execution.
        """
        import json as _json

        from platform_core.tool_gateway.gateway import ToolGateway, ToolGatewayError
        from platform_core.tool_gateway.registry import (
            ConnectorExecutorResolver,
            ensure_tool_definitions,
            risk_action_for,
        )
        from platform_core.tool_gateway.selector import select_write_tools
        from platform_policy import Decision, PolicyEngine, Principal

        async def _handoff(reason_code: str) -> RunOutcome:
            return await self._handoff_for_tool(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                reason_code=reason_code,
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
            )

        candidates = select_write_tools(detection, question)
        if not candidates:
            return await _handoff("TOOL_NO_CANDIDATE")

        resolver = ConnectorExecutorResolver(
            self._session,
            tenant_id=tenant_id,
            factories=self._deps.tool_factories,
            trace_id=ctx.trace_id,
        )
        executors = await resolver.executors_for([c.tool_name for c in candidates])
        usable = [c for c in candidates if c.tool_name in executors]
        if not usable:
            return await _handoff("TOOL_UNAVAILABLE")
        chosen = usable[0]

        # Half the arguments come from the tenant's connector configuration
        # rather than from the customer (see WRITE_ARG_DEFAULTS). A tool whose
        # configured half is unset is not proposed with an invented value - a
        # ticket filed in the wrong project is worse than one never filed.
        defaults = await resolver.default_write_arguments(chosen.tool_name)
        arguments = (
            None if defaults is None else _extract_write_args(chosen.tool_name, question, defaults)
        )
        if arguments is None:
            return await _handoff("TOOL_ARGUMENT_MISSING")

        await ensure_tool_definitions(self._session, tenant_id=tenant_id)
        required_action = await risk_action_for(
            self._session, tenant_id=tenant_id, tool_name=chosen.tool_name
        )
        if required_action is None:
            return await _handoff("TOOL_NOT_REGISTERED")

        # The AI acts as the integration service for writes as it does for
        # reads, but the action it needs is the tool's own risk class. That is
        # what makes the role's limits bind here rather than only on the HTTP
        # caller: `integration_service` holds tool.write.low and
        # tool.write.confirmed and deliberately NOT tool.human_approval, so a
        # human_approval tool is refused by policy rather than by an `if` in
        # this method that a future edit could drop.
        service_actor = uuid.uuid5(tenant_id, "system:ai-agent")
        principal = Principal(
            tenant_id=str(tenant_id), actor_id=str(service_actor), role="integration_service"
        )
        allowed = PolicyEngine().check(principal, required_action).decision == Decision.ALLOW
        idempotency_key = str(
            uuid.uuid5(
                tenant_id,
                "tool:"
                + str(run.id)
                + ":"
                + chosen.tool_name
                + ":"
                + _json.dumps(arguments, sort_keys=True, ensure_ascii=False),
            )
        )
        gateway = ToolGateway(self._session, {chosen.tool_name: executors[chosen.tool_name]})
        try:
            proposal = await gateway.propose(
                tenant_id=tenant_id,
                actor_id=service_actor,
                tool_name=chosen.tool_name,
                arguments=arguments,
                role=principal.role,
                idempotency_key=idempotency_key,
                permission_allowed=allowed,
                required_action=required_action.value,
            )
        except ToolGatewayError as exc:
            await audit_service.record(
                self._session,
                ctx=TenantContext(tenant_id=tenant_id, actor_id=None, actor_kind="system"),
                action="tool_write.rejected",
                resource_type="tool",
                resource_id=uuid.uuid5(tenant_id, f"tool:{chosen.tool_name}"),
                decision="denied",
                reason_code=exc.code[:63],
                trace_id=ctx.trace_id,
            )
            return await _handoff("TOOL_WRITE_DENIED")

        await audit_service.record(
            self._session,
            ctx=TenantContext(tenant_id=tenant_id, actor_id=None, actor_kind="system"),
            action="tool_write.proposed",
            resource_type="tool_proposal",
            resource_id=proposal.id,
            decision="completed",
            reason_code="OK",
            after={
                "tool_name": chosen.tool_name,
                "risk_action": required_action.value,
                "requires_confirmation": bool(proposal.required_confirmation),
                "action_hash": proposal.action_hash,
            },
            trace_id=ctx.trace_id,
        )

        if proposal.required_confirmation:
            # Stop here, on purpose. The proposal is a draft a human completes
            # and approves; executing it would need an ActionConfirmation this
            # agent must not be able to write, and telling the customer the
            # write was done would be a lie. Handing off is what gives the
            # confirmation something to mean - somebody now owns the outcome,
            # and the proposal expires in 15 minutes if nobody acts on it.
            logger.info(
                "tool_write_awaiting_confirmation",
                ctx,
                tool_name=chosen.tool_name,
                proposal_id=str(proposal.id),
            )
            return await _handoff("TOOL_CONFIRMATION_PENDING")

        try:
            execution = await gateway.execute(
                tenant_id=tenant_id,
                actor_id=service_actor,
                proposal_id=proposal.id,
            )
        except ToolGatewayError as exc:
            await audit_service.record(
                self._session,
                ctx=TenantContext(tenant_id=tenant_id, actor_id=None, actor_kind="system"),
                action="tool_write.failed",
                resource_type="tool_proposal",
                resource_id=proposal.id,
                decision="failed",
                reason_code=exc.code[:63],
                trace_id=ctx.trace_id,
            )
            return await _handoff("TOOL_EXECUTION_FAILED")

        if execution.status not in ("executed", "verified"):
            # UNKNOWN is the honest answer for a write whose postcondition
            # could not be determined. Reporting it as done would be the most
            # expensive lie this platform could tell.
            return await _handoff("TOOL_EXECUTION_UNVERIFIED")

        output = execution.sanitized_output or {}
        receipt_json = _json.dumps(output, sort_keys=True, ensure_ascii=False, default=str)
        receipt = RetrievedChunk(
            chunk_id=uuid.uuid5(tenant_id, f"receipt:{chosen.tool_name}:{idempotency_key}"),
            document_version_id=None,
            title=f"tool://{chosen.tool_name}",
            section_path=[],
            excerpt=receipt_json[:280],
            source_uri=f"tool://{chosen.tool_name}/{proposal.id}",
            score=1.0,
            ranking={"tool": 1.0},
        )
        logger.info(
            "tool_write_executed",
            ctx,
            tool_name=chosen.tool_name,
            run_id=str(run.id),
            status=str(execution.status),
        )
        return [receipt]

    async def _handoff_for_tool(
        self,
        *,
        run: AgentRun,
        tenant_id: uuid.UUID,
        conversation_ref_id: uuid.UUID,
        expected_lease_version: int,
        reason_code: str,
        ctx: TraceContext,
        started: float,
        question: str,
        channel_conversation_key: str | None,
    ) -> RunOutcome:
        return await self._finish_abstain(
            run=run,
            tenant_id=tenant_id,
            conversation_ref_id=conversation_ref_id,
            expected_lease_version=expected_lease_version,
            decision=AbstentionDecision(abstain=True, reason_code=reason_code, handoff=True),
            ctx=ctx,
            started=started,
            question=question,
            channel_conversation_key=channel_conversation_key,
        )

    async def _finish_abstain(
        self,
        *,
        run: AgentRun,
        tenant_id: uuid.UUID,
        conversation_ref_id: uuid.UUID,
        expected_lease_version: int,
        decision: AbstentionDecision,
        ctx: TraceContext,
        started: float,
        question: str,
        channel_conversation_key: str | None,
        handoff_context: str | None = None,
    ) -> RunOutcome:
        """Record abstention, release the lease to the human queue, and send
        the customer-safe notice (still behind the lease gate).

        The notice is the point. This method used to compute
        `safe_abstention_text` and return it in the outcome without ever
        dispatching it, so an unanswerable question produced **silence**: the
        handoff happened internally and the customer was told nothing - not
        that the platform could not verify the answer, and not that a human was
        coming. Abstention is the most common failure mode, so that was the
        most common outcome a customer could experience.

        Found by `tests/e2e/e2e_chatwoot_loop.py`, which is the only thing that
        asks a live Chatwoot whether a reply appeared.
        """
        run.status = RunStatus.ABSTAINED.value
        run.abstain_reason = decision.reason_code[:127]
        run.latency_ms = int((time.monotonic() - started) * 1000)
        await self._session.flush()

        # 4B: a pricing question is priced by the rule table or by a person,
        # never by the model. When the table can price it, the band goes to
        # the agent who will quote - the customer still hears from a human, so
        # nothing here weakens the AI-never-prices rule.
        if decision.handoff and run.route == Route.HUMAN_REQUIRED.value:
            from platform_core.pricing.service import quote_label

            band = quote_label(question)
            if band is not None:
                handoff_context = f"{handoff_context} | {band}" if handoff_context else band

        # 10.2: when the reason is an external system we could not reach, say
        # so. The generic notice ("I couldn't verify an answer") is true but
        # useless here - the customer is waiting on an outage, not on us, and
        # telling them which is which is the whole point of a graceful
        # degradation. It deliberately does NOT claim a ticket was created:
        # this path records the run and hands off, and "已留工单" when nothing
        # was filed would be the same lie as a tool reporting success it never
        # verified.
        #
        # `TOOL_EXECUTION_UNVERIFIED` has its own notice rather than sharing
        # this one: an ambiguous or absent record is not the third party's
        # outage, and reporting it as one sends the operator to look for a
        # failure that never happened (see `UNVERIFIED_READ_REASONS`).
        # The notices below follow the conversation's language, not the last
        # message's script: a bare `SO-9001` carries none, and a Chinese
        # customer must not be switched to English mid-conversation (see
        # `language.conversation_is_chinese` for the measured failure).
        prior = self._history_texts
        if decision.reason_code in SYSTEM_OUTAGE_REASONS:
            notice = system_outage_notice(question, prior_texts=prior)
        elif decision.reason_code in UNVERIFIED_READ_REASONS:
            notice = unverified_read_notice(question, prior_texts=prior)
            # The same queue/out-of-hours enrichment applies: this notice also
            # promises a person, so it must not promise one who is not there.
            if decision.handoff and not is_open():
                notice = f"{notice} {offline_notice(question=question, prior_texts=prior)}"
            elif decision.handoff:
                try:
                    position_notice = queue_notice(
                        await queue_status(
                            self._session,
                            tenant_id=tenant_id,
                            conversation_ref_id=conversation_ref_id,
                        ),
                        question,
                        prior_texts=prior,
                    )
                except Exception:  # noqa: BLE001 - enrichment, not a dependency
                    position_notice = None
                if position_notice:
                    notice = f"{notice} {position_notice}"
        else:
            notice = safe_abstention_text(decision.reason_code, question, prior_texts=prior)
            # 7.5: never promise a person who is not there. The reason code
            # still says why the run stopped - that is for the receiving agent
            # and the audit log - but the customer is told the truth about when
            # someone will look at it. Only for handoffs: a clarification that
            # says "we are closed" would strand a customer who could have
            # answered and been answered.
            if decision.handoff and not is_open():
                notice = offline_notice(question=question)
            elif decision.handoff:
                # 1.7: where they are in the queue, now that someone is
                # actually there to work it. Mutually exclusive with the
                # branch above by construction: naming a position on a queue
                # nobody is serving would promise a wait that cannot start.
                # Enrichment only - a failure to count the queue must never
                # stop the handoff from being sent.
                try:
                    position_notice = queue_notice(
                        await queue_status(
                            self._session,
                            tenant_id=tenant_id,
                            conversation_ref_id=conversation_ref_id,
                        ),
                        question,
                    )
                except Exception:  # noqa: BLE001 - enrichment, not a dependency
                    position_notice = None
                if position_notice:
                    notice = f"{notice} {position_notice}"

        # Send the notice **before** releasing the lease, because the lease
        # gate below refuses once the owner is the queue. Releasing first was
        # the first version of this fix and it blocked every notice with
        # "owner is queue" - the customer would still have heard nothing, just
        # with a different reason in the log.
        #
        # The re-check is the same one the answer path performs: a human may
        # have taken over between retrieval and here, and a notice saying "let
        # me connect you with a colleague" sent *after* a colleague already
        # replied is exactly the duplicate the lease exists to stop.
        send_error = ""
        try:
            await lease_service.assert_can_send(
                self._session,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_version=expected_lease_version,
            )
        except LeaseConflict as exc:
            send_error = f"LEASE_CONFLICT: {exc}"
            logger.warning("abstain_notice_blocked", ctx, reason_code=str(exc))
            get_metrics().lease_conflicts_total.inc()
        else:
            send_error = await self._dispatch(
                run=run,
                tenant_id=tenant_id,
                draft_text=notice,
                ctx=ctx,
                channel_conversation_key=channel_conversation_key,
                conversation_ref_id=conversation_ref_id,
                # The channel travels with the notice, not only with the
                # answer. Without this the notice fell through to the
                # platform-surface branch, returned "" - which means
                # *delivered* - and an email or WeChat customer whose question
                # could not be answered heard nothing at all.
                channel_system=self._channel_system,
                channel_address=self._channel_address,
            )
            # The evidence-carrying handoff note is gone with its transport.
            # It existed to push the reason code and the gathered evidence into
            # Chatwoot as a private note, because Chatwoot was where the agent
            # worked. The agent now works in `Workbench`, which reads the case,
            # the conversation, the run's citations and the account contacts
            # straight from the platform's own tables (`/v1/cases/{id}/workbench`).
            # Re-sending that as a note would be a second copy of data the
            # operator is already looking at.

        if decision.reason_code == ABSTAIN_CONFLICT:
            # Conflicting sources become a review-able gap (plan 4.5): the
            # abstention is honest, but two documents disagreeing is a corpus
            # defect someone must adjudicate, and an audit line nobody reads
            # is not adjudication.
            try:
                from platform_core.knowledge.gap_service import record_gap

                await record_gap(
                    self._session,
                    tenant_id=tenant_id,
                    question=question,
                    reason_code=decision.reason_code,
                )
            except Exception:  # noqa: BLE001 - the gap is enrichment
                logger.warning("gap_record_failed", ctx, reason_code=decision.reason_code)

        if decision.handoff:
            # Moves the lease, closes out the runs this conversation was still
            # holding, and tells the customer once if anything was closed. See
            # `agent_runtime.handoff` for why that is one operation and not
            # three: the count is only knowable at the moment ownership moves.
            await hand_off_to_human_queue(
                self._session,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                reason=f"abstain:{decision.reason_code}",
            )

        # The routing decision is `metadata`, not `after`, and that is
        # deliberate: `after` is hashed and unreadable, while these three
        # values are the *parameters* of the handoff - which team it went to,
        # which product line it is, and the context the run gathered. They used
        # to travel in a private Chatwoot note; with that transport gone
        # (ADR 0012), this is the record an operator or reviewer reads to answer
        # "where did this handoff go, and why".
        handoff_metadata = {
            key: value
            for key, value in (
                ("team", self._target_team),
                ("business_line", self._business_line_note),
                ("context", handoff_context),
                # 1.3: "please send a photo" when the customer already sent two
                # is the exchange that makes a handoff feel like starting over.
                # Content types only - the files never entered the platform.
                ("customer_attachments", ",".join(self._attachment_types) or None),
            )
            if value
        }
        await audit_service.record(
            self._session,
            ctx=TenantContext(tenant_id=tenant_id, actor_id=None, actor_kind="service"),
            action="agent_run.abstained",
            resource_type="agent_run",
            resource_id=run.id,
            decision="abstained",
            reason_code=decision.reason_code[:63],
            after={"handoff": decision.handoff, "notice_sent": not send_error},
            metadata=handoff_metadata or None,
            trace_id=ctx.trace_id,
        )
        logger.info(
            "run_abstained",
            ctx,
            route=run.route,
            status=run.status,
            reason_code=decision.reason_code,
            notice_sent=not send_error,
        )
        self._observe_cost(run)
        get_metrics().observe_run(
            outcome="handed_off" if decision.handoff else "abstained",
            route=run.route,
            latency_seconds=run.latency_ms / 1000.0,
            abstain_reason=decision.reason_code,
        )
        return RunOutcome(
            run_id=run.id,
            status=RunStatus.ABSTAINED,
            route=run.route,
            answer_text=notice,
            abstain_reason=decision.reason_code,
            handoff=decision.handoff,
            send_blocked_reason=send_error,
            latency_ms=run.latency_ms,
            trace_id=ctx.trace_id,
        )

    def _observe_cost(self, run: AgentRun) -> None:
        """Estimated run cost in cents (plan 5.1). Pricing is config, usage
        is the provider's own accounting - neither is invented here."""
        usage = run.token_usage or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        if not prompt_tokens and not completion_tokens:
            return
        cfg = self._settings()
        cost = (
            prompt_tokens * cfg.cost_prompt_cents_per_1k / 1000.0
            + completion_tokens * cfg.cost_completion_cents_per_1k / 1000.0
        )
        get_metrics().run_cost_cents.observe(max(0.0, cost))

    async def _clarify_or_escalate_read(
        self,
        *,
        run: AgentRun,
        tenant_id: uuid.UUID,
        conversation_ref_id: uuid.UUID,
        expected_lease_version: int,
        reason_code: str,
        ctx: TraceContext,
        started: float,
        question: str,
        channel_conversation_key: str | None,
        history: list[Turn] | None,
    ) -> RunOutcome:
        """Ask for the missing detail - unless asking has stopped working.

        A read that cannot proceed because the question is incomplete is a
        request for information, and it keeps the conversation (see
        `_finish_clarify`). But an ask that never escalates is a loop: a customer
        who does not supply the order number - because they do not have it, or
        because they are asking about something else entirely - would be asked
        forever.

        So the same limit the knowledge and write paths already apply is applied
        here: `clarification_max_streak` (2), counted from the `clarify:` refs
        the inbox consumer writes. Those two paths checked it; this one did not,
        because until 2026-09-23 this path handed off immediately and there was
        no loop to guard. Routing it to a clarification introduced the loop, so
        it needs the guard the other two already had.

        Measured before adding it: three consecutive 「我的订单到哪了？」 with no
        order number produced three identical clarifications and no escalation,
        against a threshold of 2. (The first reading of this said the `clarify:`
        marker was never written at all - it is, by
        `worker.inbox_consumer`, and the stored refs show it. The guard was
        simply not consulted on this path.)
        """
        if _clarify_streak(history or []) >= self._settings().clarification_max_streak:
            logger.info(
                "read_clarify_limit_reached",
                ctx,
                route=run.route,
                reason_code="CLARIFICATION_LIMIT",
            )
            return await self._finish_abstain(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                decision=AbstentionDecision(
                    abstain=True, reason_code="CLARIFICATION_LIMIT", handoff=True
                ),
                ctx=ctx,
                started=started,
                question=question,
                channel_conversation_key=channel_conversation_key,
            )
        return await self._finish_clarify(
            run=run,
            tenant_id=tenant_id,
            conversation_ref_id=conversation_ref_id,
            expected_lease_version=expected_lease_version,
            reason_code=reason_code,
            notice_source=reason_code,
            ctx=ctx,
            started=started,
            question=question,
            channel_conversation_key=channel_conversation_key,
        )

    async def _finish_clarify(
        self,
        *,
        run: AgentRun,
        tenant_id: uuid.UUID,
        conversation_ref_id: uuid.UUID,
        expected_lease_version: int,
        reason_code: str,
        ctx: TraceContext,
        started: float,
        question: str,
        channel_conversation_key: str | None,
        notice_source: str = ABSTAIN_CLARIFICATION,
    ) -> RunOutcome:
        """Ask the customer for the missing detail; keep the conversation.

        The one structural difference from `_finish_abstain` is that this does
        **not** release the lease. A clarification expects a reply, and
        releasing the lease to the queue would hand the conversation to a human
        who now has to ask the same question the platform just asked — so the
        customer would be asked twice and the ticket would be waited on by
        nobody.

        It still goes through the pre-send lease re-check: a human may have
        taken over between arrival and here, and a clarification sent after a
        human already replied is the duplicate the lease exists to stop.

        `notice_source` picks which customer-facing sentence to send, and it is
        separate from `reason_code` on purpose: `reason_code` is the specific
        cause recorded on the audit row and in the log, while the sentence is
        whichever one names what actually unblocks the customer. The default
        reproduces the generic clarification text exactly, so the existing
        callers are unchanged.

        It exists because "the question is incomplete" and "the customer must
        prove who they are" are both *asks*, not failures, and both used to
        hand the conversation to a human instead of asking. Measured
        2026-09-23: clicking the page's own first suggestion produced a request
        for the order number **and** a one-way handoff, so the customer
        answered the platform's question and heard nothing ever again.
        """
        run.status = RunStatus.ABSTAINED.value
        run.abstain_reason = ABSTAIN_CLARIFICATION[:127]
        run.latency_ms = int((time.monotonic() - started) * 1000)
        await self._session.flush()

        notice = safe_abstention_text(notice_source, question, prior_texts=self._history_texts)

        send_error = ""
        try:
            await lease_service.assert_can_send(
                self._session,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_version=expected_lease_version,
            )
        except LeaseConflict as exc:
            send_error = f"LEASE_CONFLICT: {exc}"
            logger.warning("clarification_blocked", ctx, reason_code=str(exc))
            get_metrics().lease_conflicts_total.inc()
        else:
            send_error = await self._dispatch(
                run=run,
                tenant_id=tenant_id,
                draft_text=notice,
                ctx=ctx,
                channel_conversation_key=channel_conversation_key,
                conversation_ref_id=conversation_ref_id,
                # The channel travels with the notice, not only with the
                # answer. Without this the notice fell through to the
                # platform-surface branch, returned "" - which means
                # *delivered* - and an email or WeChat customer whose question
                # could not be answered heard nothing at all.
                channel_system=self._channel_system,
                channel_address=self._channel_address,
            )

        await audit_service.record(
            self._session,
            ctx=TenantContext(tenant_id=tenant_id, actor_id=None, actor_kind="service"),
            action="agent_run.clarification_requested",
            resource_type="agent_run",
            resource_id=run.id,
            decision="abstained",
            reason_code=reason_code[:63],
            after={"handoff": False, "notice_sent": not send_error},
            trace_id=ctx.trace_id,
        )
        logger.info(
            "run_clarification_requested",
            ctx,
            route=run.route,
            reason_code=reason_code,
            notice_sent=not send_error,
        )
        # Counted as an abstention, not a handoff: the conversation is still
        # the AI's, and a clarification that showed up in the handoff rate
        # would make the platform look like it is giving up far more often
        # than it is.
        get_metrics().observe_run(
            outcome="abstained",
            route=run.route,
            latency_seconds=run.latency_ms / 1000.0,
            abstain_reason=ABSTAIN_CLARIFICATION,
        )
        del question
        return RunOutcome(
            run_id=run.id,
            status=RunStatus.ABSTAINED,
            route=run.route,
            answer_text=notice,
            abstain_reason=ABSTAIN_CLARIFICATION,
            handoff=False,
            send_blocked_reason=send_error,
            latency_ms=run.latency_ms,
            trace_id=ctx.trace_id,
        )

    async def _dispatch(
        self,
        *,
        run: AgentRun,
        tenant_id: uuid.UUID,
        draft_text: str,
        ctx: TraceContext,
        # Optional: the platform's own surface has no channel conversation key,
        # and it is the channel branch that needs one.
        channel_conversation_key: str | None = None,
        conversation_ref_id: uuid.UUID,
        channel_system: str | None = None,
        channel_address: str | None = None,
    ) -> str:
        """Deliver the customer-visible reply. Returns "" on success, else a
        reason code.

        Exactly two destinations remain, and the distinction is the whole
        method:

        - **A channel** (email, WeChat): the answer has to leave the platform,
          so a transport that is absent, unaddressed or failing is a real
          failure and is reported as one. Never a silent success — that is the
          `worker_cannot_send` defect, where every health check passes and the
          customer simply never hears back.
        - **The platform's own surface** (`/support`): the answer is persisted
          as an agent turn and read back by the page. There is nothing to send,
          so returning "" here is success by construction rather than a
          swallowed failure.

        The outbound idempotency key is derived from the run id, so a retry of
        the same run cannot double-send.
        """
        if channel_system:
            return await self._dispatch_channel(
                run=run,
                draft_text=draft_text,
                ctx=ctx,
                channel_system=channel_system,
                address=channel_address or "",
                conversation_key=channel_conversation_key or "",
            )
        del tenant_id, conversation_ref_id
        return ""

    async def _dispatch_channel(
        self,
        *,
        run: AgentRun,
        draft_text: str,
        ctx: TraceContext,
        channel_system: str,
        address: str,
        conversation_key: str,
    ) -> str:
        """Deliver over the channel the question arrived on (ADR 0014)."""
        sender = self._deps.channel_sender
        if sender is None or not sender.configured(channel_system):  # type: ignore[attr-defined]
            # A receive-only deployment. This must NOT be reported as a
            # successful delivery - that is the `worker_cannot_send` class of
            # defect, where everything looks healthy and the customer hears
            # nothing. It must not fail the run either: a FAILED run never
            # writes the agent turn, so the answer would vanish from the
            # platform too and nobody could even see what had been produced.
            # So it is a *withheld* delivery, recorded and logged.
            logger.warning(
                "outbound_channel_not_configured",
                ctx,
                run_id=str(run.id),
                channel=channel_system,
            )
            return "OUTBOUND_NOT_CONFIGURED"
        if not address:
            # We know the channel and cannot address it. That is a
            # misconfiguration rather than a withheld delivery, so it fails.
            return "OUTBOUND_TARGET_MISSING"
        try:
            result = await sender.send_message(  # type: ignore[attr-defined]
                system=channel_system,
                address=address,
                conversation_key=conversation_key,
                content=draft_text,
                # Same rule as the Chatwoot path: one run, one command id, so a
                # retry of this run cannot deliver the same answer twice.
                command_id=f"run:{run.id}",
            )
        except Exception as exc:  # noqa: BLE001 - mapped to a retryable outcome
            logger.error("outbound_channel_failed", ctx, error_code=type(exc).__name__)
            return "OUTBOUND_FAILED"
        if getattr(result, "ambiguous", False):
            # Outcome unknown: retry idempotently later; never claim success.
            return "OUTBOUND_AMBIGUOUS"
        return ""

    async def _adopt_or_create_run(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_ref_id: uuid.UUID,
        route: str,
        ctx: TraceContext,
        context: CompactedContext | None,
        detection: IntentDetection | None,
        rewritten: bool,
        retrieval_query: str,
        question: str,
    ) -> AgentRun:
        """One row per logical run: adopt the queued placeholder, or create one.

        The enqueue endpoint writes a `queued` row so the caller gets an id and
        the request counts against quota the moment it is accepted. This used to
        insert a **second** row for the same request, leaving the first `queued`
        forever with `input_hash=''`. Nothing advanced it -- a capability with no
        consumer -- and because the placeholder also set `started_at`, which is
        the quota predicate, `usage_snapshot` counted both. Measured before the
        fix: 434 rows counted against 171 real runs, and for one tenant in one
        month 30 placeholders against 12 real runs, so quota read 42 instead of
        12 and the tenant would have been cut off at roughly a third of its
        actual allowance.

        Adoption is claimed with `FOR UPDATE SKIP LOCKED` plus the status
        transition, so the row leaves `queued` exactly once even with several
        workers running. Oldest first, so the first request accepted is the first
        run executed.

        A placeholder that is never adopted -- the request was dropped, or it
        came from a path that does not execute -- stays `queued`, which is now an
        honest state meaning "accepted, never executed" rather than a duplicate
        of a run that did happen.

        A `superseded` placeholder is adopted too, and returned untouched. It
        was already settled by `hand_off_to_human_queue` when the conversation
        left the AI, so there is nothing to execute and nothing to overwrite -
        but its inbox event is still in the queue and still arriving here. If
        adoption only looked for `queued`, that event would find no placeholder
        and create a *second* run for a question that already has one, so a
        twenty-message burst produced 39 run rows for 20 turns. Returning the
        row as-is is what keeps one logical run to one row.
        """
        placeholder = (
            await self._session.execute(
                select(AgentRun)
                .where(
                    AgentRun.tenant_id == tenant_id,
                    AgentRun.conversation_ref_id == conversation_ref_id,
                    AgentRun.status.in_((RunStatus.QUEUED.value, RunStatus.SUPERSEDED.value)),
                    AgentRun.input_hash == "",
                )
                .order_by(AgentRun.started_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()

        if placeholder is not None and placeholder.status == RunStatus.SUPERSEDED.value:
            # Already has its outcome. Overwriting `status` would resurrect it,
            # and writing `started_at` would report a run that never began.
            return placeholder

        # What the *enqueue* path recorded about this run's purpose, read before
        # the overwrite below destroys it.
        #
        # This is the actual reason `internal_draft` never worked, and it is
        # worse than "nothing reads it": `queue_agent_run` writes
        # `model_config={"mode": mode}`, and adoption replaced `model_config`
        # wholesale with the lineage snapshot - so the mode was not merely
        # ignored, it was erased before any executor could have seen it. An
        # operator asking for a draft got a customer-visible message, and the
        # field that would have said otherwise no longer existed.
        requested_mode = ""
        if placeholder is not None and isinstance(placeholder.model_config, dict):
            requested_mode = str(placeholder.model_config.get("mode") or "")

        # `started_at` is rewritten to the moment execution actually begins.
        # The dashboard windows over it, and the placeholder's value was the
        # enqueue time; keeping that would report latency that never happened.
        values: dict[str, Any] = {
            "route": route,
            "status": RunStatus.RUNNING.value,
            "started_at": int(time.time()),
            "model_config": self._model_config(
                context=context, detection=detection, mode=requested_mode
            ),
            "retrieval_config": self._retrieval_config(
                rewritten=rewritten, retrieval_query=retrieval_query
            ),
            "policy_version": self._policy_version,
            "code_version": self._code_version,
            "trace_id": ctx.trace_id,
            "input_hash": hashlib.sha256(question.encode()).hexdigest(),
            "token_usage": {},
        }
        if placeholder is not None:
            for field, value in values.items():
                setattr(placeholder, field, value)
            run = placeholder
        else:
            run = AgentRun(
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                **values,
            )
            self._session.add(run)
        _lineage_generator = self._generator_override or self._deps.generator
        if _lineage_generator is not None:
            run.prompt_version_id = await _get_or_create_prompt(
                self._session, tenant_id, _lineage_generator
            )
        await self._session.flush()
        return run

    def _model_config(
        self,
        *,
        context: CompactedContext | None = None,
        detection: IntentDetection | None = None,
        mode: str = "",
    ) -> dict[str, Any]:
        """Versioned lineage for this run, plus the multi-turn audit snapshot.

        `docs/agent.md` requires every run to record prompt, model, retrieval,
        policy, tool schema and code versions so an answer can be reproduced.
        The conversation block is added here rather than in a new column
        because it is *reproduction input*, exactly like the retrieval config:
        two runs that differ in what context the model saw are not the same
        run, and the difference has to be visible somewhere that survives.

        Text is deliberately absent — `CompactedContext.as_dict` reports counts
        and keys only. The audit log owns what was said; duplicating customer
        content here would create a second thing to redact and a second thing
        to retain.
        """
        gen = self._deps.generator
        config: dict[str, Any] = {
            "model": getattr(self._deps.extra.get("chat"), "_chat_model", "unknown")
            if self._deps.extra
            else "unknown",
            "prompt_template": gen.template.name if gen else "",
            "prompt_version": gen.template.version if gen else 0,
            "temperature": 0.0,
        }
        if mode:
            # Carried through from the enqueue path - see `_adopt_or_create_run`
            # for why it has to be re-stated here rather than surviving the
            # overwrite.
            config["mode"] = mode
        if self._experiments:
            # Arm names, not weights: the results endpoint must read what
            # actually happened rather than re-bucket history, because
            # re-deriving would silently re-bucket every past run the moment a
            # weight changed.
            config["experiments"] = {k: v.name for k, v in self._experiments.items()}
        if detection is not None:
            config["intent"] = detection.as_dict()
        if context is not None:
            config["conversation"] = context.as_dict()
        return config

    async def _flag_enabled(self, key: str, tenant_id: uuid.UUID) -> bool:
        decision = await flag_service.evaluate(
            self._session, flag_key=key, tenant_id=tenant_id, default=False
        )
        return decision.enabled

    @staticmethod
    def _settings() -> Any:
        from platform_core.config import get_settings

        return get_settings()

    def _top_k_for_scene(self, scene: Scene) -> int:
        """Scene-tiered evidence breadth (plan 1.7).

        Policy and pre-sales questions are answered by *combining* passages,
        so they want the widest evidence set; a technical fault question is
        usually decided by one error-code page, where extra candidates only
        dilute the prompt and the rerank budget.
        """
        cfg = self._settings()
        if scene in (Scene.BILLING, Scene.PRE_SALES, Scene.ACCOUNT_SECURITY):
            return int(cfg.retrieval_top_k_policy)
        if scene is Scene.TECHNICAL_SUPPORT:
            return int(cfg.retrieval_top_k_technical)
        return int(cfg.retrieval_top_k)

    @staticmethod
    def _metadata_filter_for(question: str) -> MetadataFilter | None:
        """Metadata constraints from entities the question names explicitly.

        Only phrases that *name* a dimension ("model EC-504", "firmware 2.3.1")
        produce a filter — a bare identifier elsewhere in the sentence is far
        too weak a signal to narrow retrieval on, and a wrongly narrowed
        retrieval is how the platform confidently answers from the wrong
        variant. No mention, no filter: wide recall is the safe default
        (plan 1.5's key trade-off).
        """
        values: dict[str, str] = {}
        model = _MODEL_MENTION.search(question)
        if model:
            values["model"] = model.group(1).lower()
        firmware = _FIRMWARE_MENTION.search(question)
        if firmware:
            values["firmware_version"] = firmware.group(1).lower().rstrip(".")
        if not values:
            return None
        try:
            return build_metadata_filter(values)
        except ValueError:
            return None

    def _retrieval_config(
        self, *, rewritten: bool = False, retrieval_query: str = ""
    ) -> dict[str, Any]:
        """Retrieval lineage.

        `query_rewritten` and `rewritten_query` are recorded because a
        retrieval miss is otherwise unexplainable: given the same corpus and
        the same customer question, a run that searched for something else
        looks like a retrieval bug when it was a context decision.
        """
        config: dict[str, Any] = {
            "strategy": "hybrid_rrf",
            "embedder": type(self._deps.embedder).__name__ if self._deps.embedder else "none",
            "top_k": self._top_k,
        }
        if rewritten:
            config["query_rewritten"] = True
            config["rewritten_query"] = retrieval_query[:255]
        return config
