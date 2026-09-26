"""The inbox opens on the newest turns and can page backwards."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from platform_core.agent_runtime.chat_service import read_timeline, read_timeline_page
from platform_core.agent_runtime.models import ConversationTurn
from platform_core.cases.canned_models import CannedReply


@pytest.mark.asyncio
async def test_latest_page_and_older_cursor_do_not_drop_new_replies() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(CannedReply.__table__.create)
        await conn.run_sync(ConversationTurn.__table__.create)
    tenant, ref = uuid.uuid4(), uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(5)]
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            session.add_all(
                [
                    ConversationTurn(
                        id=ids[index],
                        tenant_id=tenant,
                        conversation_ref_id=ref,
                        role="customer",
                        text_redacted=f"message-{index + 1}",
                        text_hash=str(index),
                        ts=index + 1,
                        source="platform",
                        created_at=1,
                    )
                    for index in range(5)
                ]
            )
            await session.flush()
            newest, older = await read_timeline_page(session, ref_id=ref, limit=2)
            assert [row["text"] for row in newest] == ["message-4", "message-5"]
            assert older == str(ids[3])
            middle, older = await read_timeline_page(
                session, ref_id=ref, limit=2, before_id=uuid.UUID(older)
            )
            assert [row["text"] for row in middle] == ["message-2", "message-3"]
            assert older == str(ids[1])
            oldest, older = await read_timeline_page(
                session, ref_id=ref, limit=2, before_id=uuid.UUID(older)
            )
            assert [row["text"] for row in oldest] == ["message-1"]
            assert older is None
            assert [row["text"] for row in await read_timeline(session, ref_id=ref, limit=2)] == [
                "message-4",
                "message-5",
            ]
    finally:
        await engine.dispose()
