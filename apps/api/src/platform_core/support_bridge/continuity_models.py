"""Persistence for conversation-to-contact links (feature list 1.5).

One row per conversation, saying which channel contact it belongs to. See
migration 0045 for why the key is the contact and not the enterprise account:
colleagues share an account, and continuing a conversation across that boundary
would show one employee's messages to another.

This table holds no message content. It is a pointer, so the privacy rules for
conversation text (redaction, retention, lease-gated reads) stay in one place
and are not bypassed by the continuity feature.
"""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class ConversationContact(Base, PkMixin, TenantMixin):
    __tablename__ = "conversation_contacts"

    conversation_ref_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    external_contact_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # Which door they came in through. Nullable because a sync often cannot say.
    channel: Mapped[str | None] = mapped_column(String(31), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
