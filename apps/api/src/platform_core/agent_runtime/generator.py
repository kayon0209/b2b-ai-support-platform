"""LLM-backed AnswerGenerator (tickets 17-18).

Implements the `qa_path.AnswerGenerator` Protocol. The model proposes text
and per-claim citation ids; `qa_path.validate_citations()` remains the
authority on whether those citations are admissible. If the model returns
something unparseable we return an empty DraftAnswer, which
validate_citations rejects as NO_CLAIMS — degrading to abstention rather
than emitting an unverifiable answer.

Context minimization (docs/security.md): excerpts are truncated to a
character budget and PII-redacted before leaving the process. The full
conversation is never sent.
"""

import json
import re
import uuid
from typing import Any

from platform_core.agent_runtime.conversation import CompactedContext
from platform_core.agent_runtime.prompts import (
    KNOWLEDGE_QA_PROMPT,
    PromptTemplate,
    format_conversation,
    format_evidence,
)
from platform_core.agent_runtime.qa_path import DraftAnswer
from platform_core.evaluation.pii import redact_text
from platform_core.llm.provider import ChatMessage, ChatProvider, ChatResult, ProviderRole
from platform_core.retrieval.hybrid import RetrievedChunk

# Per-excerpt and total budgets keep the prompt inside provider limits and
# limit how much customer-derived text crosses the model boundary.
MAX_EXCERPT_CHARS = 700
MAX_TOTAL_EVIDENCE_CHARS = 6000
MAX_ANSWER_TOKENS = 900


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model response.

    Reasoning models sometimes wrap JSON in prose or code fences; we take
    the outermost brace pair and parse it, returning None on any failure so
    the caller abstains instead of guessing.
    """
    text = raw.strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class LlmAnswerGenerator:
    """Evidence-grounded generator over a provider-neutral ChatProvider."""

    def __init__(
        self,
        chat: ChatProvider,
        *,
        template: PromptTemplate | None = None,
        max_tokens: int = MAX_ANSWER_TOKENS,
        fallback_model: str | None = None,
    ) -> None:
        self._chat = chat
        self._template = template or KNOWLEDGE_QA_PROMPT
        self._max_tokens = max_tokens
        # Model fallback chain (plan 5.5): primary -> fallback -> propagate.
        # Off unless the deployment configures a fallback model; a retry on
        # the SAME model is not a fallback, it is more of the outage.
        self._fallback_model = fallback_model

    @property
    def template(self) -> PromptTemplate:
        """Exposed so the orchestrator can persist prompt lineage."""
        return self._template

    def with_template(self, template: PromptTemplate) -> "LlmAnswerGenerator":
        """A copy bound to a different prompt template, sharing the provider.

        A copy rather than a mutation. This instance is built once per worker and
        shared across concurrent runs, so assigning `_template` for one run would
        change the prompt of every other run in flight - an A/B experiment whose
        arms leak into each other is worse than no experiment, because it
        produces a number that looks like a result.

        The provider is passed through rather than re-derived, so the copy shares
        the circuit breaker and the connection pool with the original.
        """
        return LlmAnswerGenerator(
            self._chat,
            template=template,
            max_tokens=self._max_tokens,
            fallback_model=self._fallback_model,
        )

    def _build_evidence(self, evidence: list[RetrievedChunk]) -> tuple[str, dict[str, str]]:
        """Render evidence and build the id -> chunk_id resolution map.

        The model only ever sees sanitized excerpt text plus the citation id
        it must echo back; the mapping from that id to the real chunk UUID
        stays server-side.
        """
        pairs: list[tuple[str, str]] = []
        resolve: dict[str, str] = {}
        total = 0
        for chunk in evidence:
            safe_excerpt, _ = redact_text(chunk.excerpt)
            excerpt = safe_excerpt[:MAX_EXCERPT_CHARS]
            if total + len(excerpt) > MAX_TOTAL_EVIDENCE_CHARS:
                break
            total += len(excerpt)
            citation_id = str(chunk.chunk_id)
            pairs.append((citation_id, excerpt))
            resolve[citation_id] = str(chunk.chunk_id)
        return format_evidence(pairs), resolve

    def _build_conversation(
        self,
        context: CompactedContext | None,
        retrieval_query: str,
        *,
        question: str,
    ) -> str:
        """Render the conversation block, redacted and bounded.

        Redaction happens *after* rendering rather than per-turn: the
        compaction step already chose what to keep, and redacting the rendered
        block is one pass over exactly the text that will leave the process.

        The rewritten-query note is appended inside the block rather than
        merged into the question so the model can see the difference between
        what the customer typed and what was searched for. Those are different
        things and an answer that silently answers the second is the failure
        mode this makes visible.
        """
        if context is None:
            return ""
        rendered = context.render()
        if not rendered.strip():
            return ""
        if retrieval_query.strip() and retrieval_query.strip() != question.strip():
            rendered = f"{rendered}\nSearched for: {retrieval_query.strip()}"
        safe_block, _ = redact_text(rendered)
        return format_conversation(safe_block[:MAX_TOTAL_EVIDENCE_CHARS])

    async def generate(
        self,
        question: str,
        evidence: list[RetrievedChunk],
        *,
        context: CompactedContext | None = None,
        retrieval_query: str = "",
    ) -> DraftAnswer:
        """Return a DraftAnswer with claims mapped to real chunk ids.

        Never raises on model misbehaviour: a malformed response yields an
        empty DraftAnswer, and the abstention/validation layer decides what
        the customer sees.

        `context` is the compressed conversation. It is redacted like the
        question is — the redaction boundary applies to everything crossing the
        model boundary, and conversation text is the most likely place for a
        customer to have pasted a card number three turns ago.

        `retrieval_query` is the *rewritten* query, when there is one. It is
        carried so the model can be told what was actually searched for when
        the rewrite changed the subject; without it, a rewritten query produces
        evidence about something the customer did not name and the model has no
        way to reconcile the two.
        """
        if not evidence:
            return DraftAnswer(text="", claims={}, route="knowledge_qa")

        evidence_block, resolve = self._build_evidence(evidence)
        safe_question, _ = redact_text(question)
        conversation_block = self._build_conversation(context, retrieval_query, question=question)
        prompt = self._template.render(
            evidence=evidence_block,
            question=safe_question[:MAX_EXCERPT_CHARS],
            conversation=conversation_block,
        )
        from platform_core.llm.provider import ModelError

        messages = [
            ChatMessage(ProviderRole.SYSTEM, "You answer strictly from provided evidence."),
            ChatMessage(ProviderRole.USER, prompt),
        ]
        try:
            result = await self._chat.complete(
                messages,
                max_tokens=self._max_tokens,
                temperature=0.0,
            )
        except ModelError:
            if self._fallback_model is None:
                raise
            from observability_metrics import get_metrics as _gm

            _gm().model_fallback_total.inc()
            result = await self._chat.complete(
                messages,
                max_tokens=self._max_tokens,
                temperature=0.0,
                model=self._fallback_model,
            )

        # Computed before parsing: a malformed response still spent tokens.
        usage = self._usage_of(result)

        parsed = _extract_json_object(result.text)
        if parsed is None:
            return DraftAnswer(text="", claims={}, route="knowledge_qa", usage=usage)

        raw_claims = parsed.get("claims")
        if not isinstance(raw_claims, list):
            return DraftAnswer(text="", claims={}, route="knowledge_qa", usage=usage)

        claim_texts: list[str] = []
        claims: dict[int, list[uuid.UUID]] = {}
        for item in raw_claims:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            cited = item.get("citations")
            chunk_ids: list[uuid.UUID] = []
            if isinstance(cited, list):
                for citation_id in cited:
                    # Drop any id the model invented: unresolved ids are not
                    # evidence, and validate_citations would reject the claim.
                    resolved = resolve.get(str(citation_id))
                    if resolved is None:
                        continue
                    try:
                        chunk_ids.append(uuid.UUID(resolved))
                    except ValueError:
                        continue
            claim_texts.append(text)
            claims[len(claim_texts) - 1] = chunk_ids

        return DraftAnswer(
            text="\n".join(claim_texts),
            claims=claims,
            route="knowledge_qa",
            # Kept, not discarded: ADR 0005's claim-support metric checks each
            # claim against the excerpt it cites, and the joined answer is too
            # coarse to attribute a contradiction to a claim.
            claim_texts=dict(enumerate(claim_texts)),
            usage=usage,
        )

    @staticmethod
    def _usage_of(result: ChatResult) -> dict[str, Any]:
        """Token accounting for the AgentRun.

        Carried through `DraftAnswer` because the AnswerGenerator protocol
        returns only a DraftAnswer. Without it the run's `token_usage` stayed
        `{}` even though the provider reported usage on every call.
        """
        usage: dict[str, Any] = {
            "model": result.model,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
        }
        if result.raw_usage:
            usage["raw"] = result.raw_usage
        return usage
