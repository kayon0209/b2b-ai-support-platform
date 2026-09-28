"""Local/test-only synthetic authority for exercising the R3 canonical seam.

These records are invented fixtures. They are not connected to, approved by,
or evidence from an ERP/MES/WMS/CRM/PLM system. A caller must still supply a
tenant-scoped ``AuthorityBinding`` and pass every returned fact through
``read_verified_fact``; the source version keeps the synthetic provenance
visible in the resulting contract.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
    BusinessAdapterError,
    CanonicalBusinessFact,
)

DEMO_ACCOUNT_REF = "ACME-DEMO"
DEMO_CANONICAL_SOURCE_VERSION = "demo-fixture-v1"

_DOMAIN_SYSTEMS: dict[AuthorityDomain, frozenset[BusinessSystemKind]] = {
    AuthorityDomain.CUSTOMER_ACCOUNT: frozenset({BusinessSystemKind.CRM, BusinessSystemKind.ERP}),
    AuthorityDomain.ORDER: frozenset({BusinessSystemKind.ERP}),
    AuthorityDomain.INVOICE: frozenset({BusinessSystemKind.ERP}),
    AuthorityDomain.WORK_ORDER: frozenset({BusinessSystemKind.MES, BusinessSystemKind.ERP}),
    AuthorityDomain.SHIPMENT: frozenset({BusinessSystemKind.WMS, BusinessSystemKind.ERP}),
    AuthorityDomain.OPPORTUNITY: frozenset({BusinessSystemKind.CRM}),
    AuthorityDomain.PRODUCT_SPECIFICATION: frozenset(
        {BusinessSystemKind.PLM, BusinessSystemKind.MES, BusinessSystemKind.ERP}
    ),
    AuthorityDomain.INVENTORY: frozenset(
        {BusinessSystemKind.WMS, BusinessSystemKind.MES, BusinessSystemKind.ERP}
    ),
    AuthorityDomain.QUOTE: frozenset({BusinessSystemKind.CRM, BusinessSystemKind.ERP}),
}

_DEMO_RECORD_REFS: dict[AuthorityDomain, str] = {
    AuthorityDomain.CUSTOMER_ACCOUNT: DEMO_ACCOUNT_REF,
    AuthorityDomain.ORDER: "SO-DEMO-9001",
    AuthorityDomain.INVOICE: "INV-DEMO-1001",
    AuthorityDomain.WORK_ORDER: "WO-DEMO-2001",
    AuthorityDomain.SHIPMENT: "SH-DEMO-7001",
    AuthorityDomain.OPPORTUNITY: "OPP-DEMO-5001",
    AuthorityDomain.PRODUCT_SPECIFICATION: "PCB-DEMO-100",
    AuthorityDomain.INVENTORY: "PCB-DEMO-100",
    AuthorityDomain.QUOTE: "QUOTE-DEMO-3001",
}


def demo_record_ref(domain: AuthorityDomain) -> str:
    """The stable synthetic record identifier for a domain's example row."""
    return _DEMO_RECORD_REFS[domain]


