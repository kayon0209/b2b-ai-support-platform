"""Unit tests: deterministic canary rollout (Phase 4, ticket 40).

The properties asserted here are the ones that make a canary a canary. If any
of them fails, a rollout becomes unverifiable and a rollback becomes a guess:

- **Determinism.** A tenant's answer must not depend on process, run, or call
  order. A random or counter-based rollout would give different answers in
  different workers, so the same tenant would see the feature on and off
  within one session.
- **Monotonicity.** Lowering the percentage may only ever *remove* tenants,
  never add them. This is what makes "roll back to 5%" a strictly safer
  action than "roll back to 10%".
- **Independence.** Two flags at the same percentage must not select the same
  tenants, or rolling out feature B compounds feature A's blast radius.
- **Coverage of the endpoints.** 0% must include nobody and 100% everybody,
  exactly - not "whoever happens to hash low".
"""

import uuid

from platform_core.knowledge.flag_service import (
    BUCKET_SPACE,
    is_in_rollout,
    stable_bucket,
)

TENANTS = [uuid.UUID(f"0190d000-0000-7000-8000-{i:012d}") for i in range(400)]

# A tenant that lands exactly in bucket 0. Asserting "0% includes nobody" over
# a few hundred random tenants is statistically blind: bucket 0 holds about 1
# tenant in 10,000, so a `percent < 0` off-by-one would leak roughly one tenant
# per 10,000 and no plausible sample size would notice. This pair is found by
# search and pinned, so the boundary is tested exactly rather than
# probabilistically.
BUCKET_ZERO_FLAG = "f"
BUCKET_ZERO_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000d6f")
# A tenant whose bucket is exactly 5_000, i.e. exactly the 50% inclusion
# boundary. At 50% the comparison is `bucket < 5000`, so this tenant is
# excluded; an off-by-one (`<=`) would include it. The 0% and 100% fast paths
# cannot catch that, because they never reach the arithmetic.
BOUNDARY_TENANT = uuid.UUID("00000000-0000-0000-0000-000000001ed5")
BOUNDARY_BUCKET = 5000


class TestDeterminism:
    def test_same_tenant_always_gets_the_same_bucket(self) -> None:
        tenant = TENANTS[0]
        buckets = {stable_bucket(flag_key="new-retrieval", tenant_id=tenant) for _ in range(50)}
        assert len(buckets) == 1, "a tenant must not move between evaluations"

    def test_bucket_is_within_the_space(self) -> None:
        for tenant in TENANTS[:100]:
            assert 0 <= stable_bucket(flag_key="f", tenant_id=tenant) < BUCKET_SPACE

    def test_different_tenants_spread_across_buckets(self) -> None:
        # A hash that collided badly would make "10%" mean "the same 10
        # tenants for every flag and every rollout".
        buckets = {stable_bucket(flag_key="f", tenant_id=t) for t in TENANTS}
        assert len(buckets) > len(TENANTS) * 0.9

    def test_key_is_part_of_the_hash_input(self) -> None:
        # Two flags at the same percentage must select different tenants.
        tenant = TENANTS[0]
        assert stable_bucket(flag_key="flag-a", tenant_id=tenant) != stable_bucket(
            flag_key="flag-b", tenant_id=tenant
        )


class TestMonotonicity:
    def test_lowering_percent_only_removes_tenants(self) -> None:
        included_100 = {t for t in TENANTS if is_in_rollout(flag_key="f", tenant_id=t, percent=100)}
        included_50 = {t for t in TENANTS if is_in_rollout(flag_key="f", tenant_id=t, percent=50)}
        included_10 = {t for t in TENANTS if is_in_rollout(flag_key="f", tenant_id=t, percent=10)}
        included_0 = {t for t in TENANTS if is_in_rollout(flag_key="f", tenant_id=t, percent=0)}

        assert included_0 <= included_10 <= included_50 <= included_100

    def test_rollback_is_deterministic_for_a_single_tenant(self) -> None:
        # The operational question during an incident: "if I drop to 5%, is
        # this tenant still in?" must have one answer.
        tenant = TENANTS[7]
        first = [
            is_in_rollout(flag_key="f", tenant_id=tenant, percent=p)
            for p in (100, 50, 25, 10, 5, 1, 0)
        ]
        second = [
            is_in_rollout(flag_key="f", tenant_id=tenant, percent=p)
            for p in (100, 50, 25, 10, 5, 1, 0)
        ]
        assert first == second
        # And it may only switch off, never back on, as the percentage drops.
        assert first == sorted(first, reverse=True)


class TestEndpoints:
    def test_zero_percent_includes_nobody(self) -> None:
        assert not any(
            is_in_rollout(flag_key="f", tenant_id=t, percent=0) for t in TENANTS
        )

    def test_zero_percent_excludes_even_the_lowest_bucket(self) -> None:
        # The exact boundary. `stable_bucket` of this tenant is 0, which is the
        # only bucket a naive `bucket < percent * step` comparison could
        # wrongly include at 0%.
        assert stable_bucket(flag_key=BUCKET_ZERO_FLAG, tenant_id=BUCKET_ZERO_TENANT) == 0
        assert not is_in_rollout(
            flag_key=BUCKET_ZERO_FLAG, tenant_id=BUCKET_ZERO_TENANT, percent=0
        )
        # And at the smallest non-zero percentage a bucket-0 tenant is in.
        assert is_in_rollout(
            flag_key=BUCKET_ZERO_FLAG, tenant_id=BUCKET_ZERO_TENANT, percent=1
        )

    def test_hundred_percent_includes_everybody(self) -> None:
        assert all(is_in_rollout(flag_key="f", tenant_id=t, percent=100) for t in TENANTS)

    def test_negative_percent_includes_nobody(self) -> None:
        # Defensive: `set_rollout` validates, but a negative reaching the
        # math must mean "nobody", not "everybody".
        assert not is_in_rollout(flag_key="f", tenant_id=TENANTS[0], percent=-5)

    def test_over_hundred_includes_everybody(self) -> None:
        assert is_in_rollout(flag_key="f", tenant_id=TENANTS[0], percent=1000)

    def test_approximate_coverage_matches_the_percentage(self) -> None:
        # Not an exact assertion - hashing has variance - but a rollout that
        # is off by an order of magnitude is a bug, not noise.
        for percent in (10, 25, 50):
            included = sum(
                1 for t in TENANTS if is_in_rollout(flag_key="f", tenant_id=t, percent=percent)
            )
            share = included / len(TENANTS)
            assert abs(share - percent / 100) < 0.06, f"{percent}% selected {share:.0%}"

    def test_boundary_bucket_is_exclusive(self) -> None:
        # The 50% comparison must be `bucket < 5000`, not `<=`. This is the
        # only assertion that reaches the arithmetic, since the 0% and 100%
        # fast paths return before it. Verified by mutation: changing `<` to
        # `<=` and running only the endpoint tests leaves them green.
        assert stable_bucket(flag_key="f", tenant_id=BOUNDARY_TENANT) == BOUNDARY_BUCKET, (
            "the pinned boundary tenant has moved; re-find one at bucket 5000"
        )
        assert not is_in_rollout(flag_key="f", tenant_id=BOUNDARY_TENANT, percent=50)
        # It is inside just above the boundary and outside just below.
        assert is_in_rollout(flag_key="f", tenant_id=BOUNDARY_TENANT, percent=51)
        assert not is_in_rollout(flag_key="f", tenant_id=BOUNDARY_TENANT, percent=49)
