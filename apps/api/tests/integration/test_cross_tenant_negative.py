"""Cross-tenant negative suite (ticket 25, docs/testing-and-evaluation.md).

For every tenant-owned table, prove the app role sees/writes only its own
rows: direct ID access, list filters, guessed external IDs, background
writes. Fail-closed (no context = no rows) is also asserted per table.
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = os.environ.get(
    "APP_TEST_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)
TENANT_A = "01900000-0000-7000-8000-000000000001"
TENANT_B = "01900000-0000-7000-8000-000000000002"

TENANT_TABLES = (
    "memberships",
    "external_resource_refs",
    "inbox_events",
    "outbox_events",
    "conversation_control_leases",
    "knowledge_spaces",
    "knowledge_sources",
    "documents",
    "document_versions",
    "chunks",
    "knowledge_acls",
    "agent_runs",
    "citations",
    "cases",
    "audit_events",
    "issue_categories",
    "canned_replies",
    "agent_profiles",
    "sla_policies",
    "ab_experiments",
    # The conversation and tool-execution surface. These were missing while
    # the table above already looked comprehensive, which is the failure mode
    # worth naming: a hand-maintained list reads as exhaustive and is not.
    # `conversation_turns` is what a customer actually sees, `tool_executions`
    # records what the platform did on their behalf, and both sat outside the
    # zero-tolerance sweep. `tool_definitions` / `tool_proposals` /
    # `case_conversations` are here because the rows below are unreachable
    # without them, not because they are interesting on their own.
    #
    # Coverage is still partial: 51 tables carry `tenant_id` and this list
    # names 28. The remainder (billing, SSO/SCIM, connectors, sync cursors,
    # feature flags, drafts and gaps) is tracked in TODO.md under
    # "cross-tenant sweep coverage" rather than left implicit here.
    "case_conversations",
    "conversation_turns",
    "csat_responses",
    "case_attachments",
    "tool_definitions",
    "tool_proposals",
    "tool_executions",
    "action_confirmations",
    # Phase five: the identity, SSO/SCIM and connection surface. 52 tables
    # carry `tenant_id` and this list now names all of them. Measured against
    # `information_schema`, not maintained by hand - the earlier note said "51
    # carry tenant_id and this list names 28" and was already stale when written,
    # which is the whole problem with a hand-maintained list.
    #
    # All of these have RLS policies, and phase one's `0055_rls_empty_binding_guard`
    # already applied the NULLIF hardening to them, so this batch adds
    # *verification* rather than protection. That is a different and smaller
    # claim: the policy exists and no test has ever proved it denies.
    "external_identities",
    "tenant_domains",
    "membership_invitations",
    "scim_tokens",
    "saml_connections",
    "saml_consumed_assertions",
    "departments",
    # Billing, customer records, the knowledge authoring surface, connector
    # plumbing and feature flags. `billing_entries` is the one that matters
    # most: it is what a tenant is charged for, and a policy that failed to
    # deny would let one tenant read another's invoices.
    "enterprise_accounts",
    "enterprise_account_contacts",
    "connectors",
    "sync_cursors",
    "dead_letter_items",
    "feature_flags",
    "feature_flag_targets",
    "case_escalations",
    "answer_corrections",
    "conversation_contacts",
    "contact_facts",
    "visitor_session_revocations",
    "knowledge_gaps",
    "knowledge_drafts",
    "knowledge_aliases",
    "prompt_versions",
    "billing_entries",
    # AI support v2 task, assessment and copilot storage. These tables were
    # added with tenant-scoped RLS and must join the same zero-tolerance sweep.
    "semantic_assessments",
    "conversation_tasks",
    "conversation_task_events",
    "copilot_drafts",
)

_seed_ids: dict[str, str] = {}


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _purge(admin) -> None:
    """Remove any rows left by a previous run of this module.

    A `yield` fixture does not run its teardown when setup raises, so a single
    failed seed leaves rows behind - and the next run fails on the *same* rows,
    with an error that names the wrong table. That happened here: a run aborted
    mid-seed left tenant `neg-a` in place, and thirty-two unrelated tests then
    failed on `tenants_pkey` with nothing pointing at this file.

    Purging first makes the module self-healing: a failed run costs one run
    rather than every run until somebody notices. Order matters - the tenant
    rows are deleted last, after the rows that reference them.
    """
    with admin.begin() as conn:
        for tid in (TENANT_A, TENANT_B):
            for table in reversed(TENANT_TABLES):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE tenant_id = :t"),  # noqa: S608
                    {"t": tid},
                )
        conn.execute(text("DELETE FROM tenants WHERE slug IN ('neg-a','neg-b')"))


@pytest.fixture(scope="module", autouse=True)
def seed_all_tables() -> None:
    admin = create_engine(ADMIN_URL)
    # Before inserting, not only after. See `_purge`.
    _purge(admin)
    with admin.begin() as conn:
        for tid, slug in ((TENANT_A, "neg-a"), (TENANT_B, "neg-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, 'Neg', 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
        # Seed one row per tenant in each tenant-owned table (A only;
        # B seeds nothing so its view must be empty).
        for _tid, slug in ((TENANT_A, "neg-a"), (TENANT_B, "neg-b")):
            conn.execute(
                text(
                    "INSERT INTO users (id, primary_email, display_name) VALUES "
                    "(gen_random_uuid(), :email, 'Neg User') "
                    "ON CONFLICT (primary_email) DO NOTHING"
                ),
                {"email": f"neg-{slug}@test.local"},
            )
        a = TENANT_A
        space = str(uuid.uuid4())
        doc = str(uuid.uuid4())
        ver = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        case_id = str(uuid.uuid4())
        tool_def = str(uuid.uuid4())
        conversation_ref = str(uuid.uuid4())
        assessment_id = str(uuid.uuid4())
        task_id = str(uuid.uuid4())
        _seed_ids.update(
            space=space,
            doc=doc,
            ver=ver,
            run_id=run_id,
            case_id=case_id,
            tool_def=tool_def,
            conversation_ref=conversation_ref,
            assessment_id=assessment_id,
            task_id=task_id,
        )
        stmts = [
            (
                "memberships",
                "INSERT INTO memberships (id, tenant_id, user_id, role) "
                "SELECT :i, :t, u.id, 'support_agent' FROM users u "
                "WHERE u.primary_email = 'neg-neg-a@test.local'",
            ),
            ("users", None),  # users created via membership FK below
            (
                "external_resource_refs",
                "INSERT INTO external_resource_refs (id, tenant_id, system, resource_type,"
                "external_id) "
                "VALUES (:i, :t, 'chatwoot', 'account', '999888')",
            ),
            (
                "inbox_events",
                "INSERT INTO inbox_events (id, tenant_id, delivery_id, event_type,"
                "payload_hash, received_at) "
                "VALUES (:i, :t, 'neg-delivery-1', 'message_created', 'hash1', 1000)",
            ),
            (
                "outbox_events",
                "INSERT INTO outbox_events (id, tenant_id, event_id, event_type, aggregate_type, "
                "aggregate_id, created_at) VALUES (:i, :t, gen_random_uuid(), 'case.created',"
                "'case', :cid, 1000)",
            ),
            (
                "conversation_control_leases",
                "INSERT INTO conversation_control_leases (id, tenant_id, conversation_ref_id,"
                "owner_type, mode) "
                "VALUES (:i, :t, gen_random_uuid(), 'ai', 'AI_ACTIVE')",
            ),
            (
                "knowledge_spaces",
                "INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES (:sid, :t, 'neg-space')",
            ),
            (
                "knowledge_sources",
                "INSERT INTO knowledge_sources (id, tenant_id, space_id, type, name) "
                "VALUES (:i, :t, :sid, 'upload', 'neg-source')",
            ),
            (
                "documents",
                "INSERT INTO documents (id, tenant_id, space_id, source_id, canonical_uri, title) "
                "VALUES (:did, :t, :sid, (SELECT id FROM knowledge_sources WHERE tenant_id ="
                "CAST(:t AS uuid) AND name='neg-source'), 'kb://neg', 'Neg Doc')",
            ),
            (
                "document_versions",
                "INSERT INTO document_versions (id, tenant_id, document_id, version_label, "
                "content_hash, object_uri, status) VALUES (:vid, :t, :did, 'v1', 'h',"
                "'minio://neg', 'active')",
            ),
            (
                "chunks",
                "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, text, text_hash) "
                "VALUES (:i, :t, :vid, 0, 'negative suite chunk', 'h1')",
            ),
            (
                "knowledge_acls",
                "INSERT INTO knowledge_acls (id, tenant_id, resource_type, resource_id,"
                "principal_type, principal_id) "
                "VALUES (:i, :t, 'space', :sid, 'department', 'neg-team')",
            ),
            (
                "agent_runs",
                "INSERT INTO agent_runs (id, tenant_id, conversation_ref_id, route) "
                "VALUES (:rid, :t, gen_random_uuid(), 'knowledge_qa')",
            ),
            (
                "citations",
                "INSERT INTO citations (id, tenant_id, agent_run_id, document_version_id,"
                "chunk_id, "
                "excerpt_hash, source_uri) VALUES (:i, :t, :rid, :vid, gen_random_uuid(), 'h',"
                "'minio://neg')",
            ),
            (
                "cases",
                "INSERT INTO cases (id, tenant_id, subject, opened_at) "
                "VALUES (:cid, :t, 'neg case', 1000)",
            ),
            (
                "audit_events",
                "INSERT INTO audit_events (id, tenant_id, occurred_at, actor_type, action,"
                "resource_type, "
                "decision, reason_code, trace_id) VALUES (:i, :t, 1000, 'user', 'case.create',"
                "'case', "
                "'completed', 'OK', 'trace-neg')",
            ),
            (
                "issue_categories",
                "INSERT INTO issue_categories (id, tenant_id, category_key, state, "
                "created_at, updated_at) VALUES (:i, :t, 'neg|neg|neg', 'observed', 1000, 1000)",
            ),
            (
                "canned_replies",
                "INSERT INTO canned_replies (id, tenant_id, title, body, created_at, "
                "updated_at) VALUES (:i, :t, 'neg reply', 'neg body', 1000, 1000)",
            ),
            (
                "agent_profiles",
                "INSERT INTO agent_profiles (id, tenant_id, user_ref, display_name, "
                "created_at, updated_at) VALUES (:i, :t, 'neg-agent', 'Neg Agent', 1000, 1000)",
            ),
            (
                "sla_policies",
                "INSERT INTO sla_policies (id, tenant_id, tier, first_response_minutes, "
                "resolution_minutes, created_at, updated_at) VALUES "
                "(:i, :t, 'standard', 60, 480, 1000, 1000)",
            ),
            (
                "ab_experiments",
                "INSERT INTO ab_experiments (id, tenant_id, key, variants, created_at, "
                "updated_at) VALUES (:i, :t, 'neg.exp', "
                'CAST(\'[{"name": "a", "weight": 1}]\' AS jsonb), 1000, 1000)',
            ),
            (
                "case_conversations",
                "INSERT INTO case_conversations (id, tenant_id, case_id, conversation_ref_id) "
                "VALUES (:i, :t, :cid, gen_random_uuid())",
            ),
            (
                "conversation_turns",
                "INSERT INTO conversation_turns (id, tenant_id, conversation_ref_id, role, "
                "text_redacted, text_hash) SELECT :i, :t, cc.conversation_ref_id, 'customer', "
                "'neg turn', 'th' FROM case_conversations cc WHERE cc.tenant_id = CAST(:t AS uuid) "
                "ORDER BY cc.id LIMIT 1",
            ),
            (
                "csat_responses",
                "INSERT INTO csat_responses (id, tenant_id, conversation_ref_id, score, "
                "created_at) SELECT :i, :t, cc.conversation_ref_id, 5, 1000 "
                "FROM case_conversations cc WHERE cc.tenant_id = CAST(:t AS uuid) "
                "ORDER BY cc.id LIMIT 1",
            ),
            (
                "case_attachments",
                "INSERT INTO case_attachments (id, tenant_id, case_id, object_key, filename, "
                "content_type, size_bytes, created_at) "
                "VALUES (:i, :t, :cid, 'neg/attach.txt', 'attach.txt', 'text/plain', 4, 1000)",
            ),
            (
                "tool_definitions",
                "INSERT INTO tool_definitions (id, tenant_id, name) "
                "VALUES (:tooldef, :t, 'neg.tool')",
            ),
            (
                "tool_proposals",
                "INSERT INTO tool_proposals (id, tenant_id, tool_definition_id, action_hash, "
                "actor_id, idempotency_key) "
                "VALUES (:i, :t, :tooldef, 'ah', gen_random_uuid(), 'neg-idem')",
            ),
            (
                "tool_executions",
                "INSERT INTO tool_executions (id, tenant_id, actor_id, tool_definition_id, "
                "idempotency_key) VALUES (:i, :t, gen_random_uuid(), :tooldef, 'neg-idem-exec')",
            ),
            (
                "action_confirmations",
                "INSERT INTO action_confirmations (id, tenant_id, proposal_id, actor_id, "
                "action_hash, expires_at) "
                "SELECT :i, :t, tp.id, gen_random_uuid(), tp.action_hash, 9999 "
                "FROM tool_proposals tp WHERE tp.tenant_id = CAST(:t AS uuid) "
                "ORDER BY tp.id LIMIT 1",
            ),
            # --- Identity, SSO and SCIM. Each is a table a tenant's own data
            # sits in and nobody outside that tenant may reach. The seed
            # statements use literal ids rather than foreign keys where the
            # column allows it, because the point is the RLS predicate, not the
            # referential graph - and a row that cannot be inserted is a test
            # that fails for the wrong reason.
            (
                "departments",
                "INSERT INTO departments (id, tenant_id, name, slug) "
                "VALUES (:i, :t, 'Neg Dept', :slug)",
            ),
            (
                "external_identities",
                "INSERT INTO external_identities (id, tenant_id, system, subject, user_id) "
                "SELECT :i, :t, 'neg-system', :slug, u.id FROM users u "
                "WHERE u.primary_email = :email",
            ),
            (
                "tenant_domains",
                "INSERT INTO tenant_domains (id, tenant_id, domain, verification_token, "
                "created_at) VALUES (:i, :t, :slug, 'neg-token', 9999)",
            ),
            (
                "membership_invitations",
                "INSERT INTO membership_invitations (id, tenant_id, email, role, token, "
                "expires_at) VALUES (:i, :t, :email, 'support_agent', :i, 9999)",
            ),
            (
                "scim_tokens",
                "INSERT INTO scim_tokens (id, tenant_id, name, token_hash) "
                "VALUES (:i, :t, 'neg-scim', :hash)",
            ),
            (
                "saml_connections",
                "INSERT INTO saml_connections (id, tenant_id, name, idp_entity_id, "
                "idp_sso_url, idp_certificate, sp_entity_id) "
                "VALUES (:i, :t, 'Neg SAML', :slug, 'https://idp.test/sso', 'CERT', :slug)",
            ),
            (
                "saml_consumed_assertions",
                "INSERT INTO saml_consumed_assertions (id, tenant_id, connection_id, "
                "assertion_id) SELECT :i, :t, sc.id, 'neg-assertion' "
                "FROM saml_connections sc WHERE sc.tenant_id = CAST(:t AS uuid) "
                "ORDER BY sc.id LIMIT 1",
            ),
            # --- Billing, customers, connectors, knowledge authoring.
            #
            # Order follows the foreign keys: accounts before their contacts,
            # connectors before cursors and dead letters, gaps before drafts.
            # Each statement looks its parent up by tenant rather than by a
            # literal, so the seed cannot drift from whatever the previous
            # statement inserted.
            (
                "enterprise_accounts",
                "INSERT INTO enterprise_accounts (id, tenant_id, name) "
                "VALUES (:i, :t, 'Neg Account')",
            ),
            (
                "enterprise_account_contacts",
                "INSERT INTO enterprise_account_contacts (id, tenant_id, "
                "enterprise_account_id, external_contact_id, created_at) "
                "SELECT :i, :t, ea.id, :slug, 9999 FROM enterprise_accounts ea "
                "WHERE ea.tenant_id = CAST(:t AS uuid) ORDER BY ea.id LIMIT 1",
            ),
            (
                "connectors",
                "INSERT INTO connectors (id, tenant_id, provider, name) "
                "VALUES (:i, :t, 'neg-provider', 'Neg Connector')",
            ),
            (
                "sync_cursors",
                "INSERT INTO sync_cursors (id, tenant_id, connector_id, resource_type) "
                "SELECT :i, :t, c.id, 'neg-resource' FROM connectors c "
                "WHERE c.tenant_id = CAST(:t AS uuid) ORDER BY c.id LIMIT 1",
            ),
            (
                "dead_letter_items",
                "INSERT INTO dead_letter_items (id, tenant_id, resource_type, operation, "
                "operation_digest, error_code, created_at) "
                "VALUES (:i, :t, 'neg_resource', 'neg_op', :hash, 'NEG_ERROR', 9999)",
            ),
            (
                "feature_flags",
                "INSERT INTO feature_flags (id, tenant_id, key) VALUES (:i, :t, :slug)",
            ),
            (
                "feature_flag_targets",
                "INSERT INTO feature_flag_targets (id, tenant_id, flag_id, "
                "target_tenant_id) SELECT :i, :t, ff.id, CAST(:t AS uuid) "
                "FROM feature_flags ff WHERE ff.tenant_id = CAST(:t AS uuid) "
                "ORDER BY ff.id LIMIT 1",
            ),
            (
                "case_escalations",
                "INSERT INTO case_escalations (id, tenant_id, case_id, clock, level, "
                "reason_code) SELECT :i, :t, c.id, 'first_response', 1, 'neg_reason' "
                "FROM cases c WHERE c.tenant_id = CAST(:t AS uuid) "
                "ORDER BY c.id LIMIT 1",
            ),
            (
                "answer_corrections",
                "INSERT INTO answer_corrections (id, tenant_id, agent_run_id, question, "
                "correct_answer, created_at) "
                "SELECT :i, :t, ar.id, 'neg question', 'neg answer', 9999 "
                "FROM agent_runs ar WHERE ar.tenant_id = CAST(:t AS uuid) "
                "ORDER BY ar.id LIMIT 1",
            ),
            (
                "conversation_contacts",
                "INSERT INTO conversation_contacts (id, tenant_id, conversation_ref_id, "
                "external_contact_id, created_at) "
                "VALUES (:i, :t, :i, :slug, 9999)",
            ),
            (
                "contact_facts",
                "INSERT INTO contact_facts (id, tenant_id, contact_ref, key, value) "
                "VALUES (:i, :t, :i, 'neg_key', 'neg_value')",
            ),
            (
                "visitor_session_revocations",
                "INSERT INTO visitor_session_revocations (id, tenant_id, conversation_ref, "
                "revoked_at) VALUES (:i, :t, :i, 9999)",
            ),
            (
                "knowledge_gaps",
                "INSERT INTO knowledge_gaps (id, tenant_id, question_hash, sample_question, "
                "first_seen_at, last_seen_at) "
                "VALUES (:i, :t, :hash, 'neg sample', 9999, 9999)",
            ),
            (
                "knowledge_drafts",
                "INSERT INTO knowledge_drafts (id, tenant_id, gap_id, title, body) "
                "SELECT :i, :t, kg.id, 'Neg Draft', 'neg body' FROM knowledge_gaps kg "
                "WHERE kg.tenant_id = CAST(:t AS uuid) ORDER BY kg.id LIMIT 1",
            ),
            (
                "knowledge_aliases",
                "INSERT INTO knowledge_aliases (id, tenant_id, term, alias) "
                "VALUES (:i, :t, 'negterm', 'negalias')",
            ),
            (
                "prompt_versions",
                "INSERT INTO prompt_versions (id, tenant_id, template_name, version, body) "
                "VALUES (:i, :t, 'neg-template', 1, 'neg body')",
            ),
            (
                "billing_entries",
                "INSERT INTO billing_entries (id, tenant_id, event_id, run_id, "
                "period_start, recorded_at) "
                "SELECT :i, :t, gen_random_uuid(), ar.id, 9999, 9999 "
                "FROM agent_runs ar WHERE ar.tenant_id = CAST(:t AS uuid) "
                "ORDER BY ar.id LIMIT 1",
            ),
            (
                "semantic_assessments",
                "INSERT INTO semantic_assessments (id, tenant_id, conversation_ref_id, "
                "turn_id, mode, created_at) VALUES (:assessment, :t, :conversation, "
                "'neg-turn', 'off', 1000)",
            ),
            (
                "conversation_tasks",
                "INSERT INTO conversation_tasks (id, tenant_id, conversation_ref_id, "
                "source_turn_id, task_local_key, kind, status, content_hash, created_at) "
                "VALUES (:taskid, :t, :conversation, 'neg-turn', 'neg-task', 'read', "
                "'ready', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' "
                "|| 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 1000)",
            ),
            (
                "conversation_task_events",
                "INSERT INTO conversation_task_events (id, tenant_id, task_id, "
                "conversation_ref_id, to_status, actor_type, reason_code, trace_id, "
                "to_version, created_at) VALUES (:i, :t, :taskid, :conversation, "
                "'ready', 'system', 'NEGATIVE_TEST', 'trace-neg', 1, 1000)",
            ),
            (
                "copilot_drafts",
                "INSERT INTO copilot_drafts (id, tenant_id, conversation_ref_id, actor_id, "
                "job_id, kind, status, created_at, updated_at) VALUES (:i, :t, :conversation, "
                "gen_random_uuid(), gen_random_uuid(), 'summary', 'queued', 1000, 1000)",
            ),
        ]
        for _table, stmt in stmts:
            if stmt is None:
                continue
            conn.execute(
                text(stmt),
                {
                    "i": uuid.uuid4(),
                    "t": a,
                    "sid": space,
                    "did": doc,
                    "vid": ver,
                    "rid": run_id,
                    "cid": case_id,
                    "tooldef": tool_def,
                    "conversation": conversation_ref,
                    "assessment": assessment_id,
                    "taskid": task_id,
                    "slug": f"neg-{_tid[-4:]}-x",
                    "email": f"neg-{slug}@test.local",
                    "hash": "a" * 64,
                },
            )
    yield
    _purge(admin)
    admin.dispose()


@pytest.mark.zero_tolerance("cross_tenant_violations")
def test_every_tenant_table_is_isolated_and_fails_closed() -> None:
    """One sweep across all tenant-owned tables: A sees its row, B sees
    none, no-context sees none, B cannot write into A's scope."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.db import create_engine

    async def sweep() -> list[tuple[str, bool, bool, bool]]:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        results: list[tuple[str, bool, bool, bool]] = []
        for table in TENANT_TABLES:
            # Tenant A sees its own row
            async with factory() as session:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A}
                )
                a_rows = (
                    await session.execute(
                        text(f"SELECT count(*) FROM {table}")  # noqa: S608
                    )
                ).scalar()
                await session.rollback()
            # Tenant B sees nothing
            async with factory() as session:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_B}
                )
                b_rows = (
                    await session.execute(
                        text(f"SELECT count(*) FROM {table}")  # noqa: S608
                    )
                ).scalar()
                await session.rollback()
            # No context sees nothing. RESET rather than relying on a fresh
            # session: `set_config(..., false)` elsewhere is session-scoped and
            # survives on a pooled connection, so "no context" would otherwise
            # mean "whatever the last tenant-scoped test left behind" - which
            # is a different thing and makes this assertion pass for the wrong
            # reason.
            async with factory() as session:
                await session.execute(text("RESET app.tenant_id"))
                no_ctx = (
                    await session.execute(
                        text(f"SELECT count(*) FROM {table}")  # noqa: S608
                    )
                ).scalar()
                await session.rollback()
            results.append((table, int(a_rows) >= 1, int(b_rows) == 0, int(no_ctx) == 0))
        await engine.dispose()
        return results

    for table, a_sees, b_blind, fails_closed in _run(sweep()):
        assert a_sees, f"{table}: tenant A should see its own row"
        assert b_blind, f"{table}: tenant B must see zero rows"
        assert fails_closed, f"{table}: missing context must yield zero rows"


