"""Run one agent run under the non-bypass app role, then read the timeline.

Why this exists rather than a curl: the deployed interactive worker opens its
unit of work with `session_scope()`, which resolves to the bootstrap owner
(a superuser with `rolbypassrls`), so RLS is off for the whole run. With RLS
off, `flag_service._load_flag` sees every tenant's row for a key and returns an
arbitrary one - measured: it returned another tenant's
`agent.business_read_enabled=False`, so the read-tool branch was skipped and the
receipt was never published.

This probe runs the same orchestrator, deps, selector, tool gateway and demo ERP
on a session bound to `platform_app`, in-process, without a worker.

It was written to demonstrate the card path while the worker still ran the whole
run on the owner role; that is fixed now (`InboxWorker` claims on
`queue_bookkeeping_session` and processes each event on `tenant_session`), so
`scripts/support_card_smoke.cjs` in ask mode is the better end-to-end check. What
this still isolates is **which half** broke: it drives the orchestrator directly,
so a failure here is the run itself while a failure only in ask mode is the
worker's wiring. It is also the working shape of "bind a tenant, run, read back"
for any future job that has no HTTP request behind it.

**It executes a real run, so it spends a model call and writes real rows** -
point it at a throwaway conversation, not one a customer is in. Run it inside the
worker container:

    docker cp scripts/_probe_card_under_app_role.py <worker>:/tmp/probe.py
    docker exec -e PYTHONUNBUFFERED=1 <worker> python /tmp/probe.py
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

TENANT = uuid.UUID("17ab2c52-7d95-5fba-a06c-b5641393831e")
QUESTION = "What is the status of order SO-9001?"


async def main() -> None:
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator
    from platform_core.db import app_role_url, session_scope_with_url
    from platform_core.identity import lease_service
    from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
    from platform_core.retrieval.hybrid import PrincipalScope
    from worker.wiring import build_interactive_deps

    deps = build_interactive_deps()
    conv = uuid.uuid4()

    async def acquire() -> int:
        async with session_scope_with_url(app_role_url()) as session:
            await apply_rls_tenant(
                session, TenantContext(tenant_id=TENANT, actor_id=None, actor_kind="system")
            )
            lease = await lease_service.acquire_or_get(
                session, tenant_id=TENANT, conversation_ref_id=conv
            )
            return int(lease.lease_version)

    version = await acquire()
    print("lease ok, version", version, "conv", conv)

    async with session_scope_with_url(app_role_url()) as session:
        # Re-bound after the lease's own commit: `set_config(..., true)` is
        # transaction-scoped, so a commit drops it and every read after that
        # sees no rows at all through RLS.
        await apply_rls_tenant(
            session, TenantContext(tenant_id=TENANT, actor_id=None, actor_kind="system")
        )
        orch = AgentOrchestrator(session, deps)
        outcome = await orch.run(
            tenant_id=TENANT,
            conversation_ref_id=conv,
            question=QUESTION,
            principal=PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",)),
            expected_lease_version=version,
            chatwoot_account_id="1",
            chatwoot_conversation_id="1",
            history=[],
        )
        print("outcome:", outcome.status.value, outcome.route, outcome.abstain_reason)

    # What the visitor surface would read back.
    async with session_scope_with_url(app_role_url()) as session:
        await apply_rls_tenant(
            session, TenantContext(tenant_id=TENANT, actor_id=None, actor_kind="system")
        )
        from platform_core.agent_runtime import chat_service

        items = await chat_service.read_timeline(session, ref_id=conv, limit=50)

    for item in items:
        card = item.get("card")
        print(f"[{item['role']:8}] card={'YES' if card else 'no '} {item['text'][:110]!r}")
        if card:
            print("           ", json.dumps(card, ensure_ascii=False))
    print("visitor token:", end=" ")
    from platform_core.support_bridge.visitor_token import issue

    token, _exp = issue(TENANT, conv, f"probe-{time.time():.0f}")
    print(token[:40] + "...")


asyncio.run(main())
