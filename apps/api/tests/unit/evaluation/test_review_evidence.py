"""Candidate-specific human-review evidence must be complete and hash-valid."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from platform_core.evaluation.review_service import verified_review_evidence_for_prompt


def _digest(snapshot: dict[str, object]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _snapshot(prompt_version_id: uuid.UUID, *, status: str = "measured") -> dict[str, Any]:
    return {
        "schema_version": 2,
        "artifact_type": "stratified_agent_run_human_review",
        "target_prompt_version_id": str(prompt_version_id),
        "sampler_version": "risk-stratified-v1",
        "window_seconds": 86_400,
        "versions": {"prompt_version_ids": [str(prompt_version_id)]},
        "summary": {
            "status": status,
            "population_count": 8,
            "selected_count": 8,
            "reviewed_count": 8,
            "weighted_override_rate": 0.125,
            "by_stratum": {
                "routine": {
                    "population": 8,
                    "selected": 8,
                    "reviewed": 8,
                    "override_count": 1,
                }
            },
        },
        "override_reason_counts": {"wrong_route": 1},
        "selected_item_count": 8,
    }


class _Result:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class _Session:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    async def execute(self, _statement: object) -> _Result:
        return _Result(self._rows)


@pytest.mark.asyncio
async def test_verified_review_evidence_returns_complete_candidate_scope() -> None:
    tenant_id = uuid.uuid4()
    prompt_version_id = uuid.uuid4()
    snapshot = _snapshot(prompt_version_id)
    row = SimpleNamespace(
        id=uuid.uuid4(),
        evidence_hash=_digest(snapshot),
        snapshot=snapshot,
        created_at=int(time.time()),
    )

    evidence = await verified_review_evidence_for_prompt(
        _Session([row]),  # type: ignore[arg-type]
        tenant_id=tenant_id,
        prompt_version_id=prompt_version_id,
    )

    assert evidence == {
        "evidence_id": str(row.id),
        "evidence_hash": row.evidence_hash,
        "target_prompt_version_id": str(prompt_version_id),
        "summary": snapshot["summary"],
        "override_reason_counts": snapshot["override_reason_counts"],
        "versions": snapshot["versions"],
        "window_seconds": snapshot["window_seconds"],
        "created_at": row.created_at,
    }


@pytest.mark.asyncio
async def test_review_evidence_rejects_invalid_rows() -> None:
    tenant_id = uuid.uuid4()
    prompt_version_id = uuid.uuid4()
    other_prompt_version_id = uuid.uuid4()

    now = int(time.time())
    wrong_scope = _snapshot(other_prompt_version_id)
    incomplete = _snapshot(prompt_version_id, status="incomplete")
    tampered = _snapshot(prompt_version_id)
    tampered["summary"]["weighted_override_rate"] = 0.0
    stale = _snapshot(prompt_version_id)
    rows = [
        SimpleNamespace(
            id=uuid.uuid4(),
            evidence_hash=_digest(wrong_scope),
            snapshot=wrong_scope,
            created_at=now,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            evidence_hash=_digest(incomplete),
            snapshot=incomplete,
            created_at=now,
        ),
        SimpleNamespace(id=uuid.uuid4(), evidence_hash="0" * 64, snapshot=tampered, created_at=now),
        SimpleNamespace(
            id=uuid.uuid4(),
            evidence_hash=_digest(stale),
            snapshot=stale,
            created_at=now - 31 * 24 * 3600,
        ),
    ]

    evidence = await verified_review_evidence_for_prompt(
        _Session(rows),  # type: ignore[arg-type]
        tenant_id=tenant_id,
        prompt_version_id=prompt_version_id,
    )

    assert evidence is None