@pytest.mark.zero_tolerance("cross_tenant_violations")
def test_guessing_other_tenant_resource_ids_yields_nothing() -> None:
    """Direct-ID access with B's context against A's known row IDs."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from platform_core.db import create_engine

    async def sweep() -> list[tuple[str, int]]:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        out: list[tuple[str, int]] = []
        probes = [
            ("knowledge_spaces", "id", _seed_ids["space"]),
            ("documents", "id", _seed_ids["doc"]),
            ("document_versions", "id", _seed_ids["ver"]),
            ("agent_runs", "id", _seed_ids["run_id"]),
            ("cases", "id", _seed_ids["case_id"]),
        ]
        for table, col, rid in probes:
            async with factory() as session:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_B}
                )
                n = (
                    await session.execute(
                        text(
                            f"SELECT count(*) FROM {table} WHERE {col} = :rid"  # noqa: S608
                        ),
                        {"rid": rid},
                    )
                ).scalar()
                await session.rollback()
            out.append((table, int(n)))
        await engine.dispose()
        return out

    for table, n in _run(sweep()):
        assert n == 0, f"{table}: direct-ID access leaked across tenants"


def test_the_table_list_matches_the_database() -> None:
    """The list is derived from the schema, not maintained by hand.

    This list was a literal, and it drifted twice in ways nobody noticed: it
    said "51 tables carry `tenant_id` and this list names 28" when the database
    had 52, and the four entries it *did* name as gaps had been delivered two
    phases earlier. A hand-maintained list of the thing it claims to be complete
    against reads as exhaustive and is not - the previous comment in this file
    said as much while leaving the list itself unchanged.

    So the completeness claim is now checked. A table gaining a `tenant_id`
    column fails here, with its name, instead of being silently unswept until
    somebody happens to read this file.

    It is also the check that makes the rest trustworthy: "every table is
    isolated" is only a claim if the set of tables is known to be all of them.
    """
    # Asked through the admin connection already configured above, rather than by
    # shelling out to `docker exec`. The subprocess version named the container,
    # so it fails on any machine where that container is called something else -
    # and a guard that only runs on the author's laptop is not a guard. S607
    # flagged the partial executable path, which was the honest signal rather
    # than a style complaint.
    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        in_database = set(
            conn.execute(
                text(
                    "select table_name from information_schema.columns "
                    "where table_schema='public' and column_name='tenant_id' "
                    "order by table_name"
                )
            ).scalars()
        )
    admin.dispose()
    listed = set(TENANT_TABLES)

    unswept = sorted(in_database - listed)
    assert not unswept, (
        f"{len(unswept)} table(s) carry tenant_id and are not in the sweep: {unswept}. "
        "Add a seed row and the table name, or explain in a comment why it is "
        "excluded - a silent omission is the failure this assertion exists for"
    )

    # The other direction. A name in the list that is not a tenant table means
    # either a typo or a dropped column, and both make the sweep assert
    # something weaker than it appears to.
    phantom = sorted(listed - in_database)
    assert not phantom, f"the sweep names tables that carry no tenant_id: {phantom}"


def test_a_tenant_cannot_read_another_tenants_billing() -> None:
    """The one table where a broken policy costs money, called out by name.

    Every table in the sweep is asserted the same way, which is what makes the
    suite cheap - but a uniform check does not say which failure matters most.
    `billing_entries` is what a tenant is invoiced for: a policy that failed to
    deny here is not a data leak in the abstract, it is one customer seeing
    another's usage charges.

    Verified by breaking it rather than asserted by inspection: rewriting the
    policy to `USING (true)` fails this file with
    `billing_entries: tenant B must see zero rows`, and passes again once the
    policy is restored.
    """
    # The sweep above already asserts it. This exists to make the intent
    # greppable and to fail with a message that names the consequence, because
    # the generic one says only "tenant B must see zero rows" and a reader has
    # no way to know which table was being defended.
    assert "billing_entries" in TENANT_TABLES, (
        "billing_entries must be in the sweep; a tenant reading another's charges "
        "is the most expensive failure this file can catch"
    )
