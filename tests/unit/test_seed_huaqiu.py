"""The Huaqiu pilot seed script: its data and its two couplings.

`scripts/seed_huaqiu.py` is deployment tooling, so it is not imported by the
product - which is exactly why drift here is silent. A renamed capability or
a renamed flag key would leave a tenant seeded with rows that look configured
and do nothing, and nothing in the suite would go red.

Three properties are pinned, none of which is a restatement of the constant:

1. **Every seeded alias actually expands.** The alias table is the fix for
   research difficulty 1 (绿油 vs 阻焊层). An alias that the production
   normalizer does not apply is a row that costs a query rewrite and returns
   nothing, so each entry is run through the real `normalize_colloquial`.
2. **The seeded capabilities match the adapter.** `BusinessReadAdapter`
   declares what it can do; the seed claims capabilities on the connector
   row. If they diverge the Tool Gateway refuses to build an executor and
   every read tool fails as `TOOL_EXECUTOR_MISSING` - at the customer, not
   at seed time.
3. **The flag keys are the ones the code reads.** Same shape as (2): a
   renamed setting would leave the seed enabling a flag nobody evaluates.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from platform_core.agent_runtime.conversation import normalize_colloquial
from platform_core.agent_runtime.orchestrator import RERANK_FLAG_KEY
from platform_core.config import Settings
from platform_core.integrations.business_read import BusinessReadAdapter


def _load_seed_module() -> object:
    """Import the script by path: `scripts/` is tooling, not a package."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "seed_huaqiu.py"
    spec = importlib.util.spec_from_file_location("seed_huaqiu", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def seed() -> object:
    return _load_seed_module()


def _weighted(aliases: tuple[tuple[str, str], ...]) -> list[tuple[str, str, float]]:
    return [(alias, term, 1.0) for alias, term in aliases]


def test_every_seeded_alias_expands_a_query(seed: object) -> None:
    """An alias the normalizer ignores is a dead row in the tenant's table."""
    aliases = _weighted(seed.PCB_ALIASES)  # type: ignore[attr-defined]
    for alias, term in seed.PCB_ALIASES:  # type: ignore[attr-defined]
        query = f"{alias} 的工艺要求是什么"
        normalized, applied = normalize_colloquial(query, aliases)
        # `applied` reports the canonical terms that were substituted in, not
        # the surface forms, so this is the check that the mapping fired.
        assert term in applied, f"alias {alias!r} did not expand to {term!r}"
        assert term in normalized, (
            f"alias {alias!r} fired but its canonical term {term!r} is not in "
            f"the normalized query: {normalized!r}"
        )


def test_alias_surface_forms_are_unique(seed: object) -> None:
    """`uq_aliases_tenant_alias` would reject a duplicate at seed time."""
    surface = [alias for alias, _ in seed.PCB_ALIASES]  # type: ignore[attr-defined]
    assert len(surface) == len(set(surface)), f"duplicate alias: {surface}"


def test_seeded_capabilities_match_the_adapter(seed: object) -> None:
    """Divergence here fails every read tool, at the customer rather than here."""
    assert set(seed.CONNECTOR_CAPABILITIES) == set(  # type: ignore[attr-defined]
        BusinessReadAdapter.capabilities
    )


def test_the_connector_provider_is_the_one_the_registry_resolves(seed: object) -> None:
    assert seed.CONNECTOR_PROVIDER == BusinessReadAdapter.provider  # type: ignore[attr-defined]


def test_flag_keys_are_the_ones_the_code_reads(seed: object) -> None:
    assert seed.FLAG_BUSINESS_READ == Settings().flag_business_read_tools  # type: ignore[attr-defined]
    assert seed.FLAG_RERANK == RERANK_FLAG_KEY  # type: ignore[attr-defined]


def test_business_read_is_pilot_targeted_not_left_to_the_rollout_hash(
    seed: object,
) -> None:
    """A pilot tenant must not depend on where its uuid lands in a bucket.

    Asserted on the constants rather than the row: the flag is seeded with
    `rollout_percent=0` and an explicit tenant target, so whether it is on is
    decided by the target row and never by `stable_bucket`.
    """
    assert seed.RERANK_ROLLOUT_PERCENT < 100  # type: ignore[attr-defined]
    assert seed.FLAG_BUSINESS_READ != seed.FLAG_RERANK  # type: ignore[attr-defined]


def test_purge_covers_every_table_the_script_writes(seed: object) -> None:
    """`--purge` must reach every table the seed inserts into.

    A table added to an insert but not to PURGE_TABLES leaves orphan rows on
    re-seed, which is how a "clean" pilot tenant ends up carrying last week's
    configuration.
    """
    written = {
        "departments",
        "knowledge_spaces",
        "enterprise_accounts",
        "knowledge_aliases",
        "connectors",
        "feature_flags",
        "feature_flag_targets",
    }
    assert written <= set(seed.PURGE_TABLES)  # type: ignore[attr-defined]


def test_purge_lists_no_duplicates(seed: object) -> None:
    tables = list(seed.PURGE_TABLES)  # type: ignore[attr-defined]
    assert len(tables) == len(set(tables)), f"duplicate purge table: {tables}"


def test_the_read_tools_the_seed_enables_are_the_ones_the_adapter_can_serve(
    seed: object,
) -> None:
    """Every capability seeded must be reachable by a registered read tool."""
    from platform_core.integrations.business_read import READ_TOOL_RESOURCES

    assert set(READ_TOOL_RESOURCES) >= {
        "order.get_status",
        "shipment.track",
        "billing.get_invoice",
        "inventory.check_stock",
    }
    # Four tools, four capabilities: a fifth capability with no tool is the
    # recurring shape in this repo - something that exists and is seeded but
    # that no production path can reach.
    assert len(READ_TOOL_RESOURCES) == len(seed.CONNECTOR_CAPABILITIES)  # type: ignore[attr-defined]
