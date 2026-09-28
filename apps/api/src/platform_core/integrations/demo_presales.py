"""Read-only local pre-sales evidence composition from synthetic authorities.

This returns a sales-review evidence bundle, not a customer recommendation or
quote. It requires separate, tenant-scoped product, inventory, and quote
bindings and runs every fact through the canonical authority/freshness/owner
validator. CRM opportunity writes remain a separate confirmed Tool Gateway
operation and are not simulated here.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from platform_contracts.business_systems import (
    AuthorityBinding,
    AuthorityDomain,
    CanonicalInventorySnapshot,
    CanonicalProductSpecification,
    CanonicalQuote,
)
from platform_core.integrations.canonical_business import (
    BusinessAdapterError,
    CanonicalBusinessAdapter,
    read_verified_fact,
)
from platform_core.integrations.demo_canonical_business import demo_record_ref


class DemoPreSalesEvidence(BaseModel):
    """Minimal source-backed facts for an agent to review before advising."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    product_ref: str
    product_name: str
    revision: str
    product_kind: str
    specifications: dict[str, str | int | bool]
    available_quantity: Decimal
    unit_code: str
    warehouse_ref: str
    indicative_unit_price_minor: int
    currency: str
    minimum_quantity: Decimal
    lead_time_business_days: int | None
    quote_valid_until: datetime
    product_source_version: str
    inventory_source_version: str
    quote_source_version: str
    synthetic: Literal[True] = True
    customer_quote_allowed: Literal[False] = False
    handoff_required: Literal[True] = True


async def build_demo_presales_evidence(
    adapter: CanonicalBusinessAdapter,
    *,
    bindings: dict[AuthorityDomain, AuthorityBinding],
    product_ref: str,
    expected_account_ref: str,
    as_of: datetime,
) -> DemoPreSalesEvidence:
    """Compose synthetic product/stock/quote facts for a human sales review."""
    required_domains = (
        AuthorityDomain.PRODUCT_SPECIFICATION,
        AuthorityDomain.INVENTORY,
        AuthorityDomain.QUOTE,
    )
    if any(domain not in bindings for domain in required_domains):
        raise BusinessAdapterError("DEMO_PRESALES_BINDING_MISSING")
    if not product_ref.strip() or not expected_account_ref.strip():
        raise BusinessAdapterError("DEMO_PRESALES_INPUT_REQUIRED")

    product_fact = await read_verified_fact(
        adapter,
        binding=bindings[AuthorityDomain.PRODUCT_SPECIFICATION],
        record_ref=product_ref.strip(),
        as_of=as_of,
    )
    inventory_fact = await read_verified_fact(
        adapter,
        binding=bindings[AuthorityDomain.INVENTORY],
        record_ref=product_ref.strip(),
        as_of=as_of,
        expected_account_ref=expected_account_ref,
    )
    quote_fact = await read_verified_fact(
        adapter,
        binding=bindings[AuthorityDomain.QUOTE],
        record_ref=demo_record_ref(AuthorityDomain.QUOTE),
        as_of=as_of,
        expected_account_ref=expected_account_ref,
    )
    if (
        not isinstance(product_fact, CanonicalProductSpecification)
        or not isinstance(inventory_fact, CanonicalInventorySnapshot)
        or not isinstance(quote_fact, CanonicalQuote)
        or quote_fact.product_ref != product_ref
        or quote_fact.valid_until <= as_of
    ):
        raise BusinessAdapterError("DEMO_PRESALES_RECORD_UNAVAILABLE")

    return DemoPreSalesEvidence(
        product_ref=product_fact.product_ref,
        product_name=product_fact.name,
        revision=product_fact.revision,
        product_kind=product_fact.product_kind,
        specifications=product_fact.specifications,
        available_quantity=inventory_fact.available_quantity,
        unit_code=inventory_fact.unit_code,
        warehouse_ref=inventory_fact.location_ref,
        indicative_unit_price_minor=quote_fact.unit_price_minor,
        currency=quote_fact.currency,
        minimum_quantity=quote_fact.minimum_quantity,
        lead_time_business_days=quote_fact.lead_time_business_days,
        quote_valid_until=quote_fact.valid_until,
        product_source_version=product_fact.source.source_version,
        inventory_source_version=inventory_fact.source.source_version,
        quote_source_version=quote_fact.source.source_version,
    )


__all__ = ["DemoPreSalesEvidence", "build_demo_presales_evidence"]
