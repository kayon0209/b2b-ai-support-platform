"""Agent runtime orchestrator (tickets 17-18, docs/agent.md).

Assembles the documented request pipeline into one deterministic flow:

  resolve lease -> minimize -> classify -> route -> retrieve authorized
  evidence -> generate draft -> validate citations -> RECHECK LEASE
  -> send via Chatwoot -> persist run/citations/audit/metrics

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
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from observability import JsonLogger, TraceContext, new_trace_context
from observability_metrics import get_metrics
from platform_core.agent_runtime.confirmation import is_confirmation
from platform_core.agent_runtime.conversation import (
    CompactedContext,
    ConversationMemory,
    Turn,
    needs_clarification,
    normalize_colloquial,
    rewrite_query,
)
from platform_core.agent_runtime.generator import LlmAnswerGenerator
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
    AgentRun,
    Citation,
    RunStatus,
)
from platform_core.agent_runtime.models import (
    PromptTemplate as PromptVersionRow,
)
from platform_core.agent_runtime.qa_path import (
    ABSTAIN_CLARIFICATION,
    ABSTAIN_CONFLICT,
    ABSTAIN_HUMAN_REQUIRED,
    ABSTAIN_OUT_OF_SCOPE,
    ABSTAIN_SENSITIVE_REQUEST,
    AbstentionDecision,
    DraftAnswer,
    claim_contradiction_candidates,
    decide_abstention,
    excerpt_hash,
    redline_violations,
    safe_abstention_text,
    validate_citations,
)
from platform_core.audit import service as audit_service
from platform_core.identity import lease_service
from platform_core.identity.control_lease import LeaseConflict
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
    live provider, Chatwoot or Redis."""

    embedder: Embedder | None = None
    generator: LlmAnswerGenerator | None = None
    sender: object | None = None  # ChatwootClient-compatible send_message
    reader: object | None = None  # ChatwootClient-compatible fetch_message
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


def _non_answerable_reason(route: str, restricted_query: bool) -> str:
    """The abstention reason for a route that never reaches retrieval.

    Distinguishing the reasons is what makes the handoff explainable
    afterwards. They are all "do not answer" but they are not the same event:
    a customer asking for a person is a routing request being honoured, while a
    sensitive request is a disclosure being refused, and an operator reading
    the audit log needs to tell them apart.
    """
    if restricted_query or route == Route.SENSITIVE.value:
        return ABSTAIN_SENSITIVE_REQUEST
    if route == Route.HUMAN_REQUIRED.value:
        return ABSTAIN_HUMAN_REQUIRED
    if route == Route.OUT_OF_SCOPE.value:
        return ABSTAIN_OUT_OF_SCOPE
    return ABSTAIN_HUMAN_REQUIRED


