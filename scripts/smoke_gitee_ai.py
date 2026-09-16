"""Live smoke test against Gitee AI. Requires APP_LLM_API_KEY. Not part of CI.

Verifies the three capabilities the M1 slice depends on and, crucially, that
the citation contract holds end to end: a grounded question must produce
claims citing the chunk that was actually in context, and an unanswerable
question must produce nothing for the validator to approve.

Run:  ./.venv/Scripts/python.exe scripts/smoke_gitee_ai.py
"""

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.getcwd(), "apps/api/src"))
sys.path.insert(0, os.path.join(os.getcwd(), "packages/observability/src"))
sys.path.insert(0, os.path.join(os.getcwd(), "packages/policy/src"))

from platform_core.agent_runtime.generator import LlmAnswerGenerator  # noqa: E402
from platform_core.agent_runtime.qa_path import validate_citations  # noqa: E402
from platform_core.config import Settings  # noqa: E402
from platform_core.llm.gitee_ai import GiteeAiClient  # noqa: E402
from platform_core.llm.provider import ChatMessage  # noqa: E402
from platform_core.retrieval.hybrid import RetrievedChunk  # noqa: E402


def _chunk(text: str, title: str = "Refund Policy") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_version_id=uuid.uuid4(),
        title=title,
        section_path=["support", "refunds"],
        excerpt=text,
        source_uri="minio://knowledge/refund-policy.md",
        score=0.81,
    )


async def main() -> int:
    settings = Settings()
    if settings.llm_api_key is None:
        print("SKIP: APP_LLM_API_KEY is not set")
        return 0

    client = GiteeAiClient()
    failures = 0

    # --- 1. Chat. qwen3.8-flash is a reasoning model: the thinking channel
    # must be separated from the answer. ---
    result = await client.complete(
        [ChatMessage(role="user", content="Reply with exactly: OK")], max_tokens=64
    )
    chat_ok = bool(result.text.strip())
    print(
        f"[1] chat     model={result.model!r} text={result.text.strip()[:60]!r} -> "
        f"{'OK' if chat_ok else 'FAIL'}"
    )
    failures += 0 if chat_ok else 1

    # --- 2. Embeddings. Width must match the vector(1536) column, otherwise
    # the insert would corrupt or fail silently at the schema boundary. ---
    emb = await client.embed(["refund window is 30 days"])
    dim = len(emb.vectors[0]) if emb.vectors else 0
    emb_ok = dim == settings.llm_embedding_dimensions and len(emb.vectors) == 1
    print(
        f"[2] embed    dims={dim} expected={settings.llm_embedding_dimensions} -> "
        f"{'OK' if emb_ok else 'FAIL'}"
    )
    failures += 0 if emb_ok else 1

    # --- 3. Rerank. The relevant passage must come back first. ---
    docs = [
        "Annual plans may be refunded within 30 days of purchase.",
        "Our shipping hours are 9am to 5pm local time.",
        "Contact support for billing questions.",
    ]
    rr = await client.rerank("how long is the refund window?", docs, top_n=3)
    rerank_ok = bool(rr) and rr[0].index == 0
    print(
        f"[3] rerank   hits={[(h.index, round(h.relevance_score, 3)) for h in rr]} -> "
        f"{'OK' if rerank_ok else 'FAIL'}"
    )
    failures += 0 if rerank_ok else 1

    # --- 4. Citation contract: a grounded question yields citable claims. ---
    generator = LlmAnswerGenerator(client)
    grounded = _chunk("The refund window is 30 days for annual plans.")
    draft = await generator.generate("How long is the refund window?", [grounded])
    validation = validate_citations(draft, [grounded])
    cites_ok = validation.ok and draft.claims.get(0) == [grounded.chunk_id]
    print(
        f"[4] cite     claims={draft.claims} validate={validation.ok} "
        f"reason={validation.reason_code!r} -> {'OK' if cites_ok else 'FAIL'}"
    )
    failures += 0 if cites_ok else 1

    # --- 5. Abstention: an unanswerable question must not be publishable. ---
    unrelated = _chunk("Our shipping hours are 9am to 5pm local time.")
    draft2 = await generator.generate("What is the capital of the moon?", [unrelated])
    v2 = validate_citations(draft2, [unrelated])
    abstain_ok = not v2.ok
    print(
        f"[5] abstain  claims={draft2.claims} validate={v2.ok} "
        f"reason={v2.reason_code!r} -> {'OK' if abstain_ok else 'FAIL'}"
    )
    failures += 0 if abstain_ok else 1

    print()
    print("RESULT:", "ALL PASS" if failures == 0 else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop))
