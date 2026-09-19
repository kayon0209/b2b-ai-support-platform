"""A message reader that can serve the platform's own conversations.

The orchestrator reads a customer's question through
`OrchestratorDeps.reader`, which has always been the Chatwoot client: the
platform deliberately does not persist raw message bodies, it fetches them
from the system of record on demand (see `support_bridge/minimize.py`).

That left our own chat surface with a hole — a question typed into it had
no Chatwoot message behind it, so the run was queued, found nothing to
read, and completed without answering.

This reader closes the hole without weakening the original rule:

  1. try the local `conversation_turns` copy first (redacted, retention
     already bounded by `RetentionPolicy.conversation_turn_days`);
  2. fall back to Chatwoot for messages that genuinely came from there.

It is a wrapper, not a replacement: Chatwoot remains the system of record
for Chatwoot-originated traffic, and nothing is written here.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select

from platform_core.agent_runtime.models import ConversationTurn
from platform_core.db import app_role_url, session_scope_with_url

logger = logging.getLogger(__name__)


class LocalFirstReader:
    """`ChatwootClient.fetch_message`, preceded by a local lookup.

    The `message_id` for a platform-originated question is the persisted
    turn's id, so the local branch is a primary-key lookup. Anything that is
    not a UUID cannot be a local turn and goes straight to Chatwoot.
    """

    def __init__(self, chatwoot: Any | None = None) -> None:
        self._chatwoot = chatwoot

    async def fetch_message(
        self,
        *,
        account_id: str,
        conversation_id: str,
        message_id: str,
    ) -> str | None:
        local = await self._from_turns(message_id)
        if local is not None:
            return local

        if self._chatwoot is None:
            return None
        fetch = getattr(self._chatwoot, "fetch_message", None)
        if fetch is None:
            return None
        body: object = await fetch(
            account_id=account_id,
            conversation_id=conversation_id,
            message_id=message_id,
        )
        return body if isinstance(body, str) else None

    @staticmethod
    async def _from_turns(message_id: str) -> str | None:
        try:
            turn_id = uuid.UUID(str(message_id))
        except (ValueError, AttributeError, TypeError):
            return None

        try:
            async with session_scope_with_url(app_role_url()) as session:
                row = (
                    await session.execute(
                        select(ConversationTurn.text_redacted).where(ConversationTurn.id == turn_id)
                    )
                ).scalar_one_or_none()
        except Exception:  # noqa: BLE001 - a read failure must fall through
            logger.warning("local_turn_read_failed", exc_info=True)
            return None

        if isinstance(row, str) and row.strip():
            return row
        return None