CLARIFICATION_KEEP_LEASE = "clarification"


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

    async def run(
        self,
        *,
        tenant_id: uuid.UUID,
        conversation_ref_id: uuid.UUID,
        question: str,
        principal: PrincipalScope,
        trace: TraceContext | None = None,
        chatwoot_account_id: str | None = None,
        chatwoot_conversation_id: str | None = None,
        expected_lease_version: int | None = None,
        restricted_query: bool = False,
        history: list[Turn] | None = None,
        context_budget_chars: int | None = None,
        known_facts: list[tuple[str, str]] | None = None,
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
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
                expected_lease_version=expected_lease_version,
                restricted_query=restricted_query,
                history=history,
                context_budget_chars=context_budget_chars,
                known_facts=known_facts,
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
        chatwoot_account_id: str | None,
        chatwoot_conversation_id: str | None,
        expected_lease_version: int | None,
        restricted_query: bool,
        history: list[Turn] | None,
        context_budget_chars: int | None,
        known_facts: list[tuple[str, str]] | None,
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
        run_span.set_attributes(
            route=route,
            intent_scene=detection.scene.value,
            intent_kind=detection.primary_kind.value,
            intent_confidence=round(detection.confidence, 3),
            intent_multi=detection.multi_intent,
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

        run = AgentRun(
            tenant_id=tenant_id,
            conversation_ref_id=conversation_ref_id,
            route=route,
            status=RunStatus.RUNNING.value,
            # The quality dashboard windows over `started_at`; a run without it
            # is invisible to every metric and only shows up in `untimed_runs`.
            started_at=int(time.time()),
            model_config=self._model_config(context=context, detection=detection),
            retrieval_config=self._retrieval_config(
                rewritten=rewritten, retrieval_query=retrieval_query
            ),
            policy_version=self._policy_version,
            code_version=self._code_version,
            trace_id=ctx.trace_id,
            input_hash=hashlib.sha256(question.encode()).hexdigest(),
            token_usage={},
        )
        if self._deps.generator is not None:
            run.prompt_version_id = await _get_or_create_prompt(
                self._session, tenant_id, self._deps.generator
            )
        self._session.add(run)
        await self._session.flush()

        # --- 2. Restricted and non-knowledge routes never reach the model. ---
        if restricted_query or route in PRE_RETRIEVAL_ROUTES:
            return await self._finish_abstain(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                decision=AbstentionDecision(
                    abstain=True,
                    reason_code=_non_answerable_reason(route, restricted_query),
                    handoff=True,
                ),
                ctx=ctx,
                started=started,
                question=question,
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
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
                    chatwoot_account_id=chatwoot_account_id,
                    chatwoot_conversation_id=chatwoot_conversation_id,
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
                    chatwoot_account_id=chatwoot_account_id,
                    chatwoot_conversation_id=chatwoot_conversation_id,
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
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
            )

        # --- 2c. Business read tools (plan 3.2/3.4; flag off by default). ---
        #
        # Live data (order status, shipment tracking, invoices, case state)
        # is decided BEFORE the knowledge path: if a read tool answers it,
        # retrieval never runs, so a stale indexed copy cannot leak into the
        # answer. The attempt returns either customer-safe evidence (tool
        # receipts) or a finished RunOutcome (handoff) - never a guess.
        tool_evidence: list[RetrievedChunk] = []
        if route == Route.BUSINESS_READ.value and await self._flag_enabled(
            settings.flag_business_read_tools, tenant_id
        ):
            read_result = await self._attempt_business_read(
                run=run,
                tenant_id=tenant_id,
                question=question,
                detection=detection,
                ctx=ctx,
                started=started,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
            )
            if isinstance(read_result, RunOutcome):
                return read_result
            tool_evidence = read_result

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
                        chatwoot_account_id=chatwoot_account_id,
                        chatwoot_conversation_id=chatwoot_conversation_id,
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
                    chatwoot_account_id=chatwoot_account_id,
                    chatwoot_conversation_id=chatwoot_conversation_id,
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
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
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
                    chatwoot_account_id=chatwoot_account_id,
                    chatwoot_conversation_id=chatwoot_conversation_id,
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
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
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
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
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
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
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
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
            )
        # Red-line guard (huqiu research difficulty 4, flag off by default):
        # a draft that commits the company to a price, a delivery date, a
        # liability or a compensation amount is a commercial promise no one
        # authorised. Deterministic scan, so it is auditable; the flag lets
        # precision be measured before it ever blocks a send.
        if (
            validation.ok
            and redline_violations(draft.text)
            and await self._flag_enabled(self._settings().flag_redline_guard, tenant_id)
        ):
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
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
            )
        if not validation.ok:
            return await self._finish_abstain(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                decision=AbstentionDecision(
                    abstain=True, reason_code=validation.reason_code, handoff=True
                ),
                ctx=ctx,
                started=started,
                question=question,
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
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

        # --- 9. Dispatch through Chatwoot with an idempotency key. ---
        send_error = await self._dispatch(
            run=run,
            tenant_id=tenant_id,
            draft_text=draft.text,
            ctx=ctx,
            chatwoot_account_id=chatwoot_account_id,
            chatwoot_conversation_id=chatwoot_conversation_id,
            conversation_ref_id=conversation_ref_id,
        )
        if send_error:
            run.status = RunStatus.FAILED.value
            run.latency_ms = int((time.monotonic() - started) * 1000)
            await self._session.flush()
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
            draft = await self._deps.generator.generate(
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
        chatwoot_account_id: str | None,
        chatwoot_conversation_id: str | None,
    ) -> RunOutcome | list[RetrievedChunk]:
        """Attempt the read-tool path for a BUSINESS_READ run (plan 3.2/3.4).

        Returns tool-receipt evidence on success, or a finished RunOutcome
        that hands off - the two outcomes, never a fallback that pretends the
        knowledge corpus holds live data (ADR 0006). Every decision point is
        audited: selection, proposal, execution.
        """
        import json as _json

        from platform_core.tool_gateway.gateway import ToolGateway, ToolGatewayError
        from platform_core.tool_gateway.registry import ConnectorExecutorResolver
        from platform_core.tool_gateway.selector import select_read_tools
        from platform_policy import Action, Decision, PolicyEngine, Principal

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
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
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
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
            )
        chosen = usable[0]
        arguments = _extract_tool_args(chosen.tool_name, question)
        if arguments is None:
            return await self._handoff_for_tool(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                reason_code="TOOL_ARGUMENT_MISSING",
                ctx=ctx,
                started=started,
                question=question,
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
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
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
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
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
            )

        output = execution.sanitized_output or {}
        receipt_json = _json.dumps(output, sort_keys=True, ensure_ascii=False, default=str)
        ref_value = next(iter(arguments.values()), "")
        receipt_id = uuid.uuid5(tenant_id, f"receipt:{chosen.tool_name}:{idempotency_key}")
        receipt = RetrievedChunk(
            chunk_id=receipt_id,
            document_version_id=None,
            title=f"tool://{chosen.tool_name}",
            section_path=[],
            excerpt=receipt_json[:280],
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
        return [receipt]

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
        chatwoot_account_id: str | None,
        chatwoot_conversation_id: str | None,
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
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
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
        chatwoot_account_id: str | None,
        chatwoot_conversation_id: str | None,
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
            chatwoot_account_id=chatwoot_account_id,
            chatwoot_conversation_id=chatwoot_conversation_id,
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
        chatwoot_account_id: str | None,
        chatwoot_conversation_id: str | None,
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

        notice = safe_abstention_text(decision.reason_code)

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
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
                conversation_ref_id=conversation_ref_id,
            )
            # Evidence-carrying handoff (plan 5.4): the receiving agent gets
            # the reason code and the evidence the run gathered, as a PRIVATE
            # note - the customer never sees it, and the agent does not have
            # to reconstruct "why did the bot give up" from the audit log.
            if not send_error and decision.handoff and self._settings().handoff_evidence_enabled:
                await self._send_handoff_note(
                    run=run,
                    question=question,
                    reason_code=decision.reason_code,
                    ctx=ctx,
                    chatwoot_account_id=chatwoot_account_id,
                    chatwoot_conversation_id=chatwoot_conversation_id,
                )

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
            await lease_service.release_to_queue(
                self._session,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                reason=f"abstain:{decision.reason_code}",
            )

        await audit_service.record(
            self._session,
            ctx=TenantContext(tenant_id=tenant_id, actor_id=None, actor_kind="service"),
            action="agent_run.abstained",
            resource_type="agent_run",
            resource_id=run.id,
            decision="abstained",
            reason_code=decision.reason_code[:63],
            after={"handoff": decision.handoff, "notice_sent": not send_error},
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

    async def _send_handoff_note(
        self,
        *,
        run: AgentRun,
        question: str,
        reason_code: str,
        ctx: TraceContext,
        chatwoot_account_id: str | None,
        chatwoot_conversation_id: str | None,
    ) -> None:
        """Private note for the receiving agent: reason + evidence refs."""
        if self._deps.sender is None or not chatwoot_account_id or not chatwoot_conversation_id:
            return
        # No raw content: the hash links to the audit log, which is the
        # record a reviewer is allowed to read.
        note = (
            f"[handoff] reason_code={reason_code}"
            f" | question_hash={getattr(run, 'input_hash', '')}"
            f" | run_id={run.id}"
        )
        try:
            await self._deps.sender.send_message(  # type: ignore[attr-defined]
                account_id=chatwoot_account_id,
                conversation_id=chatwoot_conversation_id,
                content=note,
                command_id=f"run:{run.id}:note",
                private=True,
            )
        except Exception as exc:  # noqa: BLE001 - the note is best-effort
            logger.warning("handoff_note_failed", ctx, error_code=type(exc).__name__)

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
        chatwoot_account_id: str | None,
        chatwoot_conversation_id: str | None,
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
        """
        run.status = RunStatus.ABSTAINED.value
        run.abstain_reason = ABSTAIN_CLARIFICATION[:127]
        run.latency_ms = int((time.monotonic() - started) * 1000)
        await self._session.flush()

        notice = safe_abstention_text(ABSTAIN_CLARIFICATION)

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
                chatwoot_account_id=chatwoot_account_id,
                chatwoot_conversation_id=chatwoot_conversation_id,
                conversation_ref_id=conversation_ref_id,
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
        chatwoot_account_id: str | None,
        chatwoot_conversation_id: str | None,
        conversation_ref_id: uuid.UUID,
    ) -> str:
        """Send the customer-visible reply. Returns "" on success, else a
        reason code. The outbound idempotency key is derived from the run id
        so a retry of the same run cannot double-send."""
        if self._deps.sender is None:
            # No transport wired (unit/local): treat as un-sent, not failed.
            return ""
        if not chatwoot_account_id:
            # No Chatwoot account means the conversation is not backed by
            # Chatwoot at all - the customer typed into the platform's own
            # chat surface, and that surface is the delivery channel. This is
            # not a failed send, and treating it as one is what made every
            # such answer vanish: the run was marked FAILED even though the
            # answer had been produced, and the agent turn is only written
            # for a COMPLETED run, so nothing was ever persisted or shown.
            #
            # Note that `conversation_id` is present on these events too. It
            # is the external correlation id the conversation ref is derived
            # from, not evidence that a Chatwoot conversation exists to send
            # to - the account id is what says the conversation is Chatwoot's.
            return ""
        if not chatwoot_conversation_id:
            # An account with no conversation is a misconfiguration: we did
            # mean to reach Chatwoot and cannot. That has to fail rather than
            # report a success nobody can observe.
            return "OUTBOUND_TARGET_MISSING"

        command_id = f"run:{run.id}"
        try:
            result = await self._deps.sender.send_message(  # type: ignore[attr-defined]
                account_id=chatwoot_account_id,
                conversation_id=chatwoot_conversation_id,
                content=draft_text,
                command_id=command_id,
            )
        except Exception as exc:  # noqa: BLE001 - mapped to a retryable outcome
            logger.error("outbound_failed", ctx, error_code=type(exc).__name__)
            return "OUTBOUND_FAILED"

        if getattr(result, "ambiguous", False):
            # Outcome unknown: retry idempotently later; never claim success.
            return "OUTBOUND_AMBIGUOUS"
        del tenant_id, conversation_ref_id
        return ""

    def _model_config(
        self,
        *,
        context: CompactedContext | None = None,
        detection: IntentDetection | None = None,
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
