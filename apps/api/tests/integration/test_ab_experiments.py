"""Integration: A/B experiments actually change what runs, and are measurable.

`evaluation/ab.py` has been correct and tested since feature 8.6 was written, and
**nothing outside its own tests called it** - so an experiment nobody could run
was a comparison nobody could read. This covers the three things that make it
real: an arm is assigned deterministically, the arm changes the run, and the
results come from the runs rather than from re-deriving the bucket.

The last one matters most. If results were re-bucketed from the weights, editing
a split would silently move every past run into a different arm - the number you
looked at yesterday would change, and you would be comparing two populations
rather than two arms.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.channels.outbound import ChannelSender, SendResult

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "b2b-ai-support/tests/ab-experiments")
TENANT = str(uuid.uuid5(_NS, "tenant"))
TENANT_OTHER = str(uuid.uuid5(_NS, "tenant-other"))

QUESTION = "请问你们的标准交期一般是多久？"
CHUNK = "标准交期 7 天，加急 3 天，最终以报价单为准。"
DRAFT = "标准交期以报价单为准。"

# Per-test rows. Deliberately **not** the corpus: the knowledge base is seeded
# once for the module, and wiping `chunks` between tests deleted the evidence
# every run needed - so the runs abstained for "no authorized evidence" and the
# tests failed for a reason that had nothing to do with experiments.
_PER_TEST = (
    "conversation_turns",
    "conversation_control_leases",
    "citations",
    "agent_runs",
    "ab_experiments",
)

# Module-scoped: the corpus and the published prompt versions.
_CORPUS = (
    "prompt_versions",
    "chunks",
    "document_versions",
    "documents",
    "knowledge_spaces",
)

_TEARDOWN = _PER_TEST + _CORPUS


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _wipe(tables: tuple[str, ...], where: str, params: dict) -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for table in tables:
            conn.execute(text(f"DELETE FROM {table} WHERE {where}"), params)  # noqa: S608
    admin.dispose()


@pytest.fixture(autouse=True)
def _clean():
    _wipe(_PER_TEST, "tenant_id IN (:a, :b)", {"a": TENANT, "b": TENANT_OTHER})
    yield
    _wipe(_PER_TEST, "tenant_id IN (:a, :b)", {"a": TENANT, "b": TENANT_OTHER})


@pytest.fixture(scope="module", autouse=True)
def _seed():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "ab-a"), (TENANT_OTHER, "ab-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :slug, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
    with admin.begin() as conn:
        space, doc, ver = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        conn.execute(
            text("INSERT INTO knowledge_spaces (id, tenant_id, name) VALUES (:i, :t, 'p')"),
            {"i": space, "t": TENANT},
        )
        conn.execute(
            text(
                "INSERT INTO documents (id, tenant_id, space_id, canonical_uri, title) "
                "VALUES (:i, :t, :s, 'kb://ab', 'Policy')"
            ),
            {"i": doc, "t": TENANT, "s": space},
        )
        conn.execute(
            text(
                "INSERT INTO document_versions (id, tenant_id, document_id, version_label, "
                "content_hash, object_uri, status) VALUES "
                "(:i, :t, :d, 'v1', 'h', 'minio://ab', 'active')"
            ),
            {"i": ver, "t": TENANT, "d": doc},
        )
        conn.execute(
            text(
                "INSERT INTO chunks (id, tenant_id, document_version_id, ordinal, text, "
                "text_hash) VALUES (:i, :t, :v, 0, :x, 'r1')"
            ),
            {"i": uuid.uuid4(), "t": TENANT, "v": ver, "x": CHUNK},
        )
    with admin.begin() as conn:
        from platform_core.retrieval.hybrid import _vector_literal, embed_deterministic

        for cid, chunk_text in conn.execute(
            text("SELECT id, text FROM chunks WHERE tenant_id = :t"), {"t": TENANT}
        ).all():
            conn.execute(
                text("UPDATE chunks SET embedding = CAST(:v AS vector) WHERE id = :i"),
                {"v": _vector_literal(embed_deterministic(chunk_text)), "i": cid},
            )
    admin.dispose()
    yield
    with admin.begin() as conn:
        sub = "(SELECT id FROM tenants WHERE slug LIKE 'ab-%')"
        for table in _TEARDOWN:
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id IN {sub}"))  # noqa: S608
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'ab-%'"))
    admin.dispose()


async def _with_session(tenant: str, fn):
    from platform_core.db import create_engine as async_engine

    engine = async_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant}
            )
            try:
                result = await fn(session)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result
    finally:
        await engine.dispose()


def _define(tenant: str, key: str, variants: list[dict], *, enabled: bool = True) -> None:
    from platform_core.evaluation.ab_service import upsert_experiment

    _run(
        _with_session(
            tenant,
            lambda s: upsert_experiment(
                s,
                tenant_id=uuid.UUID(tenant),
                key=key,
                variants=variants,
                actor_id=uuid.uuid4(),
                enabled=enabled,
            ),
        )
    )


def _publish_prompt(tenant: str, *, name: str, version: int, body: str) -> uuid.UUID:
    """A prompt version the experiment can point at."""
    row_id = uuid.uuid4()
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO prompt_versions (id, tenant_id, template_name, version, body, "
                "published) VALUES (:i, :t, :n, :v, :b, true)"
            ),
            {
                "i": str(row_id),
                "t": tenant,
                "n": name,
                "v": version,
                "b": body,
            },
        )
    admin.dispose()
    return row_id


class _FixedGenerator:
    """A generator double that honours `with_template`, like the real one.

    The orchestrator resolves an arm's prompt by calling `with_template` on the
    injected generator, so a double without it fails the run rather than the
    test - and the double is standing in for a contract, not for one method.
    """

    def __init__(self, template=None) -> None:
        from platform_core.agent_runtime.prompts import KNOWLEDGE_QA_PROMPT

        self.template = template or KNOWLEDGE_QA_PROMPT

    def with_template(self, template):
        return _FixedGenerator(template)

    async def generate(self, question, evidence, **kwargs):
        from platform_core.agent_runtime.qa_path import DraftAnswer

        return DraftAnswer(text=DRAFT, claims={0: [evidence[0].chunk_id]} if evidence else {})


class _NullTransport:
    system = "email"

    async def send(self, *, address, conversation_key, content, command_id):
        return SendResult()


async def _execute(tenant: str, conversation: uuid.UUID) -> tuple[object, dict]:
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator, OrchestratorDeps
    from platform_core.db import create_engine as async_engine
    from platform_core.identity import lease_service
    from platform_core.retrieval.hybrid import PrincipalScope

    tid = uuid.UUID(tenant)
    engine = async_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
        await lease_service.acquire_or_get(session, tenant_id=tid, conversation_ref_id=conversation)
        await session.commit()

    async with factory() as session:
        await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
        orch = AgentOrchestrator(
            session,
            OrchestratorDeps(
                channel_sender=ChannelSender({"email": _NullTransport()}),
                generator=_FixedGenerator(),
            ),
        )
        outcome = await orch.run(
            tenant_id=tid,
            conversation_ref_id=conversation,
            question=QUESTION,
            principal=PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",)),
        )
        await session.commit()
    await engine.dispose()

    # Read through the **admin** connection. The run path commits internally
    # (the control lease) and `set_config(..., true)` is transaction-scoped, so
    # an app-role read afterwards sees nothing through RLS - which is
    # indistinguishable from "the run recorded nothing". This assertion is about
    # what was *written*, not about isolation (other tests cover that), so
    # bypassing RLS here is the honest way to ask it.
    config: dict = {}
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = conn.execute(
            text(
                "SELECT model_config, prompt_version_id FROM agent_runs "
                "WHERE id = :i AND tenant_id = :t"
            ),
            {"i": str(outcome.run_id), "t": tenant},
        ).one_or_none()
    admin.dispose()
    if row is not None:
        config = {"model_config": row[0], "prompt_version_id": row[1]}

    return outcome, config


# --- assignment and effect --------------------------------------------------


def test_a_run_with_no_experiment_records_no_arms() -> None:
    """The common case must change nothing - not even add a key."""
    _, config = _run(_execute(TENANT, uuid.uuid4()))
    assert "experiments" not in config["model_config"]


def test_a_disabled_experiment_does_not_assign() -> None:
    _define(TENANT, "draft.length", [{"name": "short", "weight": 1}], enabled=False)

    _, config = _run(_execute(TENANT, uuid.uuid4()))
    assert "experiments" not in config["model_config"]


def test_an_enabled_experiment_is_recorded_on_the_run() -> None:
    _define(TENANT, "draft.length", [{"name": "short", "weight": 1}])

    _, config = _run(_execute(TENANT, uuid.uuid4()))
    assert config["model_config"]["experiments"] == {"draft.length": "short"}


def test_an_arm_pointing_at_a_prompt_version_changes_the_run() -> None:
    """The point of the feature: the arm has to actually change something.

    Asserted through the recorded lineage, which is what the answer was built
    from - a variant that recorded a version it did not use would make the
    results unreadable.
    """
    version_id = _publish_prompt(
        TENANT, name="knowledge_qa", version=99, body="candidate body {question}"
    )
    _define(
        TENANT,
        "prompt.candidate",
        [{"name": "candidate", "weight": 1, "prompt_version_id": str(version_id)}],
    )

    _, config = _run(_execute(TENANT, uuid.uuid4()))
    assert config["prompt_version_id"] == version_id, "the arm's prompt was not used"


def test_a_single_arm_experiment_assigns_that_arm_to_every_conversation() -> None:
    """Determinism is what makes a comparison valid: a unit that switches arms
    contributes to both and to neither."""
    from platform_core.evaluation.ab_service import assign_experiments

    _define(TENANT, "only", [{"name": "always", "weight": 1}])
    conversation = uuid.uuid4()

    def assign():
        return _run(
            _with_session(
                TENANT,
                lambda s: assign_experiments(
                    s, tenant_id=uuid.UUID(TENANT), conversation_ref_id=conversation
                ),
            )
        )

    assert assign()["only"].name == assign()["only"].name == "always"


def test_a_two_arm_experiment_splits_by_conversation_not_by_run() -> None:
    """Two runs in one conversation must land in the same arm."""
    from platform_core.evaluation.ab_service import assign_experiments

    _define(
        TENANT,
        "split",
        [{"name": "a", "weight": 1}, {"name": "b", "weight": 1}],
    )
    conversation = uuid.uuid4()

    def assign():
        return _run(
            _with_session(
                TENANT,
                lambda s: assign_experiments(
                    s, tenant_id=uuid.UUID(TENANT), conversation_ref_id=conversation
                ),
            )
        )["split"].name

    assert assign() == assign()


# --- results ----------------------------------------------------------------


def test_results_are_read_from_the_runs_that_recorded_them() -> None:
    from platform_core.evaluation.ab_service import experiment_results

    _define(TENANT, "draft.length", [{"name": "short", "weight": 1}])
    _run(_execute(TENANT, uuid.uuid4()))

    results = _run(
        _with_session(
            TENANT,
            lambda s: experiment_results(s, tenant_id=uuid.UUID(TENANT)),
        )
    )
    bucket = next(r for r in results if r.key == "draft.length")
    arm = bucket.arms["short"]
    assert arm["runs"] == 1
    assert arm["automated"] == 1
    assert arm["automation_rate"] == 1.0


def test_an_arm_with_no_runs_has_no_rate() -> None:
    """None, not 0.0: "nobody was bucketed here" and "everything escalated" are
    opposite facts, and only the second is a reason to stop the experiment."""
    from platform_core.evaluation.ab_service import experiment_results

    _define(TENANT, "quiet", [{"name": "a", "weight": 1}, {"name": "b", "weight": 1}])

    results = _run(
        _with_session(
            TENANT,
            lambda s: experiment_results(s, tenant_id=uuid.UUID(TENANT)),
        )
    )
    bucket = next(r for r in results if r.key == "quiet")
    assert set(bucket.arms) == {"a", "b"}
    for arm in bucket.arms.values():
        assert arm["runs"] == 0
        assert arm["automation_rate"] is None


# --- validation -------------------------------------------------------------


def test_a_bad_key_is_refused() -> None:
    from platform_core.evaluation.ab_service import ExperimentError, upsert_experiment

    async def go(session):
        with pytest.raises(ExperimentError, match="invalid experiment key"):
            await upsert_experiment(
                session,
                tenant_id=uuid.UUID(TENANT),
                key="has space",
                variants=[{"name": "a", "weight": 1}],
                actor_id=uuid.uuid4(),
            )

    _run(_with_session(TENANT, go))


def test_duplicate_arm_names_are_refused() -> None:
    """A duplicate would merge two arms' traffic into one, which corrupts the
    comparison rather than merely shrinking it."""
    from platform_core.evaluation.ab_service import ExperimentError, upsert_experiment

    async def go(session):
        with pytest.raises(ExperimentError, match="unique"):
            await upsert_experiment(
                session,
                tenant_id=uuid.UUID(TENANT),
                key="dupe",
                variants=[{"name": "a", "weight": 1}, {"name": "a", "weight": 1}],
                actor_id=uuid.uuid4(),
            )

    _run(_with_session(TENANT, go))


def test_zero_total_weight_is_refused() -> None:
    from platform_core.evaluation.ab_service import ExperimentError, upsert_experiment

    async def go(session):
        with pytest.raises(ExperimentError, match="sum to more than zero"):
            await upsert_experiment(
                session,
                tenant_id=uuid.UUID(TENANT),
                key="zero",
                variants=[{"name": "a", "weight": 0}],
                actor_id=uuid.uuid4(),
            )

    _run(_with_session(TENANT, go))


def test_another_tenant_neither_sees_nor_is_bucketed_by_our_experiment() -> None:
    from platform_core.evaluation.ab_service import assign_experiments, list_experiments

    _define(TENANT, "ours", [{"name": "a", "weight": 1}])

    assert (
        _run(
            _with_session(
                TENANT_OTHER, lambda s: list_experiments(s, tenant_id=uuid.UUID(TENANT_OTHER))
            )
        )
        == []
    )
    assert (
        _run(
            _with_session(
                TENANT_OTHER,
                lambda s: assign_experiments(
                    s, tenant_id=uuid.UUID(TENANT_OTHER), conversation_ref_id=uuid.uuid4()
                ),
            )
        )
        == {}
    )
