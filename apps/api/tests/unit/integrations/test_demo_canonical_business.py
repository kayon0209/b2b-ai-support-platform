"""The local canonical authority fixture remains explicit synthetic data."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid5

import pytest

from platform_contracts.business_systems import (
    AuthorityBinding,
    AuthorityDomain,
    BusinessSystemKind,
)
from platform_core.integrations.canonical_business import (
    CANONICAL_FACT_TYPES,
    BusinessAdapterError,
    read_verified_fact,
)
from platform_core.integrations.demo_canonical_business import (
    DEMO_ACCOUNT_REF,
    DEMO_CANONICAL_SOURCE_VERSION,
    DemoCanonicalBusinessAdapter,
    demo_record_ref,
)

TENANT = UUID("0190d300-0000-7000-8000-000000000001")
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

_DOMAIN_SYSTEMS = {
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
_ACCOUNT_SCOPED = frozenset(
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


@pytest.fixture()
def demo_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from platform_core import config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(environment="test", business_api_adapter="demo"),
    )


def _binding(domain: AuthorityDomain, system: BusinessSystemKind | None = None) -> AuthorityBinding:
    domain_name = domain.value
    return AuthorityBinding(
        tenant_id=TENANT,
        binding_id=uuid5(TENANT, f"binding:{domain_name}"),
        domain=domain,
        system_kind=system or _DOMAIN_SYSTEMS[domain],
        connector_id=uuid5(TENANT, f"connector:{domain_name}"),
        binding_version=1,
        max_age_seconds=3600,
        approved_by=uuid5(TENANT, "synthetic-demo-reviewer"),
        approved_at=NOW - timedelta(days=1),
    )


@pytest.mark.parametrize("domain", list(AuthorityDomain))
def test_demo_canonical_adapter_returns_fresh_tenant_bound_records(
    demo_environment: None,
    domain: AuthorityDomain,
) -> None:
    binding = _binding(domain)
    adapter = DemoCanonicalBusinessAdapter(clock=lambda: NOW)
    expected_owner = DEMO_ACCOUNT_REF if domain in _ACCOUNT_SCOPED else None

    fact = asyncio.run(
        read_verified_fact(
            adapter,
            binding=binding,
            record_ref=demo_record_ref(domain),
            as_of=NOW,
            expected_account_ref=expected_owner,
        )
    )

    assert fact is not None
    assert isinstance(fact, CANONICAL_FACT_TYPES[domain])
    assert fact.tenant_id == TENANT
    assert fact.source.tenant_id == TENANT
    assert fact.source.source_record_ref == demo_record_ref(domain)
    assert fact.source.source_version == DEMO_CANONICAL_SOURCE_VERSION
    assert fact.source.is_fresh(as_of=NOW)
    if domain is AuthorityDomain.INVENTORY:
        assert fact.available_quantity == Decimal("120")
        assert fact.customer_account_ref == DEMO_ACCOUNT_REF


def test_demo_adapter_fails_closed_for_wrong_account_and_domain_system_pair(
    demo_environment: None,
) -> None:
    adapter = DemoCanonicalBusinessAdapter(clock=lambda: NOW)
    order_binding = _binding(AuthorityDomain.ORDER)
    with pytest.raises(BusinessAdapterError, match="BUSINESS_OWNERSHIP_MISMATCH"):
        asyncio.run(
            read_verified_fact(
                adapter,
                binding=order_binding,
                record_ref=demo_record_ref(AuthorityDomain.ORDER),
                as_of=NOW,
                expected_account_ref="OTHER-DEMO",
            )
        )

    invalid_binding = _binding(AuthorityDomain.ORDER, BusinessSystemKind.CRM)
    with pytest.raises(BusinessAdapterError, match="BUSINESS_DEMO_DOMAIN_SYSTEM_MISMATCH"):
        asyncio.run(
            adapter.read_fact(
                binding=invalid_binding,
                record_ref=demo_record_ref(AuthorityDomain.ORDER),
            )
        )


def test_demo_adapter_is_unavailable_outside_local_and_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from platform_core import config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(environment="staging", business_api_adapter="demo"),
    )
    with pytest.raises(BusinessAdapterError, match="DEMO_BUSINESS_AUTHORITY_DISABLED"):
        DemoCanonicalBusinessAdapter(clock=lambda: NOW)


def test_demo_erp_stock_read_returns_a_marked_synthetic_record() -> None:
    from platform_core.integrations.demo_erp import DemoBusinessToolExecutor

    executor = DemoBusinessToolExecutor(context=object())
    output = asyncio.run(
        executor.execute(
            "inventory.check_stock",
            {"part_number": "PCB-DEMO-100"},
            "demo-stock-read",
        )
    )

    assert output is not None
    assert output["source"] == "demo"
    assert output["part_number"] == "PCB-DEMO-100"
    assert output["available_quantity"] == 120
    assert output["unit_code"] == "pcs"


def test_demo_presales_bundle_requires_three_fresh_account_scoped_sources(
    demo_environment: None,
) -> None:
    from platform_core.integrations.demo_presales import build_demo_presales_evidence

    adapter = DemoCanonicalBusinessAdapter(clock=lambda: NOW)
    domains = (
        AuthorityDomain.PRODUCT_SPECIFICATION,
        AuthorityDomain.INVENTORY,
        AuthorityDomain.QUOTE,
    )
    bindings = {domain: _binding(domain) for domain in domains}
    evidence = asyncio.run(
        build_demo_presales_evidence(
            adapter,
            bindings=bindings,
            product_ref="PCB-DEMO-100",
            expected_account_ref=DEMO_ACCOUNT_REF,
            as_of=NOW,
        )
    )

    assert evidence.product_ref == "PCB-DEMO-100"
    assert evidence.specifications["layer_count"] == 8
    assert evidence.available_quantity == Decimal("120")
    assert evidence.indicative_unit_price_minor == 1250
    assert evidence.synthetic is True
    assert evidence.customer_quote_allowed is False
    assert evidence.handoff_required is True
    assert {
        evidence.product_source_version,
        evidence.inventory_source_version,
        evidence.quote_source_version,
    } == {DEMO_CANONICAL_SOURCE_VERSION}

    with pytest.raises(BusinessAdapterError, match="BUSINESS_OWNERSHIP_MISMATCH"):
        asyncio.run(
            build_demo_presales_evidence(
                adapter,
                bindings=bindings,
                product_ref="PCB-DEMO-100",
                expected_account_ref="OTHER-DEMO",
                as_of=NOW,
            )
        )
