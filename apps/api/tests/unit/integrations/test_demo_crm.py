from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from platform_core.integrations.demo_crm import DemoCrmOpportunityExecutor
from platform_core.integrations.sdk import ConnectorContext


def _executor(tenant_id: str = "tenant-demo") -> DemoCrmOpportunityExecutor:
    return DemoCrmOpportunityExecutor(
        ConnectorContext(
            tenant_id=tenant_id,
            connector_id="connector-demo-crm",
            credentials={},
            configuration={"mode": "synthetic"},
        )
    )


def test_demo_opportunity_is_idempotent_and_readback_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "platform_core.integrations.demo_crm.get_settings",
        lambda: SimpleNamespace(environment="test", business_api_adapter="demo"),
    )
    executor = _executor()
    parameters = {"account_ref": "acme", "product_ref": "PCB-DEMO-100"}

    first = asyncio.run(executor.execute("crm.create_opportunity", parameters, "idem-local-1"))
    replay = asyncio.run(executor.execute("crm.create_opportunity", parameters, "idem-local-1"))

    assert first == replay
    assert first is not None
    assert first["source"] == "demo"
    assert first["synthetic"] is True
    assert first["customer_contacted"] is False
    assert "amount" not in first
    assert (
        asyncio.run(executor.verify_postcondition("crm.create_opportunity", parameters, first))
        is True
    )


def test_demo_opportunity_rejects_changed_replay_and_unverified_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "platform_core.integrations.demo_crm.get_settings",
        lambda: SimpleNamespace(environment="local", business_api_adapter="demo"),
    )
    executor = _executor()
    first = asyncio.run(
        executor.execute(
            "crm.create_opportunity",
            {"account_ref": "acme", "product_ref": "PCB-DEMO-100"},
            "idem-local-conflict",
        )
    )
    conflict = asyncio.run(
        executor.execute(
            "crm.create_opportunity",
            {"account_ref": "acme", "product_ref": "PCBA-DEMO-200"},
            "idem-local-conflict",
        )
    )
    foreign = asyncio.run(
        executor.execute(
            "crm.create_opportunity",
            {"account_ref": "other-tenant", "product_ref": "PCB-DEMO-100"},
            "idem-local-foreign",
        )
    )

    assert first is not None and first["ok"] is True
    assert conflict == {"ok": False, "error_code": "IDEMPOTENCY_CONFLICT"}
    assert foreign == {"ok": False, "error_code": "BUSINESS_OWNERSHIP_MISMATCH"}


def test_demo_opportunity_readback_loss_is_unknown_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "platform_core.integrations.demo_crm.get_settings",
        lambda: SimpleNamespace(environment="test", business_api_adapter="demo"),
    )
    executor = _executor()
    parameters = {"account_ref": "acme", "product_ref": "PCB-DEMO-100"}
    output = asyncio.run(executor.execute("crm.create_opportunity", parameters, "idem-readback"))
    from platform_core.integrations.demo_crm import _reset_demo_crm_store_for_tests

    _reset_demo_crm_store_for_tests()
    assert output is not None
    assert (
        asyncio.run(executor.verify_postcondition("crm.create_opportunity", parameters, output))
        is None
    )


@pytest.mark.parametrize(
    ("environment", "adapter", "mode"),
    [
        ("staging", "demo", "synthetic"),
        ("production", "demo", "synthetic"),
        ("local", "http", "synthetic"),
        ("local", "demo", "real"),
    ],
)
def test_demo_crm_writer_refuses_non_demo_modes(
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    adapter: str,
    mode: str,
) -> None:
    monkeypatch.setattr(
        "platform_core.integrations.demo_crm.get_settings",
        lambda: SimpleNamespace(environment=environment, business_api_adapter=adapter),
    )
    with pytest.raises(RuntimeError, match="DEMO_CRM_OPPORTUNITY_DISABLED"):
        DemoCrmOpportunityExecutor(
            ConnectorContext(
                tenant_id="tenant-demo",
                connector_id="connector-demo-crm",
                credentials={},
                configuration={"mode": mode},
            )
        )
