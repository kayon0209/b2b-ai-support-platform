"""Copilot job tests (T06): COP-01 and COP-02.

The cases that matter are the races. A copilot feature is easy to build and
easy to get wrong in exactly one way: the result arrives after the situation it
was generated for has changed, and something inserts it anyway. So most of
these tests are about *staleness* and about what must never happen to an
operator's own text.
"""

from __future__ import annotations

import uuid

import pytest

from platform_core.agent_runtime.copilot import (
    JOB_TTL_SECONDS,
    MAX_INSTRUCTIONS_CHARS,
    REASON_ACTOR_CHANGED,
    REASON_EDITED,
    REASON_EXPIRED,
    REASON_LEASE_CHANGED,
    REASON_TIMELINE_MOVED,
    RESULT_FRESH_SECONDS,
    CopilotError,
    CopilotJobStatus,
    CopilotKind,
    apply_staleness,
    derive_job_id,
    new_job,
    should_expire,
    staleness,
)

TENANT = uuid.uuid4()
CONV = uuid.uuid4()
AGENT = uuid.uuid4()
OTHER_AGENT = uuid.uuid4()

SOURCES = [{"turn_id": "t-3", "kind": "customer", "offset": [0, 12]}]


def _job(**overrides: object):
    kwargs: dict[str, object] = {
        "tenant_id": TENANT,
        "conversation_ref_id": CONV,
        "actor_id": AGENT,
        "kind": CopilotKind.SUMMARY,
        "timeline_revision": 7,
        "lease_version": 3,
        "source_refs": SOURCES,
        "now": 1_700_000_000,
    }
    kwargs.update(overrides)
    return new_job(**kwargs)  # type: ignore[arg-type]


def _current(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "current_timeline_revision": 7,
        "current_lease_version": 3,
        "current_actor_id": AGENT,
        "now": 1_700_000_100,
    }
    base.update(overrides)
    return base


# --- COP-01: sources, and never sending -------------------------------------


def test_a_summary_must_name_its_sources() -> None:
    """An unsourced summary is an assertion about the conversation that nobody
    can check, and COP-01 requires a visible source per fact."""
    with pytest.raises(CopilotError) as exc:
        _job(source_refs=[])
    assert exc.value.code == "COPILOT_SUMMARY_REQUIRES_SOURCES"


def test_a_reply_may_be_generated_without_summary_sources() -> None:
    job = _job(kind=CopilotKind.REPLY, source_refs=[])
    assert job.status is CopilotJobStatus.QUEUED


def test_source_refs_are_carried_on_the_job() -> None:
    job = _job()
    assert job.source_refs == SOURCES
    assert job.as_dict()["source_refs"] == SOURCES


def test_nothing_in_the_module_can_send() -> None:
    """Structural check, and the reason it is worth stating.

    There is no outbound channel, no sender and no dispatch anywhere in the
    copilot module. A generation produces a draft row; an operator sends it
    through the existing workbench flow, which re-checks the lease. If a future
    change adds a send path, this fails and the change has to argue for itself.
    The module docstring names these concepts in prose, so the check runs
    against the parsed code rather than the raw text: what matters is that no
    *identifier* is imported or called, not that the words appear.
    """
    import ast
    import pathlib

    tree = ast.parse(
        pathlib.Path("apps/api/src/platform_core/agent_runtime/copilot.py").read_text()
    )
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                referenced.add(alias.name.split(".")[0])

    forbidden = {"outbound", "send_message", "deliver", "channel_sender", "dispatch", "sender"}
    assert not (referenced & forbidden), f"copilot.py references {referenced & forbidden}"


def test_only_a_succeeded_unedited_job_can_be_inserted() -> None:
    job = _job()
    assert job.can_insert() is False, "a queued job must not be insertable"

    from dataclasses import replace as dc_replace

    done = dc_replace(job, status=CopilotJobStatus.SUCCEEDED, body="客户询问订单状态。")
    assert done.can_insert() is True

    edited = dc_replace(done, edited_by_human=True)
    assert edited.can_insert() is False, "an edited draft must not be overwritten"


# --- COP-02: staleness ------------------------------------------------------


def test_a_new_customer_message_makes_the_job_stale() -> None:
    job = _job()
    assert staleness(job, **_current(current_timeline_revision=8)) == REASON_TIMELINE_MOVED
    stale = apply_staleness(job, **_current(current_timeline_revision=8))
    assert stale.status is CopilotJobStatus.STALE
    assert stale.can_insert() is False


def test_a_handoff_makes_the_job_stale() -> None:
    job = _job()
    stale = apply_staleness(job, **_current(current_lease_version=4))
    assert stale.status is CopilotJobStatus.STALE
    assert stale.error_code == REASON_LEASE_CHANGED