class DemoCanonicalBusinessAdapter:
    """Return deterministic synthetic facts behind the canonical R3 contract."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        from platform_core.config import get_settings

        settings = get_settings()
        if (
            settings.environment not in ("local", "test")
            or settings.business_api_adapter.strip().lower() != "demo"
        ):
            raise BusinessAdapterError("DEMO_BUSINESS_AUTHORITY_DISABLED")
        self._clock = clock or (lambda: datetime.now(UTC))

    async def read_fact(
        self,
        *,
        binding: AuthorityBinding,
        record_ref: str,
    ) -> CanonicalBusinessReadResult | None:
        if binding.system_kind not in _DOMAIN_SYSTEMS[binding.domain]:
            raise BusinessAdapterError("BUSINESS_DEMO_DOMAIN_SYSTEM_MISMATCH")
        if record_ref != demo_record_ref(binding.domain):
            return None

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise BusinessAdapterError("BUSINESS_DEMO_CLOCK_NOT_UTC")
        retrieved_at = now - timedelta(seconds=1)
        source = SourceMetadata(
            tenant_id=binding.tenant_id,
            authority_binding_id=binding.binding_id,
            authority_version=binding.binding_version,
            connector_id=binding.connector_id,
            system_kind=binding.system_kind,
            source_record_ref=record_ref,
            source_version=DEMO_CANONICAL_SOURCE_VERSION,
            retrieved_at=retrieved_at,
            valid_until=binding.freshness_deadline(retrieved_at=retrieved_at),
        )
        fact, owner_ref = _demo_fact(
            binding=binding,
            source=source,
            retrieved_at=retrieved_at,
            record_ref=record_ref,
        )
        proof = (
            OwnershipProof(
                tenant_id=binding.tenant_id,
                connector_id=binding.connector_id,
                authority_binding_id=binding.binding_id,
                authority_version=binding.binding_version,
                resource_ref=record_ref,
                expected_account_ref=owner_ref,
                observed_account_ref=owner_ref,
                verified_at=retrieved_at,
                method="provider_owner_field",
            )
            if owner_ref is not None
            else None
        )
        return CanonicalBusinessReadResult(fact=fact, ownership_proof=proof)


def _demo_fact(
    *,
    binding: AuthorityBinding,
    source: SourceMetadata,
    retrieved_at: datetime,
    record_ref: str,
) -> tuple[CanonicalBusinessFact, str | None]:
    tenant_id = binding.tenant_id
    status_time = retrieved_at - timedelta(minutes=2)
    owner = DEMO_ACCOUNT_REF

    if binding.domain is AuthorityDomain.CUSTOMER_ACCOUNT:
        return CanonicalCustomerAccount(
            tenant_id=tenant_id,
            account_ref=record_ref,
            status="active",
            source=source,
        ), None
    if binding.domain is AuthorityDomain.ORDER:
        return CanonicalOrderStatus(
            tenant_id=tenant_id,
            customer_account_ref=owner,
            order_ref=record_ref,
            status="in_production",
            status_updated_at=status_time,
            estimated_delivery_at=retrieved_at + timedelta(days=4),
            source=source,
        ), owner
    if binding.domain is AuthorityDomain.INVOICE:
        return CanonicalInvoiceStatus(
            tenant_id=tenant_id,
            customer_account_ref=owner,
            invoice_ref=record_ref,
            invoice_type="tax",
            status="issued",
            currency="CNY",
            total_amount_minor=125000,
            issued_at=retrieved_at - timedelta(days=4),
            due_at=retrieved_at + timedelta(days=26),
            source=source,
        ), owner
    if binding.domain is AuthorityDomain.WORK_ORDER:
        return CanonicalWorkOrderStatus(
            tenant_id=tenant_id,
            customer_account_ref=owner,
            work_order_ref=record_ref,
            product_ref="PCB-DEMO-100",
            revision="A",
            status="in_production",
            quality_status="pending",
            status_updated_at=status_time,
            source=source,
        ), owner
    if binding.domain is AuthorityDomain.SHIPMENT:
        return CanonicalShipmentStatus(
            tenant_id=tenant_id,
            customer_account_ref=owner,
            order_ref="SO-DEMO-9001",
            shipment_ref=record_ref,
            tracking_ref="TRACK-DEMO-7001",
            status="in_transit",
            status_updated_at=status_time,
            source=source,
        ), owner
    if binding.domain is AuthorityDomain.OPPORTUNITY:
        return CanonicalOpportunity(
            tenant_id=tenant_id,
            customer_account_ref=owner,
            opportunity_ref=record_ref,
            stage="qualified",
            product_refs=("PCB-DEMO-100",),
            quote_ref="QUOTE-DEMO-3001",
            status_updated_at=status_time,
            source=source,
        ), owner
    if binding.domain is AuthorityDomain.PRODUCT_SPECIFICATION:
        return CanonicalProductSpecification(
            tenant_id=tenant_id,
            product_ref=record_ref,
            part_number=record_ref,
            revision="A",
            name="Demo controller board",
            product_kind="pcb",
            specifications={
                "layer_count": 8,
                "board_thickness_um": 1600,
                "surface_finish": "ENIG",
            },
            source=source,
        ), None
    if binding.domain is AuthorityDomain.INVENTORY:
        return CanonicalInventorySnapshot(
            tenant_id=tenant_id,
            product_ref=record_ref,
            customer_account_ref=owner,
            location_ref="WH-DEMO-1",
            available_quantity=Decimal("120"),
            unit_code="pcs",
            source=source,
        ), owner
    if binding.domain is AuthorityDomain.QUOTE:
        return CanonicalQuote(
            tenant_id=tenant_id,
            customer_account_ref=owner,
            product_ref="PCB-DEMO-100",
            quote_ref=record_ref,
            revision="1",
            currency="CNY",
            unit_price_minor=1250,
            minimum_quantity=Decimal("100"),
            lead_time_business_days=12,
            valid_until=retrieved_at + timedelta(days=14),
            source=source,
        ), owner
    raise BusinessAdapterError("BUSINESS_DEMO_DOMAIN_UNSUPPORTED")


__all__ = [
    "DEMO_ACCOUNT_REF",
    "DEMO_CANONICAL_SOURCE_VERSION",
    "DemoCanonicalBusinessAdapter",
    "demo_record_ref",
]
