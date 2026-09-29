"""Strict, payload-minimized durable job contracts for knowledge releases."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from platform_contracts.knowledge_release import StrictReleaseModel


class ReleaseEvaluationRequested(StrictReleaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    draft_id: UUID
    knowledge_space_id: UUID
    baseline_version_id: UUID
    candidate_version_id: UUID
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_approval_ref: str = Field(min_length=1, max_length=127)
    idempotency_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReleasePostTestRequested(StrictReleaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    evaluation_id: UUID
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_approval_ref: str = Field(min_length=1, max_length=127)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


__all__ = ["ReleaseEvaluationRequested", "ReleasePostTestRequested"]