def test_a_different_actor_may_not_insert_someone_elses_result() -> None:
    job = _job()
    stale = apply_staleness(job, **_current(current_actor_id=OTHER_AGENT))
    assert stale.error_code == REASON_ACTOR_CHANGED


def test_an_edited_draft_is_never_overwritten_by_a_regeneration() -> None:
    """The one thing worse than a stale suggestion is losing what a person
    typed."""
    from dataclasses import replace as dc_replace

    job = dc_replace(
        _job(), status=CopilotJobStatus.SUCCEEDED, body="generated", edited_by_human=True
    )
    assert staleness(job, **_current()) == REASON_EDITED
    assert apply_staleness(job, **_current()).status is CopilotJobStatus.STALE


def test_the_first_thing_that_changed_is_the_reason_reported() -> None:
    """An operator told "the conversation moved" when the real cause was a
    handoff would look in the wrong place."""
    job = _job()
    both = _current(current_timeline_revision=9, current_lease_version=9)
    assert staleness(job, **both) == REASON_TIMELINE_MOVED


def test_an_old_result_goes_stale_on_its_own() -> None:
    from dataclasses import replace as dc_replace

    job = dc_replace(_job(), status=CopilotJobStatus.SUCCEEDED, updated_at=1_700_000_000)
    late = 1_700_000_000 + RESULT_FRESH_SECONDS + 60
    assert staleness(job, **_current(now=late)) == REASON_EXPIRED


def test_a_current_job_is_not_stale() -> None:
    job = _job()
    assert staleness(job, **_current()) is None
    assert apply_staleness(job, **_current()).status is CopilotJobStatus.QUEUED


# --- expiry -----------------------------------------------------------------


def test_an_uncollected_job_expires() -> None:
    job = _job()
    assert should_expire(job, now=job.created_at + 10) is False
    assert should_expire(job, now=job.created_at + JOB_TTL_SECONDS + 1) is True


def test_a_failed_job_is_not_reported_as_stale() -> None:
    """It produced nothing; calling it stale would imply a result exists."""
    from dataclasses import replace as dc_replace

    job = dc_replace(_job(), status=CopilotJobStatus.FAILED, error_code="MODEL_TIMEOUT")
    result = apply_staleness(job, **_current(current_timeline_revision=99))
    assert result.status is CopilotJobStatus.FAILED
    assert result.error_code == "MODEL_TIMEOUT"


def test_a_succeeded_job_does_not_expire_as_uncollected() -> None:
    from dataclasses import replace as dc_replace

    job = dc_replace(_job(), status=CopilotJobStatus.SUCCEEDED)
    assert should_expire(job, now=job.created_at + JOB_TTL_SECONDS * 10) is False


# --- idempotency ------------------------------------------------------------


def test_the_job_id_is_derived_from_the_state_not_generated() -> None:
    """A double-clicked "generate" must not queue two model calls for one
    revision."""
    a = _job()
    b = _job()
    assert a.job_id == b.job_id


def test_a_different_revision_is_a_different_job() -> None:
    assert _job().job_id != _job(timeline_revision=8).job_id
    assert _job().job_id != _job(lease_version=4).job_id


def test_a_different_actor_is_a_different_job() -> None:
    assert _job().job_id != _job(actor_id=OTHER_AGENT).job_id


def test_the_job_id_carries_the_tenant() -> None:
    assert _job().job_id != _job(tenant_id=uuid.uuid4()).job_id


def test_derive_job_id_is_stable_across_calls() -> None:
    args = {
        "tenant_id": TENANT,
        "conversation_ref_id": CONV,
        "actor_id": AGENT,
        "kind": CopilotKind.REPLY,
        "timeline_revision": 1,
        "lease_version": 2,
    }
    assert derive_job_id(**args) == derive_job_id(**args)  # type: ignore[arg-type]


# --- input limits -----------------------------------------------------------


def test_over_long_instructions_are_refused_not_truncated() -> None:
    """A silently shortened instruction produces a summary of something the
    agent did not ask for, with no way for them to tell."""
    with pytest.raises(CopilotError) as exc:
        _job(instructions="x" * (MAX_INSTRUCTIONS_CHARS + 1))
    assert exc.value.code == "COPILOT_INSTRUCTIONS_TOO_LONG"


def test_instructions_at_the_limit_are_accepted() -> None:
    job = _job(instructions="x" * MAX_INSTRUCTIONS_CHARS)
    assert len(job.instructions) == MAX_INSTRUCTIONS_CHARS


def test_instructions_are_stripped() -> None:
    assert _job(instructions="  be brief  ").instructions == "be brief"


def test_a_reply_job_may_carry_instructions() -> None:
    job = _job(kind=CopilotKind.REPLY, instructions="用中文简短回复", source_refs=[])
    assert job.instructions == "用中文简短回复"
