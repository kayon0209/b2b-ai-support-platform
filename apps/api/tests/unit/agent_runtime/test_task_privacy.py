from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from platform_core.agent_runtime.tasks_router import _persist_collected, _sanitize_task_slots


def test_unclassified_collected_text_redacts_contact_values() -> None:
    ctx = SimpleNamespace(actor_id=uuid.uuid4())
    slots = asyncio.run(
        _persist_collected(
            ctx,  # type: ignore[arg-type]
            {"description": "Contact support@example.test or +1 415 555 0199."},
        )
    )

    assert slots[0]["value"] == "Contact [EMAIL] or [PHONE]."


def test_schema_sensitive_values_are_withheld_on_write_and_projection() -> None:
    ctx = SimpleNamespace(actor_id=uuid.uuid4())
    collected = asyncio.run(
        _persist_collected(
            ctx,  # type: ignore[arg-type]
            {"tax_id": "123456789"},
            flow_key="invoice_application",
        )
    )
    assert collected[0]["value_withheld"] is True
    assert "value" not in collected[0]

    historical = _sanitize_task_slots(
        "invoice_application",
        [
            {
                "name": "tax_id",
                "origin": "agent_collected",
                "value": "123456789",
                "confirmed": False,
            },
            {
                "name": "description",
                "origin": "agent_collected",
                "value": "Contact support@example.test",
                "confirmed": False,
            },
        ],
    )
    assert historical[0]["value_withheld"] is True
    assert "value" not in historical[0]
    assert historical[1]["value"] == "Contact [EMAIL]"
