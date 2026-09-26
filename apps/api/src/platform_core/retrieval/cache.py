"""Feature list 11.3: caching under the retrieval layer.

What gets cached, and why it is the embedding rather than the results:

**The embedding of a query depends on the text alone.** A result set depends on
the text *plus* the tenant, the ACL principal, the knowledge spaces, the
metadata filter, the RRF constant and the candidate budgets. A cache keyed on
the query string over that second thing is a data-leak primitive: two tenants
asking the same question would share an entry, and the second one would receive
documents chosen under the first one's ACL. Whatever this module caches, the
key must contain every input that changes the answer - and the cheapest way to
be sure of that is to cache the one input whose only dependency is the text.

It is also the expensive part. A retrieval call spends one network round trip
on embedding before it touches Postgres; repeated wording (a customer asking
twice, an agent re-running a question, several conversations about the same
topic in one shift) pays that cost again every time.

**A cache is a correctness trade and the TTL is the price.** The embedding of a
given string does not change when the corpus changes - the model is fixed - so
the usual staleness worry does not apply here, and the TTL exists only to bound
memory against an unbounded query space. That is why it can be longer than the
connector caches: a stale CRM record is a wrong answer, a cached embedding of
the same string is the same vector.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

# Bounded, because the query space is not: every distinct customer question is
# a new key. 1024 vectors at 1536 floats is a few MB, which is small next to
# the latency it saves.
DEFAULT_MAX_ENTRIES = 1024

# Longer than the connector TTL on purpose - see the module docstring. An
# embedding of a fixed string under a fixed model does not go stale.
DEFAULT_TTL_SECONDS = 3600


@dataclass
class _Entry[T]:
    value: T
    expires_at: float


class TtlCache[T]:
    """A bounded, TTL'd, insertion-ordered cache with hit accounting.

    LRU by insertion order rather than by access: the access-ordered variant
    needs a move on every `get`, and for embeddings the ordering matters far
    less than the bound. Being explicit about that is cheaper than pretending
    this is an optimal eviction policy.

    `hits`/`misses` are exposed because a cache whose hit rate nobody measures
    is a cache nobody can tell is working - the failure mode is silence, not an
    error.
    """

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = max(1, max_entries)
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[str, _Entry[T]] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key: str) -> T | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        if self._clock() >= entry.expires_at:
            # Expired entries are removed on read rather than swept, so a
            # process that stops querying does not keep paying for them.
            del self._entries[key]
            self.misses += 1
            return None
        self.hits += 1
        return entry.value

    def put(self, key: str, value: T) -> None:
        if self._ttl_seconds <= 0:
            # TTL 0 disables caching rather than storing an entry that expires
            # the instant it is written - the latter still grows the dict.
            return
        self._entries[key] = _Entry(value=value, expires_at=self._clock() + self._ttl_seconds)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
            self.evictions += 1

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def hit_rate(self) -> float | None:
        """None over no traffic, which is not the same as a 0% hit rate."""
        total = self.hits + self.misses
        return round(self.hits / total, 3) if total else None


def embedding_key(text: str, *, model: str) -> str:
    """Cache key for one query embedding.

    Includes the `model` because the same string embeds differently under
    different models, and a deployment that switches models must not serve
    vectors from the previous one - the two are not comparable, and mixing
    them would silently corrupt every similarity that follows.
    """
    material = f"{model}\x00{text}".encode()
    return hashlib.sha256(material).hexdigest()


# One cache per process, matching the "one client per process" rule the model
# factory follows: several caches would divide the hit rate by their count.
embedding_cache: TtlCache[list[float]] = TtlCache()


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TTL_SECONDS",
    "TtlCache",
    "embedding_cache",
    "embedding_key",
]
