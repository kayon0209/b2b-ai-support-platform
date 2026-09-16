"""Deterministic retrieval harness for the evaluation dataset.

The dataset must be reproducible: a regression that only reproduces against
live tenant data cannot be debugged. This harness serves `CORPUS` through a
simple lexical ranker, and - importantly - it enforces `availability`
exactly the way the real retrieval gate does:

- `active` entries are returned;
- `expired` entries are withheld, mirroring `dv.expires_at > now` /
  `dv.status = 'active'`;
- `unauthorized` entries are withheld unless the case's principal scope
  explicitly grants them, mirroring the ACL pre-filter.

Enforcing availability here rather than in each assertion is what makes the
expired and unauthorized categories meaningful: the case cannot accidentally
pass by retrieving content it should never have been given.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable

from platform_core.retrieval.hybrid import PrincipalScope, RetrievedChunk

from .dataset import CORPUS, CorpusEntry

RetrieveFn = Callable[[str, PrincipalScope], Awaitable[list[RetrievedChunk]]]

# Principals allowed to see `unauthorized` corpus entries. Nothing in the
# dataset grants this, so the pricing passage stays invisible; the constant
# exists so the rule is explicit and testable rather than a hardcoded `False`.
PRIVILEGED_PRINCIPALS: frozenset[str] = frozenset()

_STOPWORDS = frozenset(
    {"the", "a", "an", "is", "are", "do", "does", "i", "you", "to", "of", "for", "in", "on", "my"}
)


def _terms(text: str) -> set[str]:
    tokens = re.findall(r"\w+", text.lower())
    out: set[str] = set()
    for token in tokens:
        if len(token) <= 2 or token in _STOPWORDS:
            continue
        # Same crude fold the abstention gate uses, so the two agree on
        # what counts as a shared term.
        for suffix in ("s", "es", "ing", "ed"):
            if token.endswith(suffix) and len(token) > len(suffix) + 2:
                token = token[: -len(suffix)]
                break
        out.add(token)
    return out


def _visible(entry: CorpusEntry, scope: PrincipalScope) -> bool:
    if entry.availability == "active":
        return True
    if entry.availability == "expired":
        # The retrieval gate withholds superseded/expired versions outright;
        # there is no principal that unlocks them.
        return False
    if entry.availability == "unauthorized":
        return bool(PRIVILEGED_PRINCIPALS & set(scope.principal_ids))
    raise ValueError(f"unknown availability {entry.availability!r}")


def _chunk(entry: CorpusEntry) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid5(uuid.NAMESPACE_URL, f"eval:{entry.version_key}"),
        document_version_id=uuid.uuid5(uuid.NAMESPACE_URL, f"eval-dv:{entry.version_key}"),
        title=entry.document_title,
        section_path=[],
        excerpt=entry.text,
        source_uri=f"minio://eval/{entry.version_key}",
        score=0.0,
    )


def make_retriever() -> RetrieveFn:
    """Return `retrieve_fn(question, scope) -> list[RetrievedChunk]`.

    Ranks by term overlap and returns only entries with a non-zero
    overlap, so an answerable question retrieves its passage and an
    unrelated question retrieves nothing (which drives abstention rather
    than handing the model an irrelevant excerpt).
    """

    async def retrieve(question: str, scope: PrincipalScope) -> list[RetrievedChunk]:
        query_terms = _terms(question)
        scored: list[tuple[float, CorpusEntry]] = []
        for entry in CORPUS:
            if not _visible(entry, scope):
                continue
            # Title is searchable, as it is in the real index: a question
            # naming a document ("the onboarding guide") must reach that
            # document even when the body words it differently
            # ("workspaces are provisioned").
            searchable = f"{entry.document_title} {entry.text}"
            overlap = len(query_terms & _terms(searchable))
            if overlap:
                scored.append((overlap / max(len(query_terms), 1), entry))
        scored.sort(key=lambda pair: pair[0], reverse=True)

        chunks: list[RetrievedChunk] = []
        for score, entry in scored:
            chunk = _chunk(entry)
            chunk.score = score
            chunks.append(chunk)
        return chunks

    return retrieve


def chunk_id_for(version_key: str) -> uuid.UUID:
    """Stable chunk id for a corpus entry, for citation assertions."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"eval:{version_key}")
