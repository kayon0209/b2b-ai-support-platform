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

from platform_core.agent_runtime.prompts import (
    KNOWLEDGE_QA_PROMPT,
    PromptTemplate,
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
    ) -> None:
        self._chat = chat
        self._template = template or KNOWLEDGE_QA_PROMPT
        self._max_tokens = max_tokens

    @property
    def template(self) -> PromptTemplate:
        """Exposed so the orchestrator can persist prompt lineage."""
        return self._template

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

    async def generate(self, question: str, evidence: list[RetrievedChunk]) -> DraftAnswer:
        """Return a DraftAnswer with claims mapped to real chunk ids.

        Never raises on model misbehaviour: a malformed response yields an
        empty DraftAnswer, and the abstention/validation layer decides what
        the customer sees.
        """
        if not evidence:
            return DraftAnswer(text="", claims={}, route="knowledge_qa")

        evidence_block, resolve = self._build_evidence(evidence)
        safe_question, _ = redact_text(question)
        prompt = self._template.render(
            evidence=evidence_block,
            question=safe_question[:MAX_EXCERPT_CHARS],
        )
        result = await self._chat.complete(
            [
                ChatMessage(ProviderRole.SYSTEM, "You answer strictly from provided evidence."),
                ChatMessage(ProviderRole.USER, prompt),
            ],
            max_tokens=self._max_tokens,
            temperature=0.0,
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
