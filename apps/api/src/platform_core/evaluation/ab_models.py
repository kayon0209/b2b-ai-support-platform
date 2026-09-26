"""Experiment definitions (feature list 8.6).

One row per experiment. The arms live in `variants` as JSON rather than in their
own table because an arm has no independent life: it is never addressed on its
own, never queried across experiments, and always read as a set - a table would
add a join to every read and a foreign key to every write for nothing.

`variants` is `[{"name", "weight", "prompt_version_id"}, ...]`. The shape is
validated on the way in (`ab_service._parse_arms`) rather than constrained by the
schema, because the validation is about *meaning* - unique names, positive total
weight, a parseable version id - and expressing that in a CHECK constraint would
be a JSON expression nobody could read.

`enabled` defaults to false, matching every other rollout switch here: an
experiment that starts bucketing traffic the moment it is defined is one that
ships before anyone decided to run it.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import BigInteger, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin

MAX_KEY = 127


class AbExperiment(Base, PkMixin, TenantMixin):
    __tablename__ = "ab_experiments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_ab_experiment_key"),
        Index("ix_ab_experiments_tenant", "tenant_id", "enabled"),
    )

    key: Mapped[str] = mapped_column(String(MAX_KEY), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    variants: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
