"""Feature list 3.9: cluster similar questions.

Why this exists when `question_hash` already deduplicates: the hash catches
the *same* question asked twice ("How do I reset my password" / "how do i
reset my password"). It cannot catch the same question asked *differently* -
"能不能加急" and "加急打样多久" are two rows today, and a knowledge owner
reading the leak queue sees two unrelated items instead of one demand with a
count of two. Clustering is what turns a list of questions into a list of
*topics*, and the topic count is what decides what to write next.

The similarity measure is imported from `cases.service` rather than
reimplemented. That is deliberate: §5-B established, by measurement on this
database, that pg_trgm `similarity()` returns 0.000 for short Chinese pairs
(the shared term 加急 lands in different three-character windows), and replaced
it with term overlap over Latin words + CJK bigrams. A second copy of that
logic would drift from the first, and two measures that disagree is worse than
one measure that is imperfect.

TODO: `_term_set` / `_ubiquitous_terms` / `_compare` should be lifted into a
shared `retrieval.terms` module and imported by both callers. They are private
today, and importing a private name is the lesser evil next to duplicating a
measured algorithm.

Clustering is greedy and deterministic: the highest-frequency question becomes
a cluster's representative, and every later question joins the first cluster it
is similar enough to. Deterministic matters here - the same leak queue must
produce the same clusters tomorrow, because an owner noticing "that topic
moved" needs it to mean something.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Imported, not copied: see the module docstring for why.
from platform_core.cases.service import _compare, _term_set, _ubiquitous_terms

# Dice coefficient above which two questions are treated as one topic.
#
# Measured, not guessed. The pair this feature exists for - "能不能加急" and
# "加急打样多久" - shares exactly one bigram (加急) out of 4 and 5 terms, so its
# Dice is 2/9 = **0.222**. A threshold above that leaves them as two rows,
# which is the bug being fixed. Unrelated pairs score 0.0 ("怎么重置登录密码" vs
# "PCB 打样的交期是多久" share nothing), so 0.2 separates what must stay apart
# with room to spare.
#
# Clustering is a coarser judgement than "show me similar cases" - it is a
# grouping aid for a human who will read the members anyway - so it sits at the
# low end on purpose. Parameterised because the right value depends on the
# tenant's phrasing, and that is an operational judgement, not a constant of
# nature.
DEFAULT_SIMILARITY_THRESHOLD = 0.20


@dataclass(frozen=True)
class QuestionCluster:
    """One topic in the leak queue."""

    representative: str
    members: tuple[str, ...]
    total_frequency: int
    # Why the members are together. A cluster with no visible shared term is
    # not reviewable: the owner has to take it on trust.
    shared_terms: tuple[str, ...] = ()

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass
class _Bucket:
    representative: str
    terms: set[str]
    members: list[str] = field(default_factory=list)
    frequency: int = 0
    shared: tuple[str, ...] = ()


def cluster_questions(
    questions: list[tuple[str, int]],
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[QuestionCluster]:
    """Group (question, frequency) pairs into topics, biggest demand first.

    `questions` is a list rather than a queryset so this stays a pure function
    of its input - testable without a database, and usable over any list of
    questions a caller already has.
    """
    if not questions:
        return []

    # Most-demanded first, so a cluster's representative is the question
    # customers actually ask most, not whichever arrived first.
    ordered = sorted(questions, key=lambda pair: (-pair[1], pair[0]))
    term_sets = [_term_set(text) for text, _ in ordered]
    ubiquitous = _ubiquitous_terms(term_sets)

    buckets: list[_Bucket] = []
    for (text, frequency), terms in zip(ordered, term_sets, strict=True):
        match: _Bucket | None = None
        for bucket in buckets:
            shared, score = _compare(bucket.terms, terms, ubiquitous)
            if score >= threshold:
                match = bucket
                # Keep the first evidence: the terms that put the second
                # member in the cluster are the ones an owner will check.
                if not bucket.shared:
                    bucket.shared = shared
                break
        if match is None:
            buckets.append(
                _Bucket(representative=text, terms=terms, members=[text], frequency=frequency)
            )
        else:
            match.members.append(text)
            match.frequency += frequency

    buckets.sort(key=lambda bucket: (-bucket.frequency, bucket.representative))
    return [
        QuestionCluster(
            representative=bucket.representative,
            members=tuple(bucket.members),
            total_frequency=bucket.frequency,
            shared_terms=bucket.shared,
        )
        for bucket in buckets
    ]


__all__ = ["DEFAULT_SIMILARITY_THRESHOLD", "QuestionCluster", "cluster_questions"]
