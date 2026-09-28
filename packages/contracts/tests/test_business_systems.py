"""R3 canonical ERP/MES/WMS/CRM and guarded-write contract tests."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from platform_contracts.business_systems import (
    AuthorityBinding,
    AuthorityDomain,
    BusinessSystemKind,
    BusinessWriteReason,
    BusinessWriteStatus,
    CanonicalCustomerAccount,
    CanonicalInventorySnapshot,
    CanonicalInvoiceStatus,
    CanonicalOpportunity,
    CanonicalOrderStatus,
    CanonicalProductSpecification,
    CanonicalQuote,
    CanonicalShipmentStatus,
    CanonicalWorkOrderStatus,
    ExternalWriteReceipt,
    OwnershipProof,
    SourceMetadata,
)

TENANT = UUID("01900000-0000-7000-8000-000000000001")
CONNECTOR = UUID("01900000-0000-7000-8000-000000000002")
BINDING = UUID("01900000-0000-7000-8000-000000000003")
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def _source(**overrides) -> SourceMetadata:
    values = {
        "tenant_id": TENANT,
        "authority_binding_id": BINDING,
        "authority_version": 1,
        "connector_id": CONNECTOR,
        "system_kind": BusinessSystemKind.ERP,
        "source_record_ref": "record-42",
        "source_version": "revision-7",
        "retrieved_at": NOW,
        "valid_until": NOW + timedelta(minutes=10),
    }
    values.update(overrides)
    return SourceMetadata(**values)


def test_authority_binding_is_tenant_owned_versioned_and_explicitly_approved() -> None:
    binding = AuthorityBinding(
        tenant_id=TENANT,
        binding_id=BINDING,
        domain=AuthorityDomain.PRODUCT_SPECIFICATION,
        system_kind=BusinessSystemKind.MES,
        connector_id=CONNECTOR,
        binding_version=1,
        max_age_seconds=3600,
        approved_by=UUID("01900000-0000-7000-8000-000000000004"),
        approved_at=NOW,
    )

    assert binding.canonical_schema_version == "1.1"
    assert binding.max_age_seconds == 3600
    assert binding.freshness_deadline(retrieved_at=NOW) == NOW + timedelta(hours=1)
    assert binding.freshness_deadline(
        retrieved_at=NOW,
        source_valid_until=NOW + timedelta(minutes=15),
    ) == NOW + timedelta(minutes=15)
    with pytest.raises(ValidationError):
        AuthorityBinding.model_validate({**binding.model_dump(), "tenant_id": None})


def test_source_metadata_requires_utc_and_has_a_bounded_freshness_window() -> None:
    source = _source()
    assert source.is_fresh(as_of=NOW + timedelta(seconds=1))
    assert not source.is_fresh(as_of=NOW + timedelta(minutes=10))
    assert not source.is_fresh(as_of=NOW - timedelta(seconds=1))
    assert not source.is_fresh(as_of=datetime.now())

    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        _source(retrieved_at=datetime(2026, 9, 27, 20, 0, tzinfo=timezone(timedelta(hours=8))))
    with pytest.raises(ValidationError, match="valid_until must follow"):
        _source(valid_until=NOW)


def test_product_spec_has_canonical_pcb_fields_and_no_raw_provider_payload() -> None:
    product = CanonicalProductSpecification(
        tenant_id=TENANT,
        product_ref="product-123",
        part_number="PCB-123",
        revision="B",
        name="Industrial controller board",
        product_kind="pcb",
        specifications={
            "layer_count": 8,
            "board_thickness_um": 1600,
            "material_code": "FR4-TG170",
            "surface_finish": "ENIG",
        },
        source=_source(),
    )
    assert product.specifications["layer_count"] == 8

    with pytest.raises(ValidationError):
        CanonicalProductSpecification.model_validate(
            {**product.model_dump(), "raw_provider_payload": {"customer_note": "private"}}
        )
    with pytest.raises(ValidationError, match="tenant must match"):
        CanonicalProductSpecification(
            **{**product.model_dump(), "tenant_id": UUID("01900000-0000-7000-8000-000000000005")}
        )


def test_inventory_and_quote_require_units_provenance_and_source_expiry() -> None:
    inventory = CanonicalInventorySnapshot(
        tenant_id=TENANT,
        product_ref="component-007",
        customer_account_ref="account-9",
        location_ref="warehouse-east",
        available_quantity=Decimal("1250.5"),
        unit_code="pcs",
        source=_source(),
    )
    quote = CanonicalQuote(
        tenant_id=TENANT,
        customer_account_ref="account-9",
        product_ref="pcb-123",
        quote_ref="quote-9",
        revision="3",
        currency="CNY",
        unit_price_minor=2850,
        minimum_quantity=Decimal("500"),
        lead_time_business_days=12,
        valid_until=NOW + timedelta(days=7),
        source=_source(),
    )

    assert inventory.available_quantity == Decimal("1250.5")
    assert quote.unit_price_minor == 2850
    with pytest.raises(ValidationError):
        quote.unit_price_minor = 3000  # type: ignore[misc]

    with pytest.raises(ValidationError):
        CanonicalQuote(
            **{
                **quote.model_dump(),
                "currency": "cny",
            }
        )


def test_order_and_shipment_states_are_canonical_source_backed_facts() -> None:
    order = CanonicalOrderStatus(
        tenant_id=TENANT,
        customer_account_ref="account-9",
        order_ref="order-17",
        status="in_production",
        status_updated_at=NOW - timedelta(minutes=1),
        estimated_delivery_at=NOW + timedelta(days=4),
        source=_source(),
    )
    shipment = CanonicalShipmentStatus(
        tenant_id=TENANT,
        customer_account_ref="account-9",
        order_ref="order-17",
        shipment_ref="shipment-2",
        tracking_ref="track-9",
        status="in_transit",
        status_updated_at=NOW - timedelta(minutes=2),
        source=_source(),
    )
    assert order.status == "in_production"
    assert shipment.status == "in_transit"
    with pytest.raises(ValidationError):
        CanonicalOrderStatus(**{**order.model_dump(), "status": "almost_shipped"})
    with pytest.raises(ValidationError, match="later than source retrieval"):
        CanonicalShipmentStatus(
            **{**shipment.model_dump(), "status_updated_at": NOW + timedelta(seconds=1)}
        )


def test_customer_account_work_order_invoice_and_crm_contracts_are_typed() -> None:
    account = CanonicalCustomerAccount(
        tenant_id=TENANT,
        account_ref="account-9",
        status="active",
        source=_source(),
    )
    work_order = CanonicalWorkOrderStatus(
        tenant_id=TENANT,
        customer_account_ref="account-9",
        work_order_ref="work-11",
        product_ref="pcb-123",
        revision="B",
        status="quality_hold",
        quality_status="hold",
        status_updated_at=NOW - timedelta(minutes=1),
        source=_source(),
    )
    invoice = CanonicalInvoiceStatus(
        tenant_id=TENANT,
        customer_account_ref="account-9",
        invoice_ref="invoice-31",
        invoice_type="tax",
        status="issued",
        currency="CNY",
        total_amount_minor=120050,
        issued_at=NOW - timedelta(days=1),
        due_at=NOW + timedelta(days=20),
        source=_source(),
    )
    opportunity = CanonicalOpportunity(
        tenant_id=TENANT,
        customer_account_ref="account-9",
        opportunity_ref="opportunity-8",
        stage="proposal",
        product_refs=("pcb-123", "component-7"),
        quote_ref="quote-9",
        status_updated_at=NOW - timedelta(minutes=3),
        source=_source(),
    )
    assert account.status == "active"
    assert work_order.quality_status == "hold"
    assert invoice.total_amount_minor == 120050
    assert opportunity.stage == "proposal"

    with pytest.raises(ValidationError, match="amount and currency"):
        CanonicalInvoiceStatus(
            tenant_id=TENANT,
            customer_account_ref="account-9",
            invoice_ref="invoice-32",
            invoice_type="tax",
            status="issued",
            total_amount_minor=1200,
            source=_source(),
        )


def test_external_record_must_prove_account_ownership() -> None:
    proof = OwnershipProof(
        tenant_id=TENANT,
        connector_id=CONNECTOR,
        authority_binding_id=BINDING,
        authority_version=1,
        resource_ref="record-42",
        expected_account_ref="account-9",
        observed_account_ref="account-9",
        verified_at=NOW,
        method="provider_owner_field",
    )
    assert proof.expected_account_ref == proof.observed_account_ref

    with pytest.raises(ValidationError, match="ownership does not match"):
        OwnershipProof(**{**proof.model_dump(), "observed_account_ref": "another-account"})


def test_write_receipt_never_calls_an_unknown_timeout_a_success() -> None:
    unknown = ExternalWriteReceipt(
        tenant_id=TENANT,
        connector_id=CONNECTOR,
        action="crm.opportunity.create",
        status=BusinessWriteStatus.UNKNOWN,
        idempotency_key_hash="a" * 64,
        reason_code=BusinessWriteReason.PROVIDER_TIMEOUT,
    )
    assert unknown.status is BusinessWriteStatus.UNKNOWN
    assert unknown.postcondition_verified is False

    with pytest.raises(ValidationError, match="verified postcondition"):
        ExternalWriteReceipt(
            tenant_id=TENANT,
            connector_id=CONNECTOR,
            action="crm.opportunity.create",
            status=BusinessWriteStatus.VERIFIED_APPLIED,
            idempotency_key_hash="b" * 64,
            reason_code=BusinessWriteReason.POSTCONDITION_VERIFIED,
        )
    with pytest.raises(ValidationError, match="inconsistent"):
        ExternalWriteReceipt(
            tenant_id=TENANT,
            connector_id=CONNECTOR,
            action="order.change_delivery_date",
            status=BusinessWriteStatus.UNKNOWN,
            idempotency_key_hash="c" * 64,
            reason_code=BusinessWriteReason.POSTCONDITION_VERIFIED,
        )

    verified = ExternalWriteReceipt(
        tenant_id=TENANT,
        connector_id=CONNECTOR,
        action="crm.opportunity.create",
        status=BusinessWriteStatus.VERIFIED_APPLIED,
        idempotency_key_hash="d" * 64,
        external_record_ref="opp-101",
        postcondition_verified=True,
        observed_source_version="12",
        reason_code=BusinessWriteReason.POSTCONDITION_VERIFIED,
    )
    assert verified.postcondition_verified is True
