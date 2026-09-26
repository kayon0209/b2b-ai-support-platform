"""Worker queue routing for the R1 semantic and copilot consumers."""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from platform_core.agent_runtime.copilot import COPILOT_EVENT_TYPE
from platform_core.agent_runtime.semantic.shadow import SHADOW_EVENT_TYPE
from platform_core.agent_runtime.tasks.planning_seam import TASK_PLANNING_EVENT_TYPE


class _Rows:
    def all(self) -> list[object]:
        return []


class _Session:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _Rows:
        self.statements.append(statement)
        return _Rows()


def test_default_relay_leaves_dedicated_events_to_the_semantic_worker() -> None:
    from worker.outbox_relay import build_default_relay

    relay = build_default_relay()

    assert set(relay.excluded_event_types) == {
        SHADOW_EVENT_TYPE,
        COPILOT_EVENT_TYPE,
        TASK_PLANNING_EVENT_TYPE,
    }


@pytest.mark.parametrize(
    "claim",
    [
        pytest.param("shadow", id="shadow"),
        pytest.param("copilot", id="copilot"),
        pytest.param("task-planning", id="task-planning"),
    ],
)
@pytest.mark.asyncio
async def test_owner_claim_selects_queue_metadata_without_payload(claim: str) -> None:
    session = _Session()
    if claim == "shadow":
        from worker.shadow_consumer import claim_shadow_events

        claimed = await claim_shadow_events(session)  # type: ignore[arg-type]
    elif claim == "copilot":
        from worker.copilot_consumer import claim_copilot_jobs

        claimed = await claim_copilot_jobs(session)  # type: ignore[arg-type]
    else:
        from worker.task_planning_consumer import claim_task_planning_events

        claimed = await claim_task_planning_events(session)  # type: ignore[arg-type]

    assert claimed == []
    statement = session.statements[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    selected_columns = compiled.split(" FROM ", maxsplit=1)[0]
    assert "payload" not in selected_columns.lower()
    assert "event_id" in selected_columns
    assert "tenant_id" in selected_columns


@pytest.mark.parametrize("consumer", ["shadow", "copilot", "task-planning"])
@pytest.mark.asyncio
async def test_stale_reclaim_uses_claim_time_not_event_creation_time(consumer: str) -> None:
    session = _Session()
    if consumer == "shadow":
        from worker.shadow_consumer import reclaim_stale_shadow

        await reclaim_stale_shadow(session)  # type: ignore[arg-type]
    elif consumer == "copilot":
        from worker.copilot_consumer import reclaim_stale_copilot

        await reclaim_stale_copilot(session)  # type: ignore[arg-type]
    else:
        from worker.task_planning_consumer import reclaim_stale_task_planning

        await reclaim_stale_task_planning(session)  # type: ignore[arg-type]

    statement = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert "processing_started_at" in statement
    assert "created_at" not in statement
