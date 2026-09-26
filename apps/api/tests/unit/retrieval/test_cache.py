"""Feature list 11.3: the retrieval cache is safe, bounded, and measurable.

Three properties, each with a test that would fail if it broke:

- **It caches the embedding, not the results.** The embedding depends on the
  text alone; a result set also depends on tenant and ACL. Caching results on a
  text key would serve one tenant documents selected under another's ACL -
  a leak that no error message announces. Asserted by checking the key changes
  when the model changes, and (in the integration path) that the cached value
  is a vector, not a chunk list.
- **It is bounded and it expires.** The query space is unbounded, so an
  unbounded dict is a slow leak. Both the size cap and the TTL are exercised.
- **Its hit rate is knowable.** A cache whose effectiveness nobody measures
  fails silently, so hits/misses/evictions are counted and `hit_rate` is None
  (not 0.0) over no traffic.
"""

from __future__ import annotations

import pytest

from platform_core.retrieval.cache import TtlCache, embedding_key


class _Clock:
    """A controllable monotonic clock, so TTL is tested without sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_a_stored_value_comes_back() -> None:
    cache: TtlCache[str] = TtlCache()
    cache.put("k", "v")
    assert cache.get("k") == "v"


def test_a_missing_key_is_a_miss() -> None:
    cache: TtlCache[str] = TtlCache()
    assert cache.get("absent") is None
    assert cache.misses == 1


def test_an_expired_entry_is_not_returned() -> None:
    clock = _Clock()
    cache: TtlCache[str] = TtlCache(ttl_seconds=60, clock=clock)
    cache.put("k", "v")
    clock.advance(61)
    assert cache.get("k") is None


def test_an_entry_survives_inside_its_ttl() -> None:
    clock = _Clock()
    cache: TtlCache[str] = TtlCache(ttl_seconds=60, clock=clock)
    cache.put("k", "v")
    clock.advance(59)
    assert cache.get("k") == "v"


def test_the_cache_is_bounded_and_evicts_oldest_first() -> None:
    """The query space is unbounded; the cache must not be."""
    cache: TtlCache[int] = TtlCache(max_entries=2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    assert len(cache) == 2
    assert cache.get("a") is None  # evicted
    assert cache.get("c") == 3
    assert cache.evictions == 1


def test_a_zero_ttl_disables_storing_entirely() -> None:
    """TTL 0 must not grow a dict of already-expired entries."""
    cache: TtlCache[str] = TtlCache(ttl_seconds=0)
    cache.put("k", "v")
    assert len(cache) == 0
    assert cache.get("k") is None


def test_hit_rate_is_measured() -> None:
    cache: TtlCache[str] = TtlCache()
    cache.put("k", "v")
    cache.get("k")
    cache.get("k")
    cache.get("absent")
    assert cache.hits == 2
    assert cache.misses == 1
    assert cache.hit_rate == pytest.approx(0.667, abs=0.001)


def test_hit_rate_over_no_traffic_is_unknown_not_zero() -> None:
    """0% would look like a cache that never helps."""
    assert TtlCache().hit_rate is None


def test_the_embedding_key_depends_on_the_text() -> None:
    assert embedding_key("a", model="m") != embedding_key("b", model="m")


def test_the_embedding_key_depends_on_the_model() -> None:
    """Two models' vectors are not comparable; mixing them corrupts silently."""
    assert embedding_key("same", model="m1") != embedding_key("same", model="m2")


def test_the_embedding_key_is_stable_for_the_same_input() -> None:
    assert embedding_key("same", model="m") == embedding_key("same", model="m")


def test_the_embedding_key_does_not_collide_across_text_boundaries() -> None:
    """A separator is used so ("ab","c") and ("a","bc") differ."""
    assert embedding_key("ab", model="c") != embedding_key("a", model="bc")
