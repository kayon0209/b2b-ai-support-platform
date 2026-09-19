"""Seed the HUAQIU pilot tenant (huqiu research report, stage 0).

Not part of the product: deployment tooling for the first customer rollout.
Uses the `platform` (superuser) role because it must insert across RLS
boundaries before the tenant context exists - the same pattern as
`seed_admin_demo.py`.

Creates, idempotently:

- tenant `huaqiu` in the cn-north-1 data region (data residency: the report's
  compliance requirement);
- the four business-line departments (PCB / PCBA / 商城 / 工程);
- the five knowledge spaces (工艺能力 / 交易政策 / 元器件 / DFM-EDA / 大客户专属);
- EnterpriseAccounts exercising every contract tier, so tier-driven SLA and
  escalation have data to act on;
- the PCB terminology alias table initial version (huqiu research difficulty 1:
  colloquial names vs document names - 绿油=阻焊, 沉金=化金, 半孔=半孔径...);
- the `business_api` connector the read tools need (research stage 1), and the
  two feature flags that gate them.

The connector is created only when `HUAQIU_ERP_BASE_URL` is set. Seeding a
connector with a placeholder URL would *look* configured while every call
failed, which is worse than being visibly absent - the same reason an unset
model key fails the model boundary closed. `agent.business_read_enabled`
follows the connector: read tools that cannot reach the ERP would turn a
routine "where is my order" into a tool failure instead of a knowledge answer.

Usage:
    python scripts/seed_huaqiu.py            # create what is missing
    python scripts/seed_huaqiu.py --purge    # delete tenant rows first
"""

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api", "src"))

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

TENANT_SLUG = "huaqiu"
TENANT_NAME = "深圳华秋智联股份有限公司"

DEPARTMENTS = (
    ("pcb", "PCB 事业部"),
    ("pcba", "PCBA 事业部"),
    ("mall", "商城运营"),
    ("engineering", "工程技术部"),
)

KNOWLEDGE_SPACES = (
    ("process-capability", "PCB 工艺能力"),
    ("trade-policy", "交易与政策"),
    ("components", "元器件"),
    ("dfm-eda", "DFM 与 EDA 工具"),
    ("key-accounts", "大客户专属政策"),
)

# tier -> (account name, contract status). One account per tier so the
# tier-driven SLA / escalation ladder has data to act on from day one.
ENTERPRISE_ACCOUNTS = (
    ("strategic", "华秋战略客户示例（协议价 + 专属对接）", "active"),
    ("enterprise", "华秋企业客户示例", "active"),
    ("standard", "华秋标准客户示例", "active"),
    ("basic", "华秋基础客户示例", "active"),
)

# Colloquial name -> canonical document term (huqiu research difficulty 1).
# Weight 1.0 (the 0-2 multiplier from migration 0033): aliases rank a
# candidate up but never double it.
PCB_ALIASES: tuple[tuple[str, str], ...] = (
    ("绿油", "阻焊层"),
    ("阻焊", "阻焊层"),
    ("沉金", "化学沉镍金"),
    ("化金", "化学沉镍金"),
    ("喷锡", "热风整平"),
    ("半孔", "半孔径"),
    ("邮票孔", "半孔径"),
    ("过孔", "导通孔"),
    ("盲孔", "盲埋孔"),
    ("埋孔", "盲埋孔"),
    ("铜厚", "成品铜厚"),
    ("飞针", "飞针测试"),
    ("字高", "字符高度"),
    ("V割", "V-CUT"),
    ("邮票边", "工艺边"),
    ("EQ", "工程确认"),
    ("MI", "制作指示"),
    ("TGZ", "工程文件包"),
)

# --- Stage-1 wiring (huqiu research 3.5): read tools and their gates --------
#
# The four capabilities the BusinessReadAdapter claims. Seeding them is what
# lets the Tool Gateway build an executor; a connector that claims only reads
# can never become a write path.
CONNECTOR_PROVIDER = "business_api"
CONNECTOR_NAME = "huaqiu-erp-read"
CONNECTOR_CAPABILITIES = (
    "orders_read",
    "shipments_read",
    "invoices_read",
    "inventory_read",
)

# Read tools are gated on this flag, and the pilot tenant is targeted
# explicitly rather than left to the rollout hash: a pilot whose behaviour
# depends on where its uuid lands in a bucket cannot be debugged.
FLAG_BUSINESS_READ = "agent.business_read_enabled"
# Reranking is deliberately NOT tenant-targeted: the report asks for a small
# partial rollout so its effect is measured, not assumed.
FLAG_RERANK = "agent.rerank_enabled"
RERANK_ROLLOUT_PERCENT = 10

