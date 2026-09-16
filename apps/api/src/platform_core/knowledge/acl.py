"""Knowledge ACL model (ticket 15, docs/domain-model.md).

Resource-level ACL: space | document | version. Principals: user | role |
department | enterprise_account. ACL filtering happens BEFORE retrieval
candidate scoring (docs/security.md retrieval rules); the retrieval service
applies it inside SQL, not as a post-filter.
"""

import uuid

from sqlalchemy import Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin

RESOURCE_TYPES = ("space", "document", "version")
PRINCIPAL_TYPES = ("user", "role", "department", "enterprise_account")


class KnowledgeAcl(Base, PkMixin, TenantMixin):
    __tablename__ = "knowledge_acls"
    __table_args__ = (
        UniqueConstraint(
            "resource_type",
            "resource_id",
            "principal_type",
            "principal_id",
            name="uq_acl_entry",
        ),
        Index("ix_knowledge_acls_principal", "principal_type", "principal_id"),
    )

    resource_type: Mapped[str] = mapped_column(String(31), nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    principal_type: Mapped[str] = mapped_column(String(31), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    permission: Mapped[str] = mapped_column(String(31), nullable=False, default="read")
