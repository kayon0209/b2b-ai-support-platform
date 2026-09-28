"""Print the synthetic nine-domain R3 authority and pre-sales scenario.

This is an in-memory local/test demonstration only. It makes no database or
network calls, creates no persisted authority binding, and does not generate
an enterprise approval, customer quote, CRM opportunity, or business write.

Run from the repository root with APP_ENVIRONMENT=local (or test) and
APP_BUSINESS_API_ADAPTER=demo. The output labels every row as synthetic.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

REPO_ROOT = Path(__file__).resolve().parents[1]
for _relative in (
    "apps/api/src",
    "packages/contracts/src",
    "packages/policy/src",
    "packages/observability/src",
):
    _path = str(REPO_ROOT / _relative)
    if Path(_path).is_dir() and _path not in sys.path:
        sys.path.insert(0, _path)

os.environ.setdefault("APP_ENVIRONMENT", "local")
os.environ.setdefault("APP_BUSINESS_API_ADAPTER", "demo")
os.environ.setdefault("APP_ALLOW_BOOTSTRAP_TOKENS", "true")

from platform_contracts.business_systems import (  # noqa: E402
    AuthorityBinding,
    AuthorityDomain,
    BusinessSystemKind,
)
from platform_core.integrations.canonical_business import read_verified_fact  # noqa: E402
from platform_core.integrations.demo_canonical_business import (  # noqa: E402
    DEMO_ACCOUNT_REF,
    DemoCanonicalBusinessAdapter,
    demo_record_ref,
)
from platform_core.integrations.demo_presales import (  # noqa: E402
    build_demo_presales_evidence,
)

DEMO_TENANT = UUID("0190d300-0000-7000-8000-000000000101")
DEMO_ACTOR = uuid5(DEMO_TENANT, "demo-only-binding-fixture")
DOMAIN_SYSTEM = {
    AuthorityDomain.CUSTOMER_ACCOUNT: BusinessSystemKind.CRM,
    AuthorityDomain.ORDER: BusinessSystemKind.ERP,
    AuthorityDomain.INVOICE: BusinessSystemKind.ERP,
    AuthorityDomain.WORK_ORDER: BusinessSystemKind.MES,
    AuthorityDomain.SHIPMENT: BusinessSystemKind.WMS,
    AuthorityDomain.OPPORTUNITY: BusinessSystemKind.CRM,
    AuthorityDomain.PRODUCT_SPECIFICATION: BusinessSystemKind.PLM,
    AuthorityDomain.INVENTORY: BusinessSystemKind.WMS,
    AuthorityDomain.QUOTE: BusinessSystemKind.CRM,
}
ACCOUNT_SCOPED = frozenset(
    {
        AuthorityDomain.ORDER,
        AuthorityDomain.INVOICE,
        AuthorityDomain.WORK_ORDER,
        AuthorityDomain.SHIPMENT,
        AuthorityDomain.OPPORTUNITY,
        AuthorityDomain.INVENTORY,
        AuthorityDomain.QUOTE,
    }
)


def _bindings() -> dict[AuthorityDomain, AuthorityBinding]:
    now = datetime.now(UTC)
    return {
        domain: AuthorityBinding(
            tenant_id=DEMO_TENANT,
            binding_id=uuid5(DEMO_TENANT, f"binding:{domain.value}"),
            domain=domain,
            system_kind=system,
            connector_id=uuid5(DEMO_TENANT, f"connector:{domain.value}"),
            binding_version=1,
            max_age_seconds=3600,
            approved_by=DEMO_ACTOR,
            approved_at=now - timedelta(seconds=1),
        )
        for domain, system in DOMAIN_SYSTEM.items()
    }


async def _run() -> None:
    bindings = _bindings()
    adapter = DemoCanonicalBusinessAdapter()
    as_of = datetime.now(UTC)
    facts = []
    for domain, binding in bindings.items():
        fact = await read_verified_fact(
            adapter,
            binding=binding,
            record_ref=demo_record_ref(domain),
            as_of=as_of,
            expected_account_ref=(DEMO_ACCOUNT_REF if domain in ACCOUNT_SCOPED else None),
        )
        facts.append(
            {
                "domain": domain.value,
                "record_ref": fact.source.source_record_ref if fact else None,
                "source_version": fact.source.source_version if fact else None,
                "fact": fact.model_dump(mode="json") if fact else None,
            }
        )

    presales = await build_demo_presales_evidence(
        adapter,
        bindings=bindings,
        product_ref="PCB-DEMO-100",
        expected_account_ref=DEMO_ACCOUNT_REF,
        as_of=datetime.now(UTC),
    )
    print(
        json.dumps(
            {
                "classification": "synthetic_demo_only",
                "authority_binding_approval": "ephemeral fixture; no human approval",
                "customer_quote_allowed": False,
                "crm_write_performed": False,
                "facts": facts,
                "presales_evidence": presales.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(_run())