# Ordered children-first: a superset of seed_admin_demo's list plus the tables
# this script fills (aliases, departments, accounts, spaces, connectors).
PURGE_TABLES = (
    "knowledge_aliases",
    "connectors",
    "billing_entries",
    "feature_flag_targets",
    "feature_flags",
    # "knowledge_gap_drafts" was listed here and does not exist. The DELETE
    # raised, the transaction rolled back, and --purge silently removed
    # nothing while printing a traceback - a cleanup command that looks like
    # it failed noisily, which is the one failure mode nobody re-checks.
    # Two more had rotted the same way: "case_events" and "tenant_branding".
    "knowledge_gaps",
    "prompt_versions",
    # children of `cases`, and why the list is ordered children-first
    "case_conversations",
    "case_escalations",
    "cases",
    "membership_invitations",
    "memberships",
    "audit_events",
    "outbox_events",
    "enterprise_accounts",
    "chunks",
    "document_versions",
    "documents",
    "knowledge_acls",
    "knowledge_sources",
    "knowledge_spaces",
    "departments",
)


def purge(conn: object, tenant_id: uuid.UUID) -> None:
    """Delete the tenant's rows, or refuse before touching any of them.

    The table list is checked up front because the deletes run in one
    transaction: a name that no longer exists raises halfway through and
    rolls the whole thing back, so `--purge` removed nothing while looking
    like it had failed at the very end. Refusing first turns that into a
    message naming the stale entry.
    """
    existing = {
        row[0]
        for row in conn.execute(  # type: ignore[attr-defined]
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        )
    }
    missing = [t for t in PURGE_TABLES if t not in existing]
    if missing:
        raise SystemExit(
            f"PURGE_TABLES names tables that do not exist: {missing}. "
            "The schema moved; update the list rather than deleting around it."
        )
    for table in PURGE_TABLES:
        conn.execute(  # type: ignore[attr-defined]
            text(f"DELETE FROM {table} WHERE tenant_id = :t"),  # noqa: S608 - fixed table list
            {"t": str(tenant_id)},
        )


