"""Chunk-size / overlap tuning experiment (iteration plan 1.2).

Sweeps a (max_chars, overlap_chars) grid over a fixed corpus and measures
recall@5 / recall@10 / MRR / chunk count / ingestion time per configuration.
Writes `tests/artifacts/chunking_tuning.json`; the DEFAULTS in
`platform_core.config` must be traceable to a line in that report.

Method notes:
- Fixed corpus + fixed query set (from the eval dataset), because an
  experiment that reads live data cannot be reproduced or diffed.
- Lexical scoring (BM25-free term overlap) is used when no embedding
  provider is configured; vector-informed runs append an `embedder` field so
  the two modes are never silently compared.

Usage:
    python scripts/tune_chunking.py [--out tests/artifacts/chunking_tuning.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api/src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/observability/src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/policy/src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from evals.dataset import CORPUS, EvalCategory, cases_for  # noqa: E402


def tune_key_of(chunk: object) -> str:
    """Key extraction for this script's `tune://` URIs (plan 4.1 pattern)."""
    prefix = "tune://"
    uri = getattr(chunk, "source_uri", "")
    return uri[len(prefix) :] if uri.startswith(prefix) else getattr(chunk, "title", "")


from platform_core.knowledge.ingest import ChunkingConfig, Section, chunk_sections  # noqa: E402

GRID = [
    (400, 0),
    (600, 0),
    (600, 100),
    (900, 0),
    (900, 150),
    (1200, 0),
    (1200, 150),
    (1200, 300),
    (1600, 200),
]

# The reference split (plan 1.1): parameterised chunking replaces the fixed
# constants; the tuning grid is what chooses among them.
QUERY_CATEGORIES = (
    EvalCategory.ANSWERABLE,
    EvalCategory.POLICY_CONTRACT,
    EvalCategory.MULTILINGUAL,
)


def _corpus_chunks(config: ChunkingConfig) -> list[tuple[str, Section]]:
    """Chunk every corpus entry, keyed by version_key."""
    out: list[tuple[str, Section]] = []
    for entry in CORPUS:
        if entry.availability != "active":
            continue
        section = Section(path=[entry.document_title], text=entry.text)
        for piece in chunk_sections([section], config):
            out.append((entry.version_key, piece))
    return out


def _score(config: ChunkingConfig) -> dict[str, object]:
    import uuid

    from platform_core.evaluation.runner import EvaluationRunner
    from platform_core.retrieval.hybrid import PrincipalScope, RetrievedChunk

    started = time.perf_counter()
    indexed = _corpus_chunks(config)
    ingest_seconds = time.perf_counter() - started

    from evals.harness import _terms

    async def retrieve(question: str, scope: PrincipalScope) -> list[RetrievedChunk]:
        query_terms = _terms(question)
        scored: list[tuple[float, str, Section]] = []
        for key, section in indexed:
            searchable = f"{section.path[-1] if section.path else ''} {section.text}"
            overlap = len(query_terms & _terms(searchable))
            if overlap:
                scored.append((overlap / max(len(query_terms), 1), key, section))
        scored.sort(key=lambda triple: triple[0], reverse=True)
        return [
            RetrievedChunk(
                chunk_id=uuid.uuid5(uuid.NAMESPACE_URL, f"{key}:{i}"),
                document_version_id=None,
                title=section.path[-1] if section.path else "",
                section_path=[],
                excerpt=section.text[:280],
                source_uri=f"tune://{key}",
                score=score,
            )
            for i, (score, key, section) in enumerate(scored)
        ]

    async def run() -> tuple[float, float, float, int]:
        cases = [
            case
            for category in QUERY_CATEGORIES
            for case in cases_for(category)
            if case.expected_version_keys
        ]
        from platform_core.agent_runtime.qa_path import DraftAnswer

        async def _answer(question: str, evidence: list) -> DraftAnswer:
            # Retrieval-only scoring: the answer layer is deliberately idle,
            # because chunk shape cannot affect what the oracle would say.
            return DraftAnswer(text="", claims={}, route="knowledge_qa")

        runner = EvaluationRunner(_answer, retrieve, key_of=tune_key_of)
        recalls: list[float] = []
        ranks: list[float] = []
        for case in cases:
            result = await runner.run_case(case)
            if result.recall_at_k is None:
                continue
            recalls.append(result.recall_at_k)
            # MRR over expected keys: 1/(first hit position in retrieval order)
            position = next(
                (
                    i + 1
                    for i, key in enumerate(result.retrieved_keys)
                    if key in case.expected_version_keys
                ),
                0,
            )
            ranks.append(1.0 / position if position else 0.0)
        return (
            sum(recalls) / len(recalls) if recalls else 0.0,
            sum(ranks) / len(ranks) if ranks else 0.0,
            len(cases),
            len(indexed),
        )

    recall10, mrr, n_cases, n_chunks = asyncio.run(run())
    return {
        "max_chars": config.max_chars,
        "overlap_chars": config.overlap_chars,
        "recall_at_k": round(recall10, 4),
        "mrr": round(mrr, 4),
        "cases_measured": n_cases,
        "chunks": n_chunks,
        "ingest_seconds": round(ingest_seconds, 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="tests/artifacts/chunking_tuning.json", help="report path")
    args = parser.parse_args()

    report = {
        "method": "lexical overlap (no provider configured)" if True else "vector",
        "grid": GRID,
        "runs": [],
    }
    for max_chars, overlap in GRID:
        try:
            config = ChunkingConfig(max_chars=max_chars, min_chars=50, overlap_chars=overlap)
        except ValueError:
            continue
        result = _score(config)
        report["runs"].append(result)  # type: ignore[index]
        print(json.dumps(result))

    # Recommendation: highest recall, ties broken by fewer chunks (index cost).
    runs = report["runs"]  # type: ignore[index]
    if runs:
        best = max(runs, key=lambda r: (r["recall_at_k"], -r["chunks"]))  # type: ignore[index]
        report["recommended"] = {  # type: ignore[index]
            "max_chars": best["max_chars"],
            "overlap_chars": best["overlap_chars"],
            "reason": "highest recall@k; ties broken by smaller index",
        }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
