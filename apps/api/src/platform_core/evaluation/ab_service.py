"""Experiment definitions, assignment, and the results they produce.

Three pieces, and each exists because of a way the obvious version goes wrong:

- **`assign_experiments`** buckets a *conversation* through `ab.variant_for_conversation`
  and returns the arm for every enabled experiment. Deterministic per
  conversation, so a unit cannot switch arms mid-experiment - a unit in both
  arms is a unit in neither.
- **`resolve_prompt_override`** turns the assigned arm into a `PromptTemplate`,
  or None when the arm names no version (which is the control: "current
  behaviour" must be expressible without publishing a duplicate of the live
  prompt).
- **`experiment_results`** reads the arms back from the runs that recorded them,
  using the same strict automation rule as every other report. Re-deriving the
  bucket from history instead would silently re-bucket every past run the moment
  a weight changed.

Nothing here mutates a shared object. The prompt override is applied by building
a *copy* of the generator (`LlmAnswerGenerator.with_template`) rather than by
swapping the template on the shared instance, which would change the prompt of
every other run in flight.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.models import (
    AgentRun,
    RunStatus,
    run_executed,
)
from platform_core.agent_runtime.models import (
    PromptTemplate as PromptVersionRow,
)
from platform_core.agent_runtime.prompts import PromptTemplate
from platform_core.evaluation.ab import Variant, variant_for_conversation
from platform_core.evaluation.ab_models import AbExperiment
from platform_core.evaluation.categories import automated_run_ids
from platform_core.knowledge.flag_service import validate_flag_key

# Bounded so a dashboard cannot run an unbounded scan.
MAX_RUNS_SCANNED = 20000


class ExperimentError(ValueError):
    """A refused write. Mapped to 400 by the router."""


@dataclass(frozen=True)
class ArmSpec:
    name: str
    weight: float = 1.0
    prompt_version_id: uuid.UUID | None = None


@dataclass
class ExperimentResults:
    key: str
    enabled: bool
    arms: dict[str, dict[str, Any]] = field(default_factory=dict)


def _parse_arms(raw: object) -> list[ArmSpec]:
    """Read the stored JSON into arms, refusing anything malformed.

    Refused rather than skipped: a variant silently dropped from an experiment
    means its traffic is assigned to a *different* arm, which corrupts the
    comparison rather than merely shrinking it.
    """
    if not isinstance(raw, list) or not raw:
        raise ExperimentError("an experiment needs at least one variant")
    arms: list[ArmSpec] = []
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise ExperimentError("every variant needs a name")
        version_id = item.get("prompt_version_id")
        # `weight` absent defaults to 1.0; `weight: 0` is an explicit zero and
        # must stay zero. `item.get("weight") or 1.0` cannot tell them apart -
        # 0 is falsy - so a zero-weight arm would silently get equal traffic and
        # the "must sum above zero" guard below could never fire.
        raw_weight = item.get("weight")
        arms.append(
            ArmSpec(
                name=str(item["name"]).strip(),
                weight=float(raw_weight) if raw_weight is not None else 1.0,
                prompt_version_id=uuid.UUID(str(version_id)) if version_id else None,
            )
        )
    if len({a.name for a in arms}) != len(arms):
        raise ExperimentError("variant names must be unique within an experiment")
    if sum(a.weight for a in arms) <= 0:
        raise ExperimentError("variant weights must sum to more than zero")
    return arms


async def list_experiments(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[AbExperiment]:
    return list(
        (
            await session.execute(
                select(AbExperiment)
                .where(AbExperiment.tenant_id == tenant_id)
                .order_by(AbExperiment.key)
            )
        )
        .scalars()
        .all()
    )


async def upsert_experiment(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    key: str,
    variants: object,
    actor_id: uuid.UUID | None,
    description: str = "",
    enabled: bool = False,
) -> AbExperiment:
    """Create or replace an experiment. The caller owns the transaction."""
    clean_key = (key or "").strip()
    # The same key rule as a feature flag: it ends up in a log line and a
    # bucket key, so anything else either breaks the bucket or corrupts the log.
    if not validate_flag_key(clean_key):
        raise ExperimentError(f"invalid experiment key: {key!r}")
    arms = _parse_arms(variants)

    row = (
        await session.execute(
            select(AbExperiment).where(
                AbExperiment.tenant_id == tenant_id, AbExperiment.key == clean_key
            )
        )
    ).scalar_one_or_none()
    now = int(time.time())
    if row is None:
        row = AbExperiment(tenant_id=tenant_id, key=clean_key, created_at=now, updated_at=now)
        session.add(row)
    row.description = (description or "").strip()
    row.variants = [
        {
            "name": a.name,
            "weight": a.weight,
            "prompt_version_id": str(a.prompt_version_id) if a.prompt_version_id else None,
        }
        for a in arms
    ]
    row.enabled = bool(enabled)
    row.created_by = actor_id
    row.updated_at = now
    await session.flush()
    return row


async def assign_experiments(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_ref_id: uuid.UUID
) -> dict[str, ArmSpec]:
    """The arm this conversation is in, for every enabled experiment.

    Empty when the tenant runs no experiments - which is the common case and
    must cost one query and change nothing.
    """
    rows = (
        (
            await session.execute(
                select(AbExperiment).where(
                    AbExperiment.tenant_id == tenant_id, AbExperiment.enabled.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )
    assigned: dict[str, ArmSpec] = {}
    for row in rows:
        arms = _parse_arms(row.variants)
        chosen = variant_for_conversation(
            experiment=row.key,
            conversation_ref_id=str(conversation_ref_id),
            variants=[Variant(name=a.name, weight=a.weight) for a in arms],
        )
        assigned[row.key] = next(a for a in arms if a.name == chosen)
    return assigned


async def resolve_prompt_override(
    session: AsyncSession, *, tenant_id: uuid.UUID, arm: ArmSpec
) -> PromptTemplate | None:
    """The template this arm runs with, or None for "current behaviour"."""
    if arm.prompt_version_id is None:
        return None
    row = (
        await session.execute(
            select(PromptVersionRow).where(
                PromptVersionRow.tenant_id == tenant_id,
                PromptVersionRow.id == arm.prompt_version_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        # The arm names a version this tenant does not have. Falling back to the
        # default would silently merge the arm into the control and make the
        # comparison meaningless, so the run proceeds on the default *and* the
        # arm is recorded - the results endpoint will show the arm with no
        # distinguishing prompt, which is the visible form of the mistake.
        return None
    return PromptTemplate(name=row.template_name, version=int(row.version), body=row.body)


async def experiment_results(
    session: AsyncSession, *, tenant_id: uuid.UUID, window_seconds: int = 14 * 24 * 3600
) -> list[ExperimentResults]:
    """Per arm: how many runs, and how many actually resolved.

    Read from `model_config["experiments"]`, which the run path records. The
    automation rule is `categories.automated_run_ids` - the same one every other
    report uses, so an arm's number and a category's number mean the same thing.
    """
    experiments = await list_experiments(session, tenant_id=tenant_id)
    if not experiments:
        return []

    cutoff = _now() - window_seconds
    runs = list(
        (
            await session.execute(
                select(AgentRun)
                .where(
                    AgentRun.tenant_id == tenant_id,
                    AgentRun.started_at.is_not(None),
                    AgentRun.started_at >= cutoff,
                    run_executed(),
                )
                .order_by(AgentRun.started_at)
                .limit(MAX_RUNS_SCANNED)
            )
        )
        .scalars()
        .all()
    )
    automated = automated_run_ids(runs)

    results: dict[str, ExperimentResults] = {
        row.key: ExperimentResults(key=row.key, enabled=bool(row.enabled)) for row in experiments
    }
    for row in experiments:
        for arm in _parse_arms(row.variants):
            results[row.key].arms[arm.name] = {
                "runs": 0,
                "automated": 0,
                "escalated": 0,
                "automation_rate": None,
            }

    for run in runs:
        config = run.model_config if isinstance(run.model_config, dict) else {}
        assignments = config.get("experiments")
        if not isinstance(assignments, dict):
            continue
        for key, arm_name in assignments.items():
            bucket = results.get(str(key))
            if bucket is None:
                continue
            arm_bucket = bucket.arms.get(str(arm_name))
            if arm_bucket is None:
                # An arm that no longer exists (the experiment was edited). Its
                # runs stay unattributed rather than being folded into a
                # surviving arm, which would mix two different prompts.
                continue
            arm_bucket["runs"] = int(arm_bucket["runs"]) + 1
            if run.id in automated:
                arm_bucket["automated"] = int(arm_bucket["automated"]) + 1
            elif run.status in (RunStatus.ABSTAINED.value, RunStatus.HANDED_OFF.value):
                arm_bucket["escalated"] = int(arm_bucket["escalated"]) + 1

    for bucket in results.values():
        for arm_totals in bucket.arms.values():
            total = int(arm_totals["runs"])
            arm_totals["automation_rate"] = (
                round(int(arm_totals["automated"]) / total, 4) if total else None
            )

    return [results[row.key] for row in experiments]


def _now() -> int:
    import time as _t

    return int(_t.time())
