"""R3 fake-adapter contract checks; they make no live provider claim."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from platform_contracts.business_systems import (
    AuthorityBinding,
    AuthorityDomain,
    BusinessSystemKind,
    CanonicalBusinessReadResult,
    CanonicalCustomerAccount,
    CanonicalInventorySnapshot,
    CanonicalInvoiceStatus,
    CanonicalOpportunity,
    CanonicalOrderStatus,
    CanonicalProductSpecification,
    CanonicalQuote,
    CanonicalShipmentStatus,
    CanonicalWorkOrderStatus,
    OwnershipProof,
    SourceMetadata,
)
from platform_core.integrations.canonical_business import (
    CANONICAL_FACT_TYPES,
    BusinessAdapterError,
    CanonicalBusinessAdapter,
    CanonicalBusinessFact,
    read_verified_fact,
    validate_canonical_fact,
)

TENANT = UUID("01900000-0000-7000-8000-000000000001")
CONNECTOR = UUID("01900000-0000-7000-8000-000000000002")
BINDING = UUID("01900000-0000-7000-8000-000000000003")
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _binding(
    *,
    domain: AuthorityDomain = AuthorityDomain.PRODUCT_SPECIFICATION,
    max_age_seconds: int = 3600,
) -> AuthorityBinding:
    return AuthorityBinding(
        tenant_id=TENANT,
        binding_id=BINDING,
        domain=domain,
        system_kind=BusinessSystemKind.MES,
        connector_id=CONNECTOR,
        binding_version=4,
        max_age_seconds=max_age_seconds,
        approved_by=UUID("01900000-0000-7000-8000-000000000004"),
        approved_at=NOW - timedelta(days=1),
    )


def _source(**overrides) -> SourceMetadata:
    values = {
        "tenant_id": TENANT,
        "authority_binding_id": BINDING,
        "authority_version": 4,
        "connector_id": CONNECTOR,
        "system_kind": BusinessSystemKind.MES,
        "source_record_ref": "part-42",
        "source_version": "rev-4",
        "retrieved_at": NOW - timedelta(minutes=5),
        "valid_until": NOW + timedelta(minutes=20),
    }
    values.update(overrides)
    return SourceMetadata(**values)


class _FakeMesAdapter:
    def __init__(
        self,
        result: CanonicalBusinessReadResult,
        *,
        expected_record_ref: str = "part-42",
    ) -> None:
        self.result = result
        self.expected_record_ref = expected_record_ref

    async def read_fact(
        self, *, binding: AuthorityBinding, record_ref: str
    ) -> CanonicalBusinessReadResult | None:
        assert binding.connector_id == CONNECTOR
        assert record_ref == self.expected_record_ref
        return self.result


def _product(source: SourceMetadata | None = None) -> CanonicalProductSpecification:
    return CanonicalProductSpecification(
        tenant_id=TENANT,
        product_ref="pcb-42",
        part_number="PCB-42",
        revision="D",
        name="Control board",
        product_kind="pcb",
        specifications={"layer_count": 8, "board_thickness_um": 1600},
        source=source or _source(),
    )


def test_fake_adapter_returns_a_fresh_tenant_bound_canonical_record() -> None:
    adapter: CanonicalBusinessAdapter = _FakeMesAdapter(
        CanonicalBusinessReadResult(fact=_product())
    )
    fact = asyncio.run(
        read_verified_fact(
            adapter,
            binding=_binding(),
            record_ref="part-42",
            as_of=NOW,
        )
    )
    assert fact is not None
    assert fact.product_ref == "pcb-42"


def test_record_cannot_cross_tenant_connector_or_authority_version() -> None:
    with pytest.raises(BusinessAdapterError, match="BUSINESS_TENANT_MISMATCH"):
        validate_canonical_fact(
            binding=AuthorityBinding(
                **{
                    **_binding().model_dump(),
                    "tenant_id": UUID("01900000-0000-7000-8000-000000000099"),
                }
            ),
            fact=_product(),
            expected_record_ref="part-42",
            as_of=NOW,
        )

    with pytest.raises(BusinessAdapterError, match="BUSINESS_AUTHORITY_MISMATCH"):
        validate_canonical_fact(
            binding=_binding(),
            fact=_product(_source(connector_id=UUID("01900000-0000-7000-8000-000000000098"))),
            expected_record_ref="part-42",
            as_of=NOW,
        )


def test_stale_and_ttl_exceeding_provider_data_are_refused() -> None:
    stale = _source(
        retrieved_at=NOW - timedelta(hours=2),
        valid_until=NOW - timedelta(hours=1),
    )
    with pytest.raises(BusinessAdapterError, match="BUSINESS_SOURCE_STALE"):
        validate_canonical_fact(
            binding=_binding(),
            fact=_product(stale),
            expected_record_ref="part-42",
            as_of=NOW,
        )

    too_long = _source(valid_until=NOW + timedelta(days=2))
    with pytest.raises(BusinessAdapterError, match="BUSINESS_SOURCE_TTL_EXCEEDED"):
        validate_canonical_fact(
            binding=_binding(max_age_seconds=3600),
            fact=_product(too_long),
            expected_record_ref="part-42",
            as_of=NOW,
        )


def test_customer_scoped_quote_requires_matching_ownership_proof() -> None:
    quote = CanonicalQuote(
        tenant_id=TENANT,
        customer_account_ref="account-42",
        product_ref="pcb-42",
        quote_ref="quote-9",
        revision="2",
        currency="CNY",
        unit_price_minor=1200,
        minimum_quantity=Decimal("100"),
        valid_until=NOW + timedelta(days=1),
        source=_source(source_record_ref="quote-9"),
    )
    binding = _binding(domain=AuthorityDomain.QUOTE)
    with pytest.raises(BusinessAdapterError, match="BUSINESS_OWNERSHIP_UNVERIFIED"):
        validate_canonical_fact(
            binding=binding,
            fact=quote,
            expected_record_ref="quote-9",
            as_of=NOW,
            expected_account_ref="account-42",
        )

    proof = OwnershipProof(
        tenant_id=TENANT,
        connector_id=CONNECTOR,
        authority_binding_id=BINDING,
        authority_version=4,
        resource_ref="quote-9",
        expected_account_ref="account-42",
        observed_account_ref="account-42",
        verified_at=NOW - timedelta(seconds=1),
        method="provider_owner_field",
    )
    validate_canonical_fact(
        binding=binding,
        fact=quote,
        expected_record_ref="quote-9",
        as_of=NOW,
        expected_account_ref="account-42",
        ownership_proof=proof,
    )


def test_every_authority_domain_has_a_canonical_read_contract() -> None:
    facts: dict[AuthorityDomain, CanonicalBusinessFact] = {
        AuthorityDomain.CUSTOMER_ACCOUNT: CanonicalCustomerAccount(
            tenant_id=TENANT, account_ref="account-42", status="active", source=_source()
        ),
        AuthorityDomain.ORDER: CanonicalOrderStatus(
            tenant_id=TENANT,
            customer_account_ref="account-42",
            order_ref="order-42",
            status="accepted",
            status_updated_at=NOW - timedelta(minutes=6),
            source=_source(),
        ),
        AuthorityDomain.INVOICE: CanonicalInvoiceStatus(
            tenant_id=TENANT,
            customer_account_ref="account-42",
            invoice_ref="invoice-42",
            invoice_type="tax",
            status="issued",
            source=_source(),
        ),
        AuthorityDomain.WORK_ORDER: CanonicalWorkOrderStatus(
            tenant_id=TENANT,
            customer_account_ref="account-42",
            work_order_ref="work-42",
            product_ref="pcb-42",
            revision="D",
            status="in_production",
            quality_status="pending",
            status_updated_at=NOW - timedelta(minutes=6),
            source=_source(),
        ),
        AuthorityDomain.SHIPMENT: CanonicalShipmentStatus(
            tenant_id=TENANT,
            customer_account_ref="account-42",
            order_ref="order-42",
            shipment_ref="shipment-42",
            status="in_transit",
            status_updated_at=NOW - timedelta(minutes=6),
            source=_source(),
        ),
        AuthorityDomain.OPPORTUNITY: CanonicalOpportunity(
            tenant_id=TENANT,
            customer_account_ref="account-42",
            opportunity_ref="opportunity-42",
            stage="qualified",
            status_updated_at=NOW - timedelta(minutes=6),
            source=_source(),
        ),
        AuthorityDomain.PRODUCT_SPECIFICATION: _product(),
        AuthorityDomain.INVENTORY: CanonicalInventorySnapshot(
            tenant_id=TENANT,
            product_ref="pcb-42",
            location_ref="warehouse-1",
            available_quantity=Decimal("8"),
            unit_code="pcs",
            source=_source(),
        ),
        AuthorityDomain.QUOTE: CanonicalQuote(
            tenant_id=TENANT,
            customer_account_ref="account-42",
            product_ref="pcb-42",
            quote_ref="quote-42",
            revision="1",
            currency="CNY",
            unit_price_minor=1000,
            minimum_quantity=Decimal("10"),
            valid_until=NOW + timedelta(days=1),
            source=_source(),
        ),
    }
    assert set(CANONICAL_FACT_TYPES) == set(AuthorityDomain)
    for domain, fact in facts.items():
        owner_ref = getattr(fact, "customer_account_ref", None)
        proof = (
            OwnershipProof(
                tenant_id=TENANT,
                connector_id=CONNECTOR,
                authority_binding_id=BINDING,
                authority_version=4,
                resource_ref="part-42",
                expected_account_ref=owner_ref,
                observed_account_ref=owner_ref,
                verified_at=NOW - timedelta(minutes=1),
                method="provider_owner_field",
            )
            if owner_ref is not None
            else None
        )
        validate_canonical_fact(
            binding=_binding(domain=domain),
            fact=fact,
            expected_record_ref="part-42",
            as_of=NOW,
            expected_account_ref=owner_ref,
            ownership_proof=proof,
        )
    with pytest.raises(TypeError):
        CANONICAL_FACT_TYPES[AuthorityDomain.ORDER] = CanonicalProductSpecification  # type: ignore[index]
    with pytest.raises(BusinessAdapterError, match="BUSINESS_FACT_TYPE_MISMATCH"):
        validate_canonical_fact(
            binding=_binding(domain=AuthorityDomain.ORDER),
            fact=_product(),
            expected_record_ref="part-42",
            as_of=NOW,
        )


def test_order_and_invoice_require_same_customer_ownership_proof() -> None:
    order = CanonicalOrderStatus(
        tenant_id=TENANT,
        customer_account_ref="account-42",
        order_ref="order-42",
        status="accepted",
        status_updated_at=NOW - timedelta(minutes=6),
        source=_source(),
    )
    proof = OwnershipProof(
        tenant_id=TENANT,
        connector_id=CONNECTOR,
        authority_binding_id=BINDING,
        authority_version=4,
        resource_ref="part-42",
        expected_account_ref="another-account",
        observed_account_ref="another-account",
        verified_at=NOW - timedelta(seconds=1),
        method="provider_owner_field",
    )
    invoice = CanonicalInvoiceStatus(
        tenant_id=TENANT,
        customer_account_ref="account-42",
        invoice_ref="invoice-42",
        invoice_type="tax",
        status="issued",
        source=_source(),
    )
    assert invoice.customer_account_ref == order.customer_account_ref
    for domain, fact in (
        (AuthorityDomain.ORDER, order),
        (AuthorityDomain.INVOICE, invoice),
    ):
        with pytest.raises(BusinessAdapterError, match="BUSINESS_OWNERSHIP_MISMATCH"):
            validate_canonical_fact(
                binding=_binding(domain=domain),
                fact=fact,
                expected_record_ref="part-42",
                as_of=NOW,
                expected_account_ref="another-account",
                ownership_proof=proof,
            )


def test_ownership_proof_is_bound_to_resource_version_and_freshness() -> None:
    order = CanonicalOrderStatus(
        tenant_id=TENANT,
        customer_account_ref="account-42",
        order_ref="order-42",
        status="accepted",
        status_updated_at=NOW - timedelta(minutes=6),
        source=_source(source_record_ref="order-42"),
    )
    binding = _binding(domain=AuthorityDomain.ORDER, max_age_seconds=3600)
    proof = OwnershipProof(
        tenant_id=TENANT,
        connector_id=CONNECTOR,
        authority_binding_id=BINDING,
        authority_version=4,
        resource_ref="order-42",
        expected_account_ref="account-42",
        observed_account_ref="account-42",
        verified_at=NOW - timedelta(minutes=6),
        method="provider_owner_field",
    )
    validate_canonical_fact(
        binding=binding,
        fact=order,
        expected_record_ref="order-42",
        as_of=NOW,
        expected_account_ref="account-42",
        ownership_proof=proof,
    )

    with pytest.raises(BusinessAdapterError, match="BUSINESS_OWNERSHIP_MISMATCH"):
        validate_canonical_fact(
            binding=binding,
            fact=order,
            expected_record_ref="order-42",
            as_of=NOW,
            expected_account_ref="account-42",
            ownership_proof=proof.model_copy(update={"resource_ref": "another-order"}),
        )
    with pytest.raises(BusinessAdapterError, match="BUSINESS_OWNERSHIP_MISMATCH"):
        validate_canonical_fact(
            binding=binding,
            fact=order,
            expected_record_ref="order-42",
            as_of=NOW,
            expected_account_ref="account-42",
            ownership_proof=proof.model_copy(update={"authority_version": 3}),
        )
    with pytest.raises(BusinessAdapterError, match="BUSINESS_OWNERSHIP_PROOF_STALE"):
        validate_canonical_fact(
            binding=binding,
            fact=order,
            expected_record_ref="order-42",
            as_of=NOW,
            expected_account_ref="account-42",
            ownership_proof=proof.model_copy(update={"verified_at": NOW - timedelta(hours=1)}),
        )


def test_read_result_carries_ownership_proof_into_the_enforcing_helper() -> None:
    quote = CanonicalQuote(
        tenant_id=TENANT,
        customer_account_ref="account-42",
        product_ref="pcb-42",
        quote_ref="quote-42",
        revision="2",
        currency="CNY",
        unit_price_minor=1200,
        minimum_quantity=Decimal("100"),
        valid_until=NOW + timedelta(days=1),
        source=_source(source_record_ref="quote-42"),
    )
    proof = OwnershipProof(
        tenant_id=TENANT,
        connector_id=CONNECTOR,
        authority_binding_id=BINDING,
        authority_version=4,
        resource_ref="quote-42",
        expected_account_ref="account-42",
        observed_account_ref="account-42",
        verified_at=NOW - timedelta(minutes=4),
        method="provider_owner_field",
    )
    adapter = _FakeMesAdapter(
        CanonicalBusinessReadResult(fact=quote, ownership_proof=proof),
        expected_record_ref="quote-42",
    )
    verified = asyncio.run(
        read_verified_fact(
            adapter,
            binding=_binding(domain=AuthorityDomain.QUOTE),
            record_ref="quote-42",
            as_of=NOW,
            expected_account_ref="account-42",
        )
    )
    assert verified == quote

    unbound_adapter = _FakeMesAdapter(
        CanonicalBusinessReadResult(
            fact=quote,
            ownership_proof=proof.model_copy(update={"resource_ref": "quote-elsewhere"}),
        ),
        expected_record_ref="quote-42",
    )
    with pytest.raises(BusinessAdapterError, match="BUSINESS_OWNERSHIP_MISMATCH"):
        asyncio.run(
            read_verified_fact(
                unbound_adapter,
                binding=_binding(domain=AuthorityDomain.QUOTE),
                record_ref="quote-42",
                as_of=NOW,
                expected_account_ref="account-42",
            )
        )


def test_provider_cannot_substitute_another_record_for_the_requested_ref() -> None:
    adapter = _FakeMesAdapter(
        CanonicalBusinessReadResult(fact=_product(_source(source_record_ref="part-43"))),
        expected_record_ref="part-42",
    )
    with pytest.raises(BusinessAdapterError, match="BUSINESS_RECORD_MISMATCH"):
        asyncio.run(
            read_verified_fact(
                adapter,
                binding=_binding(),
                record_ref="part-42",
                as_of=NOW,
            )
        )


def test_customer_scoped_fact_cannot_skip_account_scope_and_proof() -> None:
    order = CanonicalOrderStatus(
        tenant_id=TENANT,
        customer_account_ref="account-42",
        order_ref="order-42",
        status="accepted",
        status_updated_at=NOW - timedelta(minutes=6),
        source=_source(source_record_ref="order-42"),
    )
    with pytest.raises(BusinessAdapterError, match="BUSINESS_OWNERSHIP_UNVERIFIED"):
        validate_canonical_fact(
            binding=_binding(domain=AuthorityDomain.ORDER),
            fact=order,
            expected_record_ref="order-42",
            as_of=NOW,
        )
