"""ORM base with tenant-scoped conventions.

Every tenant-owned table inherits TenantMixin (non-null tenant_id, UUIDv7 pk
via uuid6 per docs/domain-model.md and repository coding rules).
"""

import uuid

from sqlalchemy import BigInteger
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column
from uuid6 import uuid7


class Base(DeclarativeBase):
    pass


def default_uuid() -> uuid.UUID:
    return uuid7()


class PkMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=default_uuid)


class TenantMixin:
    """Mixin for tenant-owned rows. RLS policies reference tenant_id."""

    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(nullable=False, index=True)


class TimestampMixin:
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


__all__ = ["Base", "PkMixin", "TenantMixin", "TimestampMixin", "default_uuid", "uuid7"]
