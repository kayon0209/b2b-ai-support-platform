"""Versioned, tenant-bound manifest for approved knowledge release cases."""

from __future__ import annotations

import re
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from platform_contracts.knowledge_release import StrictReleaseModel

_GENERATED_CASE_ID = re.compile(r"^eval-[0-9a-f]{10}$")


class ReleaseDatasetCase(StrictReleaseModel):
    case_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=8000)
    role: str = Field(min_length=1, max_length=63)
    principal_groups: tuple[str, ...] = ()
    expected_route: str = Field(default="", max_length=63)
    required_claims: tuple[str, ...] = ()
    forbidden_claims: tuple[str, ...] = ()
    must_abstain: bool = False
    restricted_query: bool = False
    expected_handoff: bool = False
    cross_lingual: bool = False
    allowed_tools: tuple[str, ...] = ()
    expected_version_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def stable_case_id(self) -> ReleaseDatasetCase:
        if _GENERATED_CASE_ID.fullmatch(self.case_id):
            raise ValueError("case_id must be stable across dataset loads")
        return self


class ReleaseDatasetPrincipalScope(StrictReleaseModel):
    principal_types: tuple[Literal["user", "role", "department", "enterprise_account"], ...] = (
        Field(min_length=1)
    )
    principal_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def paired_principals(self) -> ReleaseDatasetPrincipalScope:
        if len(self.principal_types) != len(self.principal_ids):
            raise ValueError("principal type/id pairs must have equal length")
        if any(not principal_id or len(principal_id) > 255 for principal_id in self.principal_ids):
            raise ValueError("principal ids must be non-empty and bounded")
        return self


class KnowledgeReleaseDatasetManifest(StrictReleaseModel):
    schema_version: Literal[1] = 1
    tenant_id: UUID
    knowledge_space_id: UUID
    cases: tuple[ReleaseDatasetCase, ...] = Field(min_length=1, max_length=5000)
    principal_scopes: dict[str, ReleaseDatasetPrincipalScope]

    @model_validator(mode="after")
    def complete_authorized_sample_set(self) -> KnowledgeReleaseDatasetManifest:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case ids must be unique")
        if set(self.principal_scopes) != set(case_ids):
            raise ValueError("every case needs one server-resolved principal scope")
        if not any(not case.must_abstain for case in self.cases):
            raise ValueError("dataset needs at least one answerable case")
        if not any(case.expected_version_keys for case in self.cases):
            raise ValueError("dataset needs expected source references")
        return self


__all__ = [
    "KnowledgeReleaseDatasetManifest",
    "ReleaseDatasetCase",
    "ReleaseDatasetPrincipalScope",
]