def now() -> int:
    return int(datetime.now(UTC).timestamp())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--purge", action="store_true", help="delete tenant rows first")
    args = parser.parse_args()

    engine = create_engine(ADMIN_URL)
    tenant_id = uuid.uuid5(uuid.NAMESPACE_URL, f"tenant:{TENANT_SLUG}")

    with engine.begin() as conn:
        if args.purge:
            purge(conn, tenant_id)

        # --- tenant (global reference table, no RLS) ---
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status, data_region) VALUES "
                "(:id, :slug, :name, 'active', 'cn-north-1') "
                "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name"
            ),
            {"id": str(tenant_id), "slug": TENANT_SLUG, "name": TENANT_NAME},
        )
        tid = str(tenant_id)
        stamp = now()

        # --- departments (huqiu research 1.5.1: the four business lines) ---
        for slug, name in DEPARTMENTS:
            dept_id = str(uuid.uuid5(tenant_id, f"department:{slug}"))
            conn.execute(
                text(
                    "INSERT INTO departments (id, tenant_id, name, slug, created_at, updated_at) "
                    "VALUES (:id, :t, :n, :s, :ts, :ts) "
                    "ON CONFLICT (tenant_id, slug) DO UPDATE SET name = EXCLUDED.name"
                ),
                {"id": dept_id, "t": tid, "n": name, "s": slug, "ts": stamp},
            )

        # --- knowledge spaces (huqiu research 3.2: the four knowledge layers
        #     plus the key-account ACL space) ---
        for slug, name in KNOWLEDGE_SPACES:
            space_id = str(uuid.uuid5(tenant_id, f"space:{slug}"))
            conn.execute(
                text(
                    "INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES "
                    "(:id, :t, :n) ON CONFLICT DO NOTHING"
                ),
                {"id": space_id, "t": tid, "n": name},
            )

        # --- enterprise accounts, one per contract tier ---
        for tier, name, status in ENTERPRISE_ACCOUNTS:
            account_id = str(uuid.uuid5(tenant_id, f"account:{tier}"))
            conn.execute(
                text(
                    "INSERT INTO enterprise_accounts (id, tenant_id, name, tier, "
                    "contract_status, created_at, updated_at) VALUES "
                    "(:id, :t, :n, :tier, :status, :ts, :ts) "
                    "ON CONFLICT DO NOTHING"
                ),
                {
                    "id": account_id,
                    "t": tid,
                    "n": name,
                    "tier": tier,
                    "status": status,
                    "ts": stamp,
                },
            )

        # --- PCB terminology aliases (difficulty 1: the first knowledge gate) ---
        for alias, term in PCB_ALIASES:
            alias_id = str(uuid.uuid5(tenant_id, f"alias:{alias}"))
            conn.execute(
                text(
                    "INSERT INTO knowledge_aliases "
                    "(id, tenant_id, term, alias, weight, created_at) VALUES "
                    "(:id, :t, :term, :alias, 1.0, :ts) "
                    "ON CONFLICT (tenant_id, alias) DO UPDATE SET term = EXCLUDED.term"
                ),
                {"id": alias_id, "t": tid, "term": term, "alias": alias, "ts": stamp},
            )

        # --- business_api connector (research stage 1: the read tools) -------
        base_url = os.environ.get("HUAQIU_ERP_BASE_URL", "").strip()
        token_env = os.environ.get("HUAQIU_ERP_TOKEN_ENV", "HUAQIU_ERP_API_TOKEN")
        connector_ready = bool(base_url)
        connector_id = str(uuid.uuid5(tenant_id, f"connector:{CONNECTOR_NAME}"))

        if connector_ready:
            conn.execute(
                text(
                    "INSERT INTO connectors (id, tenant_id, provider, name, status, "
                    "capabilities, configuration, credential_ref) VALUES "
                    "(:id, :t, :p, :n, 'active', CAST(:caps AS jsonb), "
                    "CAST(:cfg AS jsonb), :cred) "
                    "ON CONFLICT (tenant_id, provider, name) DO UPDATE SET "
                    "configuration = EXCLUDED.configuration, "
                    "credential_ref = EXCLUDED.credential_ref"
                ),
                {
                    "id": connector_id,
                    "t": tid,
                    "p": CONNECTOR_PROVIDER,
                    "n": CONNECTOR_NAME,
                    "caps": json.dumps(list(CONNECTOR_CAPABILITIES)),
                    "cfg": json.dumps({"base_url": base_url}),
                    # A reference, never a credential: the pilot scheme is
                    # env:// and production swaps the resolver, not this row.
                    "cred": f"env://{token_env}",
                },
            )
        else:
            # Leave whatever is there alone: an operator who configured it by
            # hand should not have their row deleted by a re-seed.
            print("warning: HUAQIU_ERP_BASE_URL is unset - no connector seeded;")
            print("         read tools stay disabled for this tenant (see below).")

        # --- feature flags ---------------------------------------------------
        # business_read follows the connector: with no ERP to read from, a
        # "where is my order" question must fall back to a knowledge answer
        # rather than fail through a tool that cannot connect.
        flags = (
            (FLAG_BUSINESS_READ, connector_ready, 0, "read tools need the ERP connector"),
            (FLAG_RERANK, True, RERANK_ROLLOUT_PERCENT, "partial rollout, measured"),
        )
        for key, enabled, percent, description in flags:
            flag_id = str(uuid.uuid5(tenant_id, f"flag:{key}"))
            conn.execute(
                text(
                    "INSERT INTO feature_flags (id, tenant_id, key, description, "
                    "enabled, rollout_percent, created_at) VALUES "
                    "(:id, :t, :k, :d, :e, :p, :ts) "
                    "ON CONFLICT (tenant_id, key) DO UPDATE SET "
                    "enabled = EXCLUDED.enabled, rollout_percent = EXCLUDED.rollout_percent"
                ),
                {
                    "id": flag_id,
                    "t": tid,
                    "k": key,
                    "d": description,
                    "e": enabled,
                    "p": percent,
                    "ts": stamp,
                },
            )
            # Only business_read is tenant-targeted, and only when the
            # connector exists: an explicit target overrides the rollout hash
            # in both directions, so it is the one way to say "this tenant",
            # deterministically, rather than "whoever hashes into the bucket".
            if key == FLAG_BUSINESS_READ:
                conn.execute(
                    text(
                        "INSERT INTO feature_flag_targets (id, tenant_id, flag_id, "
                        "target_tenant_id, enabled) VALUES (:id, :t, :f, :target, :e) "
                        "ON CONFLICT (flag_id, target_tenant_id) DO UPDATE SET "
                        "enabled = EXCLUDED.enabled"
                    ),
                    {
                        "id": str(uuid.uuid5(tenant_id, f"flagtarget:{key}")),
                        "t": tid,
                        "f": flag_id,
                        "target": tid,
                        "e": connector_ready,
                    },
                )

    engine.dispose()

    print(f"tenant {TENANT_SLUG} ({tenant_id}) seeded:")
    print(f"  departments        : {len(DEPARTMENTS)}")
    print(f"  knowledge spaces   : {len(KNOWLEDGE_SPACES)}")
    print(f"  enterprise accounts: {len(ENTERPRISE_ACCOUNTS)} (one per tier)")
    print(f"  pcb aliases        : {len(PCB_ALIASES)}")
    print(f"  erp connector      : {'seeded' if connector_ready else 'NOT configured'}")
    print(f"  {FLAG_BUSINESS_READ}: {'on' if connector_ready else 'off (no connector)'}")
    print(f"  {FLAG_RERANK}: {RERANK_ROLLOUT_PERCENT}% rollout")
    print("next: upload the corpus into the spaces, then run the stage-1 acceptance")
    print("     gates (scripts/run_eval.py) against the huaqiu eval cases.")


if __name__ == "__main__":
    main()
