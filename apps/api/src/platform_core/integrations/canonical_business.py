"""Tenant-bound canonical projection and provider-neutral adapter seam for R3."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Protocol

from platform_contracts.business_systems import (
    AuthorityBinding,
    AuthorityDomain,
    CanonicalBusinessFact,
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
)

CANONICAL_FACT_TYPES = MappingProxyType(
    {
        AuthorityDomain.CUSTOMER_ACCOUNT: CanonicalCustomerAccount,
        AuthorityDomain.ORDER: CanonicalOrderStatus,
        AuthorityDomain.INVOICE: CanonicalInvoiceStatus,
        AuthorityDomain.WORK_ORDER: CanonicalWorkOrderStatus,
        AuthorityDomain.SHIPMENT: CanonicalShipmentStatus,
        AuthorityDomain.OPPORTUNITY: CanonicalOpportunity,
        AuthorityDomain.PRODUCT_SPECIFICATION: CanonicalProductSpecification,
        AuthorityDomain.INVENTORY: CanonicalInventorySnapshot,
        AuthorityDomain.QUOTE: CanonicalQuote,
    }
)


class BusinessAdapterError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CanonicalBusinessAdapter(Protocol):
    """A provider adapter returns typed facts, never raw provider payloads."""

    async def read_fact(
        self,
        *,
        binding: AuthorityBinding,
        record_ref: str,
    ) -> CanonicalBusinessReadResult | None: ...


async def read_verified_fact(
    adapter: CanonicalBusinessAdapter,
    *,
    binding: AuthorityBinding,
    record_ref: str,
    as_of: datetime,
    expected_account_ref: str | None = None,
) -> CanonicalBusinessFact | None:
    """Return a fact only after authority, freshness, and optional owner checks."""
    result = await adapter.read_fact(binding=binding, record_ref=record_ref)
    if result is None:
        return None
    validate_canonical_fact(
        binding=binding,
        fact=result.fact,
        expected_record_ref=record_ref,
        as_of=as_of,
        expected_account_ref=expected_account_ref,
        ownership_proof=result.ownership_proof,
    )
    return result.fact


def validate_canonical_fact(
    *,
    binding: AuthorityBinding,
    fact: CanonicalBusinessFact,
    expected_record_ref: str,
    as_of: datetime,
    expected_account_ref: str | None = None,
    ownership_proof: OwnershipProof | None = None,
) -> None:
    """Reject mismatched authority, tenant, expired facts, and unowned records."""
    expected_fact_type = CANONICAL_FACT_TYPES.get(binding.domain)
    if expected_fact_type is None or not isinstance(fact, expected_fact_type):
        raise BusinessAdapterError("BUSINESS_FACT_TYPE_MISMATCH")
    if fact.tenant_id != binding.tenant_id or fact.source.tenant_id != binding.tenant_id:
        raise BusinessAdapterError("BUSINESS_TENANT_MISMATCH")
    if (
        fact.source.authority_binding_id != binding.binding_id
        or fact.source.authority_version != binding.binding_version
        or fact.source.connector_id != binding.connector_id
        or fact.source.system_kind != binding.system_kind
    ):
        raise BusinessAdapterError("BUSINESS_AUTHORITY_MISMATCH")
    if fact.source.source_record_ref != expected_record_ref:
        raise BusinessAdapterError("BUSINESS_RECORD_MISMATCH")
    freshness_deadline = binding.freshness_deadline(
        retrieved_at=fact.source.retrieved_at,
        source_valid_until=fact.source.valid_until,
    )
    if freshness_deadline != fact.source.valid_until:
        raise BusinessAdapterError("BUSINESS_SOURCE_TTL_EXCEEDED")
    if not fact.source.is_fresh(as_of=as_of):
        raise BusinessAdapterError("BUSINESS_SOURCE_STALE")
    record_owner = getattr(fact, "customer_account_ref", None)
    if record_owner is not None and expected_account_ref is None:
        raise BusinessAdapterError("BUSINESS_OWNERSHIP_UNVERIFIED")
    if expected_account_ref is not None:
        if ownership_proof is None:
            raise BusinessAdapterError("BUSINESS_OWNERSHIP_UNVERIFIED")
        if (
            ownership_proof.tenant_id != binding.tenant_id
            or ownership_proof.connector_id != binding.connector_id
            or ownership_proof.authority_binding_id != binding.binding_id
            or ownership_proof.authority_version != binding.binding_version
            or ownership_proof.resource_ref != fact.source.source_record_ref
            or ownership_proof.expected_account_ref != expected_account_ref
            or ownership_proof.observed_account_ref != expected_account_ref
            or ownership_proof.verified_at > as_of
        ):
            raise BusinessAdapterError("BUSINESS_OWNERSHIP_MISMATCH")
        if ownership_proof.verified_at + timedelta(seconds=binding.max_age_seconds) <= as_of:
            raise BusinessAdapterError("BUSINESS_OWNERSHIP_PROOF_STALE")
        if record_owner is not None and record_owner != expected_account_ref:
            raise BusinessAdapterError("BUSINESS_OWNERSHIP_MISMATCH")


__all__ = [
    "BusinessAdapterError",
    "CANONICAL_FACT_TYPES",
    "CanonicalBusinessAdapter",
    "CanonicalBusinessFact",
    "CanonicalBusinessReadResult",
    "read_verified_fact",
    "validate_canonical_fact",
]
