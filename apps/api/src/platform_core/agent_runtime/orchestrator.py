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
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from observability import JsonLogger, TraceContext, new_trace_context
from observability_metrics import get_metrics
from platform_core.agent_runtime.generator import LlmAnswerGenerator
from platform_core.agent_runtime.models import (
    AgentRun,
    Citation,
    RunStatus,
)
from platform_core.agent_runtime.models import (
    PromptTemplate as PromptVersionRow,
)
from platform_core.agent_runtime.qa_path import (
    AbstentionDecision,
    DraftAnswer,
    decide_abstention,
    excerpt_hash,
    safe_abstention_text,
    validate_citations,
)
from platform_core.audit import service as audit_service
from platform_core.identity import lease_service
from platform_core.identity.control_lease import LeaseConflict
from platform_core.identity.tenant_context import TenantContext
from platform_core.llm.provider import ModelError
from platform_core.retrieval.hybrid import Embedder, PrincipalScope, RetrievedChunk

logger = JsonLogger("platform.agent_runtime")

CODE_VERSION = "0.1.0"
POLICY_VERSION = "v1"


class Route(StrEnum):
    """Routing classes from docs/agent.md."""

    KNOWLEDGE_QA = "knowledge_qa"
    HUMAN_REQUIRED = "human_required"
    OUT_OF_SCOPE = "out_of_scope"


# Terms that must never be answered by the AI regardless of evidence
# (docs/agent.md SENSITIVE routing class).
RESTRICTED_TERMS = (
    "password",
    "credential",
    "api key",
    "social security",
    "credit card number",
    "bank account",
)


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
    extra: dict[str, Any] = field(default_factory=dict)


def classify_route(question: str) -> str:
    """Deterministic pre-classification.

    The MVP routes knowledge questions to the QA path and everything that
    looks like a credential or account-ownership request to a human. Richer
    intent classification ships with the evaluation-driven milestones; this
    stays conservative on purpose.
    """
    lowered = question.lower()
    if any(term in lowered for term in RESTRICTED_TERMS):
        return Route.HUMAN_REQUIRED.value
    return Route.KNOWLEDGE_QA.value


async def retrieve_evidence(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    query: str,
    principal: PrincipalScope,
    embedder: Embedder | None,
    top_k: int = 8,
    trace: TraceContext | None = None,
) -> list[RetrievedChunk]:
    """Authorized retrieval. Evidence never crosses tenants: the tenant
    filter and ACL narrowing are applied inside hybrid_search before any
    candidate is scored."""
    from platform_core.retrieval.hybrid import hybrid_search

    started = time.monotonic()
    span = trace.span("retrieval", **{"span.kind": "internal"}) if trace else None
    try:
        chunks = await hybrid_search(
            session,
            tenant_id=tenant_id,
            query=query,
            top_k=top_k,
            principal=principal,
            embedder=embedder,
        )
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
                    document_version_id=chunk.document_version_id,
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
    ) -> RunOutcome:
        """Execute the pipeline for one inbound customer message.

        `expected_lease_version` is the version observed when the run was
        queued. When supplied, the pre-send gate refuses to dispatch if the
        lease has moved — this is what prevents an AI reply racing a human
        takeover.
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
    ) -> RunOutcome:

        # --- 1. Acquire/observe the control lease. ---
        lease = await lease_service.acquire_or_get(
            self._session, tenant_id=tenant_id, conversation_ref_id=conversation_ref_id
        )
        if expected_lease_version is None:
            expected_lease_version = int(lease.lease_version)

        route = classify_route(question)
        run_span.set_attributes(route=route, **{"lease.version": expected_lease_version})

        run = AgentRun(
            tenant_id=tenant_id,
            conversation_ref_id=conversation_ref_id,
            route=route,
            status=RunStatus.RUNNING.value,
            # The quality dashboard windows over `started_at`; a run without it
            # is invisible to every metric and only shows up in `untimed_runs`.
            started_at=int(time.time()),
            model_config=self._model_config(),
            retrieval_config=self._retrieval_config(),
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
        if restricted_query or route == Route.HUMAN_REQUIRED.value:
            return await self._finish_abstain(
                run=run,
                tenant_id=tenant_id,
                conversation_ref_id=conversation_ref_id,
                expected_lease_version=expected_lease_version,
                decision=AbstentionDecision(
                    abstain=True, reason_code="RESTRICTED_REQUEST", handoff=True
                ),
                ctx=ctx,
                started=started,
                question=question,
            )

        # --- 3. Retrieve authorized evidence. ---
        try:
            evidence = await retrieve_evidence(
                self._session,
                tenant_id=tenant_id,
                query=question,
                principal=principal,
                embedder=self._deps.embedder,
                trace=ctx,
            )
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
            )

        # --- 4. Abstention gate before spending a model call. ---
        decision = decide_abstention(question, evidence, restricted_query=restricted_query)
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
            )
        try:
            draft = await self._generate_with_telemetry(ctx, question, evidence)
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
            )

        # --- 6. Validate citations. Unsupported output is not publishable. ---
        validation = validate_citations(draft, evidence)
        get_metrics().citation_validation_total.labels(
            status="supported" if validation.ok else "unsupported"
        ).inc()
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
        self, ctx: TraceContext, question: str, evidence: list[RetrievedChunk]
    ) -> DraftAnswer:
        """Wrap the model call with a span and latency/token/error metrics.

        Kept separate from `run()` so the model boundary is measurable
        without threading timing variables through the pipeline body.

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
            draft = await self._deps.generator.generate(question, evidence)
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
    ) -> RunOutcome:
        """Record abstention, release the lease to the human queue, and
        send the customer-safe notice (still behind the lease gate)."""
        run.status = RunStatus.ABSTAINED.value
        run.abstain_reason = decision.reason_code[:127]
        run.latency_ms = int((time.monotonic() - started) * 1000)
        await self._session.flush()

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
            after={"handoff": decision.handoff},
            trace_id=ctx.trace_id,
        )
        logger.info(
            "run_abstained",
            ctx,
            route=run.route,
            status=run.status,
            reason_code=decision.reason_code,
        )
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
            answer_text=safe_abstention_text(decision.reason_code),
            abstain_reason=decision.reason_code,
            handoff=decision.handoff,
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
        if not chatwoot_account_id or not chatwoot_conversation_id:
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

    def _model_config(self) -> dict[str, Any]:
        gen = self._deps.generator
        return {
            "model": getattr(self._deps.extra.get("chat"), "_chat_model", "unknown")
            if self._deps.extra
            else "unknown",
            "prompt_template": gen.template.name if gen else "",
            "prompt_version": gen.template.version if gen else 0,
            "temperature": 0.0,
        }

    def _retrieval_config(self) -> dict[str, Any]:
        return {
            "strategy": "hybrid_rrf",
            "embedder": type(self._deps.embedder).__name__ if self._deps.embedder else "none",
            "top_k": 8,
        }
