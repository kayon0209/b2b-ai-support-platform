"""Vendor-neutral contract for self-developed ERP/MES/WMS/CRM connections.

The contract describes canonical facts and guarded write receipts. It does
not select an authority system, define provider endpoints, or treat a request
being sent as proof that an external business change succeeded.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BusinessSystemKind(StrEnum):
    ERP = "erp"
    MES = "mes"
    WMS = "wms"
    CRM = "crm"
    PLM = "plm"


class AuthorityDomain(StrEnum):
    PRODUCT_SPECIFICATION = "product_specification"
    INVENTORY = "inventory"
    QUOTE = "quote"
    CUSTOMER_ACCOUNT = "customer_account"
    ORDER = "order"
    WORK_ORDER = "work_order"
    SHIPMENT = "shipment"
    OPPORTUNITY = "opportunity"


class AuthorityBinding(StrictContract):
    """A tenant-approved, versioned source for exactly one business domain."""

    tenant_id: UUID
    binding_id: UUID
    domain: AuthorityDomain
    system_kind: BusinessSystemKind
    connector_id: UUID
    binding_version: int = Field(ge=1)
    canonical_schema_version: Literal["1.0"] = "1.0"
    max_age_seconds: int = Field(gt=0, le=31_536_000)
    approved_by: UUID
    approved_at: datetime

    @model_validator(mode="after")
    def approval_time_is_utc(self) -> AuthorityBinding:
        _require_utc(self.approved_at, "approved_at")
        return self

    def freshness_deadline(
        self,
        *,
        retrieved_at: datetime,
        source_valid_until: datetime | None = None,
    ) -> datetime:
        """Cap a provider's validity at the tenant-approved freshness TTL."""
        _require_utc(retrieved_at, "retrieved_at")
        deadline = retrieved_at + timedelta(seconds=self.max_age_seconds)
        if source_valid_until is not None:
            _require_utc(source_valid_until, "source_valid_until")
            if source_valid_until <= retrieved_at:
                raise ValueError("source_valid_until must follow retrieved_at")
            deadline = min(deadline, source_valid_until)
        return deadline


class SourceMetadata(StrictContract):
    """Provenance and freshness attached to every canonical external fact."""

    tenant_id: UUID
    authority_binding_id: UUID
    authority_version: int = Field(ge=1)
    connector_id: UUID
    system_kind: BusinessSystemKind
    source_record_ref: str = Field(min_length=1, max_length=255)
    source_version: str = Field(min_length=1, max_length=127)
    retrieved_at: datetime
    valid_until: datetime

    @model_validator(mode="after")
    def times_are_ordered_utc(self) -> SourceMetadata:
        _require_utc(self.retrieved_at, "retrieved_at")
        _require_utc(self.valid_until, "valid_until")
        if self.valid_until <= self.retrieved_at:
            raise ValueError("valid_until must follow retrieved_at")
        return self

    def is_fresh(self, *, as_of: datetime) -> bool:
        """Return false for future, expired, or non-UTC observation times."""
        try:
            _require_utc(as_of, "as_of")
        except ValueError:
            return False
        return self.retrieved_at <= as_of < self.valid_until


class CanonicalProductSpecification(StrictContract):
    """A source-backed product/part specification, including PCB/PCBA facts."""

    tenant_id: UUID
    product_ref: str = Field(min_length=1, max_length=255)
    part_number: str = Field(min_length=1, max_length=127)
    revision: str = Field(min_length=1, max_length=63)
    name: str = Field(min_length=1, max_length=255)
    product_kind: Literal["pcb", "pcba", "component", "material", "other"]
    # Stable canonical keys such as layer_count, board_thickness_um,
    # copper_weight_millioz, material_code, and surface_finish. Values are
    # normalized primitives; a provider's raw response never crosses here.
    specifications: dict[str, str | int | bool] = Field(max_length=64)
    source: SourceMetadata

    @model_validator(mode="after")
    def tenant_matches_source(self) -> CanonicalProductSpecification:
        if self.tenant_id != self.source.tenant_id:
            raise ValueError("product tenant must match provenance tenant")
        return self


class CanonicalInventorySnapshot(StrictContract):
    """A point-in-time, account-scoped stock fact with an explicit unit."""

    tenant_id: UUID
    product_ref: str = Field(min_length=1, max_length=255)
    customer_account_ref: str | None = Field(default=None, max_length=255)
    location_ref: str = Field(min_length=1, max_length=255)
    available_quantity: Decimal = Field(ge=Decimal("0"))
    unit_code: str = Field(min_length=1, max_length=31)
    source: SourceMetadata

    @model_validator(mode="after")
    def tenant_matches_source(self) -> CanonicalInventorySnapshot:
        if self.tenant_id != self.source.tenant_id:
            raise ValueError("inventory tenant must match provenance tenant")
        return self


