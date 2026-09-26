"""Integration: the agent workbench bundle.

Feature list 7.4/7.6. The complaint about handoffs is never the handoff, it is
that the customer then repeats themselves - so the point of this endpoint is
that opening a case is enough to answer. It asserts the bundle is assembled
from rows that already exist (case + link + turns), that another tenant's case
is not visible, and that a role without CASE_READ is refused with a real 403.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from platform_core.identity.middleware import TenantContextMiddleware
from platform_core.identity.tenant_context import TenantContext

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

TENANT = "01900000-0000-7000-8000-0000000000ca"
OTHER_TENANT = "01900000-0000-7000-8000-0000000000cb"
SLUG = "case-workbench"
OTHER_SLUG = "case-workbench-other"

CASE_A = "01900000-0000-7000-8000-0000000000e1"
CASE_B = "01900000-0000-7000-8000-0000000000e2"
CONVERSATION = "01900000-0000-7000-8000-0000000000e3"

# The related-case fixtures. Each one exists to make exactly one distinction
# observable, and the subjects were chosen by measuring `similarity()` rather
# than by feel (see `RELATED_SUBJECT_FLOOR`):
#
#   CASE_A       "Short circuit claim"              the case being opened
#   CASE_C       "Short circuit claim on batch 42"  shares 3 terms + category => `match: subject`
#   CASE_B       "Earlier claim"                    shares the term `claim`   => `match: subject`
#   CASE_E       "Wrong part delivered"             same category, no shared term
#                                                   => `match: category`, capped
#   CASE_GENERAL "Where is my order?"               category `general`
#   CASE_D       "Invoice address change"           same category as the above, no
#                                                   shared term => bucket-only slot
#   OTHER_CASE   "Short circuit claim"              ANOTHER TENANT, identical subject
CASE_C = "01900000-0000-7000-8000-0000000000e4"
CASE_D = "01900000-0000-7000-8000-0000000000e5"
CASE_GENERAL = "01900000-0000-7000-8000-0000000000e6"
OTHER_CASE = "01900000-0000-7000-8000-0000000000e7"
CASE_E = "01900000-0000-7000-8000-0000000000e8"

AGENT_TEXT = "标准交期以报价单为准，我帮您核实具体单号。"


class _RoleResolver:
    def __init__(self, tenant_id: str, role: str) -> None:
        self._tenant_id = tenant_id
        self._role = role

    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=uuid.UUID(self._tenant_id),
            actor_id=uuid.uuid5(uuid.NAMESPACE_URL, f"actor:{self._tenant_id}-{self._role}"),
            actor_kind="user",
            role=self._role,
        )


def _client(tenant_id: str, role: str) -> TestClient:
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=_RoleResolver(tenant_id, role))
    return TestClient(fresh, raise_server_exceptions=False)


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer pt_bootstrap_test"}


def _seed() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, SLUG), (OTHER_TENANT, OTHER_SLUG)):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :slug, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
        for case_id, subject, tenant, category in (
            (CASE_A, "Short circuit claim", TENANT, "quality"),
            (CASE_B, "Earlier claim", TENANT, "quality"),
            (CASE_C, "Short circuit claim on batch 42", TENANT, "quality"),
            (CASE_E, "Wrong part delivered", TENANT, "quality"),
            (CASE_GENERAL, "Where is my order?", TENANT, "general"),
            # Same bucket as CASE_GENERAL and nothing else in common: this is
            # the row that must be labelled as a bucket match, and capped.
            (CASE_D, "Invoice address change", TENANT, "general"),
            # The decoy: identical subject and category, different tenant.
            (OTHER_CASE, "Short circuit claim", OTHER_TENANT, "quality"),
        ):
            conn.execute(
                text(
                    "INSERT INTO cases (id, tenant_id, subject, description, status, "
                    "priority, category, version, opened_at, elapsed_running_seconds, "
                    "last_state_changed_at) VALUES (:i, :t, :s, '', 'open', 'p2', "
                    ":cat, 1, 0, 0, 0) ON CONFLICT DO NOTHING"
                ),
                {"i": case_id, "t": tenant, "s": subject, "cat": category},
            )
        conn.execute(
            text(
                "INSERT INTO case_conversations (id, tenant_id, case_id, "
                "conversation_ref_id, relationship) VALUES "
                "(gen_random_uuid(), :t, :c, :conv, 'origin') ON CONFLICT DO NOTHING"
            ),
            {"t": TENANT, "c": CASE_A, "conv": CONVERSATION},
        )
        for ordinal, (role, body) in enumerate((("customer", "板子短路了"), ("agent", AGENT_TEXT))):
            conn.execute(
                text(
                    "INSERT INTO conversation_turns (id, tenant_id, conversation_ref_id, "
                    "role, text_redacted, text_hash, ts) VALUES "
                    "(gen_random_uuid(), :t, :conv, :r, :x, :h, :o) ON CONFLICT DO NOTHING"
                ),
                {
                    "t": TENANT,
                    "conv": CONVERSATION,
                    "r": role,
                    "x": body,
                    "h": f"hash-{ordinal}",
                    "o": ordinal,
                },
            )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tenant in (TENANT, OTHER_TENANT):
            # Bindings before parents, and before any re-seed: they are unique
            # per (tenant, contact), so a row left behind makes the next run's
            # ON CONFLICT DO NOTHING a no-op that silently keeps stale values.
            conn.execute(
                text("DELETE FROM enterprise_account_contacts WHERE tenant_id = :t"),
                {"t": tenant},
            )
            conn.execute(text("DELETE FROM conversation_turns WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM case_conversations WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM cases WHERE tenant_id = :t"), {"t": tenant})
            # Accounts last: `cases.enterprise_account_id` is a composite FK to
            # them, so deleting the account while a case still points at it is
            # a violation rather than a cascade.
            conn.execute(
                text("DELETE FROM enterprise_accounts WHERE tenant_id = :t"), {"t": tenant}
            )
        for slug in (SLUG, OTHER_SLUG):
            conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": slug})
    admin.dispose()


@pytest.fixture(autouse=True)
def clean() -> None:
    _clear()
    _seed()
    yield
    _clear()


def test_the_bundle_carries_case_conversation_and_the_ai_proposal() -> None:
    agent = _client(TENANT, "support_agent")

    resp = agent.get(f"/v1/cases/{CASE_A}/workbench", headers=_headers())

    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["case"]["case_id"] == CASE_A
    roles = [turn["role"] for turn in body["conversation"]]
    assert roles == ["customer", "agent"]
    # The proposal an agent reads instead of reconstructing it.
    assert body["ai_suggestion"]["text"] == AGENT_TEXT
    assert isinstance(body["ai_suggestion"]["sources"], list)


def test_related_cases_state_their_basis_and_their_reason() -> None:
    """The panel must state what it computed, and every row why it is there.

    The basis used to be `same_category`, which was honest about a query that
    only bucketed by category - and useless, because `category` defaults to
    `general`: for a `general` case the panel listed the most recent cases in
    the tenant, unrelated tickets under a relevance heading.
    """
    agent = _client(TENANT, "support_agent")

    body = agent.get(f"/v1/cases/{CASE_A}/workbench", headers=_headers()).json()

    related = body["related_cases"]
    assert related["basis"] == "subject_terms_or_category"
    assert related["items"], "CASE_B shares the term `claim` with CASE_A"
    for item in related["items"]:
        assert item["match"] in ("subject", "category"), item
        assert isinstance(item["score"], (int, float)), item
    ids = [item["case_id"] for item in related["items"]]
    assert CASE_A not in ids, "a case is not related to itself"


def test_a_wording_match_ranks_above_a_bucket_match() -> None:
    """Ranking is what makes the strip usable, so it is asserted, not assumed.

    CASE_C shares three terms with CASE_A; CASE_E shares none and only matches
    the bucket. The wording match must come first and say so - otherwise the
    operator gets the old list with a longer heading.
    """
    agent = _client(TENANT, "support_agent")

    body = agent.get(f"/v1/cases/{CASE_A}/workbench", headers=_headers()).json()
    items = body["related_cases"]["items"]
    by_id = {item["case_id"]: item for item in items}

    assert items[0]["case_id"] == CASE_C, items
    assert by_id[CASE_C]["match"] == "subject"
    assert set(by_id[CASE_C]["shared_terms"]) >= {"short", "circuit", "claim"}, by_id[CASE_C]
    assert by_id[CASE_E]["match"] == "category", by_id[CASE_E]
    assert by_id[CASE_E]["shared_terms"] == []
    # Bucket matches sit behind every wording match, wherever they appear.
    first_weak = next(i for i, item in enumerate(items) if item["match"] == "category")
    assert all(item["match"] == "subject" for item in items[:first_weak]), items


def test_a_bucket_match_is_capped_and_labelled() -> None:
    """`general` is the default category, so "same category" alone is noise.

    For CASE_GENERAL the only peer is CASE_D, in the same bucket and sharing
    nothing. It is allowed on the panel - a same-bucket case with different
    wording is still worth a glance - but as what it is (`match: category`) and
    in bounded supply, so a panel in the dominant bucket cannot become a list
    of arbitrary recent tickets.
    """
    agent = _client(TENANT, "support_agent")

    body = agent.get(f"/v1/cases/{CASE_GENERAL}/workbench", headers=_headers()).json()
    items = body["related_cases"]["items"]

    weak = [item for item in items if item["match"] == "category"]
    assert [item["case_id"] for item in weak] == [CASE_D], items
    assert all(item["shared_terms"] == [] for item in weak), items


def test_a_related_case_from_another_tenant_is_never_returned() -> None:
    """The mandatory negative: this is a list of *other people's* tickets.

    Tenant B holds a case with the *identical* subject to CASE_A and the same
    category, which is the strongest possible decoy. Two independent barriers
    stand in front of it - the explicit `tenant_id` predicate and RLS - and
    this asserts the outcome rather than either mechanism, because a test that
    names one mechanism stops protecting the moment the other becomes the only
    one left.
    """
    agent = _client(TENANT, "support_agent")

    body = agent.get(f"/v1/cases/{CASE_A}/workbench", headers=_headers()).json()
    ids = [item["case_id"] for item in body["related_cases"]["items"]]

    assert OTHER_CASE not in ids, body["related_cases"]
    # A negative that passes on an empty panel proves nothing, so the panel is
    # asserted non-empty first - and its strongest match must be this tenant's
    # own wording match rather than the decoy.
    assert body["related_cases"]["items"], "an empty panel would make this vacuous"
    assert ids[0] == CASE_C, body["related_cases"]


def test_another_tenants_case_is_not_found() -> None:
    other = _client(OTHER_TENANT, "support_agent")

    resp = other.get(f"/v1/cases/{CASE_A}/workbench", headers=_headers())

    assert resp.status_code == 404, resp.text[:200]


def test_a_role_without_case_read_is_refused() -> None:
    nobody = _client(TENANT, "viewer")

    resp = nobody.get(f"/v1/cases/{CASE_A}/workbench", headers=_headers())

    assert resp.status_code == 403, f"got {resp.status_code}: {resp.text[:200]}"


def test_the_bundle_lists_the_accounts_other_contacts() -> None:
    """Feature list 2.1, in the place it pays off.

    One company, several channels, one Chatwoot contact per channel. The
    binding already knows they are the same account; if the workbench does not
    show that, an agent reads the email from last week and the message from
    this morning as two different customers and asks the same questions twice.
    """
    import sqlalchemy as sa

    account_id = "01900000-0000-7000-8000-0000000000f1"
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO enterprise_accounts (id, tenant_id, name, tier, "
                "contract_status, attributes, created_at, updated_at) VALUES "
                "(:i, :t, 'Multi Channel Co', 'enterprise', 'active', '{}'::jsonb, 0, 0) "
                "ON CONFLICT DO NOTHING"
            ),
            {"i": account_id, "t": TENANT},
        )
        conn.execute(
            sa.text("UPDATE cases SET enterprise_account_id = :a WHERE id = :c AND tenant_id = :t"),
            {"a": account_id, "c": CASE_A, "t": TENANT},
        )
        for contact, channel in (("email-contact", "email"), ("wechat-contact", "wechat")):
            conn.execute(
                sa.text(
                    "INSERT INTO enterprise_account_contacts (id, tenant_id, "
                    "enterprise_account_id, external_contact_id, channel, created_at) "
                    "VALUES (gen_random_uuid(), :t, :a, :c, :ch, 0) ON CONFLICT DO NOTHING"
                ),
                {"t": TENANT, "a": account_id, "c": contact, "ch": channel},
            )
    admin.dispose()

    body = (
        _client(TENANT, "support_agent")
        .get(f"/v1/cases/{CASE_A}/workbench", headers=_headers())
        .json()
    )

    contacts = {c["external_contact_id"]: c["channel"] for c in body["account_contacts"]}
    assert contacts["email-contact"] == "email"
    assert contacts["wechat-contact"] == "wechat"
    assert body["account_tier"] == "enterprise"
