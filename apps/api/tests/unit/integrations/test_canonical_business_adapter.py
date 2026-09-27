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
    CanonicalProductSpecification,
    CanonicalQuote,
    OwnershipProof,
    SourceMetadata,
)
from platform_core.integrations.canonical_business import (
    BusinessAdapterError,
    CanonicalBusinessAdapter,
    CanonicalBusinessFact,
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
    def __init__(self, fact: CanonicalBusinessFact) -> None:
        self.fact = fact

    async def read_fact(
        self, *, binding: AuthorityBinding, record_ref: str
    ) -> CanonicalBusinessFact | None:
        assert binding.connector_id == CONNECTOR
        assert record_ref == "part-42"
        return self.fact


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
    adapter: CanonicalBusinessAdapter = _FakeMesAdapter(_product())
    fact = asyncio.run(adapter.read_fact(binding=_binding(), record_ref="part-42"))
    assert fact is not None
    validate_canonical_fact(binding=_binding(), fact=fact, as_of=NOW)


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
            as_of=NOW,
        )

    with pytest.raises(BusinessAdapterError, match="BUSINESS_AUTHORITY_MISMATCH"):
        validate_canonical_fact(
            binding=_binding(),
            fact=_product(_source(connector_id=UUID("01900000-0000-7000-8000-000000000098"))),
            as_of=NOW,
        )


def test_stale_and_ttl_exceeding_provider_data_are_refused() -> None:
    stale = _source(
        retrieved_at=NOW - timedelta(hours=2),
        valid_until=NOW - timedelta(hours=1),
    )
    with pytest.raises(BusinessAdapterError, match="BUSINESS_SOURCE_STALE"):
        validate_canonical_fact(binding=_binding(), fact=_product(stale), as_of=NOW)

    too_long = _source(valid_until=NOW + timedelta(days=2))
    with pytest.raises(BusinessAdapterError, match="BUSINESS_SOURCE_TTL_EXCEEDED"):
        validate_canonical_fact(
            binding=_binding(max_age_seconds=3600),
            fact=_product(too_long),
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
        source=_source(),
    )
    binding = _binding(domain=AuthorityDomain.QUOTE)
    with pytest.raises(BusinessAdapterError, match="BUSINESS_OWNERSHIP_UNVERIFIED"):
        validate_canonical_fact(
            binding=binding,
            fact=quote,
            as_of=NOW,
            expected_account_ref="account-42",
        )

    proof = OwnershipProof(
        tenant_id=TENANT,
        connector_id=CONNECTOR,
        expected_account_ref="account-42",
        observed_account_ref="account-42",
        verified_at=NOW - timedelta(seconds=1),
        method="provider_owner_field",
    )
    validate_canonical_fact(
        binding=binding,
        fact=quote,
        as_of=NOW,
        expected_account_ref="account-42",
        ownership_proof=proof,
    )