class CanonicalQuote(StrictContract):
    """An authoritative customer quote; no model-generated price is valid."""

    tenant_id: UUID
    customer_account_ref: str = Field(min_length=1, max_length=255)
    product_ref: str = Field(min_length=1, max_length=255)
    quote_ref: str = Field(min_length=1, max_length=255)
    revision: str = Field(min_length=1, max_length=63)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit_price_minor: int = Field(ge=0)
    minimum_quantity: Decimal = Field(gt=Decimal("0"))
    lead_time_business_days: int | None = Field(default=None, ge=0, le=3650)
    valid_until: datetime
    source: SourceMetadata

    @model_validator(mode="after")
    def quote_is_tenant_bound_and_time_bounded(self) -> CanonicalQuote:
        if self.tenant_id != self.source.tenant_id:
            raise ValueError("quote tenant must match provenance tenant")
        _require_utc(self.valid_until, "valid_until")
        if self.valid_until <= self.source.retrieved_at:
            raise ValueError("quote validity must follow source retrieval")
        return self


class OwnershipProof(StrictContract):
    """Evidence that an external record belongs to the requested account."""

    tenant_id: UUID
    connector_id: UUID
    expected_account_ref: str = Field(min_length=1, max_length=255)
    observed_account_ref: str = Field(min_length=1, max_length=255)
    verified_at: datetime
    method: Literal["provider_owner_field", "signed_event", "approved_mapping"]

    @model_validator(mode="after")
    def owner_matches_and_time_is_utc(self) -> OwnershipProof:
        if self.expected_account_ref != self.observed_account_ref:
            raise ValueError("external record ownership does not match the requested account")
        _require_utc(self.verified_at, "verified_at")
        return self


class BusinessWriteStatus(StrEnum):
    NEEDS_CONFIRMATION = "needs_confirmation"
    UNKNOWN = "unknown"
    VERIFIED_APPLIED = "verified_applied"
    REJECTED = "rejected"


class BusinessWriteReason(StrEnum):
    CONFIRMATION_REQUIRED = "confirmation_required"
    PROVIDER_TIMEOUT = "provider_timeout"
    CONNECTION_LOST = "connection_lost"
    INCOMPLETE_READBACK = "incomplete_readback"
    POSTCONDITION_VERIFIED = "postcondition_verified"
    PROVIDER_REJECTED = "provider_rejected"
    AUTHORIZATION_DENIED = "authorization_denied"
    OWNERSHIP_MISMATCH = "ownership_mismatch"


class ExternalWriteReceipt(StrictContract):
    """A bounded write result; only read-back verified writes are successes."""

    tenant_id: UUID
    connector_id: UUID
    action: str = Field(min_length=1, max_length=127)
    status: BusinessWriteStatus
    idempotency_key_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    external_record_ref: str | None = Field(default=None, max_length=255)
    postcondition_verified: bool = False
    observed_source_version: str | None = Field(default=None, max_length=127)
    reason_code: BusinessWriteReason

    @model_validator(mode="after")
    def success_requires_readback(self) -> ExternalWriteReceipt:
        if self.status is BusinessWriteStatus.VERIFIED_APPLIED:
            if (
                not self.postcondition_verified
                or not self.external_record_ref
                or self.reason_code is not BusinessWriteReason.POSTCONDITION_VERIFIED
            ):
                raise ValueError(
                    "verified_applied requires a verified postcondition and record ref"
                )
        elif self.postcondition_verified:
            raise ValueError("only verified_applied may claim a verified postcondition")
        allowed_reasons = {
            BusinessWriteStatus.NEEDS_CONFIRMATION: {BusinessWriteReason.CONFIRMATION_REQUIRED},
            BusinessWriteStatus.UNKNOWN: {
                BusinessWriteReason.PROVIDER_TIMEOUT,
                BusinessWriteReason.CONNECTION_LOST,
                BusinessWriteReason.INCOMPLETE_READBACK,
            },
            BusinessWriteStatus.VERIFIED_APPLIED: {BusinessWriteReason.POSTCONDITION_VERIFIED},
            BusinessWriteStatus.REJECTED: {
                BusinessWriteReason.PROVIDER_REJECTED,
                BusinessWriteReason.AUTHORIZATION_DENIED,
                BusinessWriteReason.OWNERSHIP_MISMATCH,
            },
        }
        if self.reason_code not in allowed_reasons[self.status]:
            raise ValueError("reason code is inconsistent with external write status")
        return self


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")


__all__ = [
    "AuthorityBinding",
    "AuthorityDomain",
    "BusinessWriteReason",
    "BusinessSystemKind",
    "BusinessWriteStatus",
    "CanonicalInventorySnapshot",
    "CanonicalProductSpecification",
    "CanonicalQuote",
    "ExternalWriteReceipt",
    "OwnershipProof",
    "SourceMetadata",
]
