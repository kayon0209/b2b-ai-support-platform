"""Run the evaluation dataset against the real pipeline and write its report.

`release_check` reads `tests/artifacts/eval_report.json` and nothing produced
it. That made the documented release process unexecutable: three quality gates
(citation coverage, abstention correctness, forbidden claims) had a reader, a
threshold, and no input.

**Why this is not the test harness.** `tests/evals/harness.py` serves a fixed
corpus through a lexical ranker so a regression reproduces deterministically.
Feeding *that* to the gate would report an oracle's numbers as measured
quality - the same theatre as quoting an RPO from a logical dump. This script
therefore does the opposite of the harness: it seeds the same corpus into a
real tenant, ingests it through the real worker with real embeddings, retrieves
through `hybrid_search` with the real ACL and status filters, and generates
with the live model. Every number in the report comes from the system under
test.

The dataset's `availability` field is reproduced with the machinery that
actually enforces it, not simulated:

    active        -> an open space (no ACL rows), status 'active'
    expired       -> status 'expired', which `hybrid_search` filters out
    unauthorized  -> a space carrying an ACL grant for a principal the cases
                     do not act as, so the ACL filter closes it

Requires the compose services (Postgres, MinIO) and a live `APP_LLM_API_KEY`.

Usage:
    python scripts/run_eval.py                 # writes tests/artifacts/eval_report.json
    python scripts/run_eval.py --keep-tenant   # leave the tenant for inspection
    python scripts/run_eval.py --limit 3       # smoke-run a few cases first
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

# The e2e MinIO container, unless the environment already names a bucket.
os.environ.setdefault("APP_OBJECT_STORAGE_ENDPOINT", "localhost:19000")
os.environ.setdefault("APP_OBJECT_STORAGE_ACCESS_KEY", "minioadmin")
os.environ.setdefault("APP_OBJECT_STORAGE_SECRET_KEY", "minioadmin")
os.environ.setdefault("APP_OBJECT_STORAGE_BUCKET", "documents")
os.environ.setdefault("APP_OBJECT_STORAGE_SECURE", "false")

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (
    "apps/api/src",
    "apps/worker/src",
    "packages/contracts/src",
    "packages/policy/src",
    "packages/observability/src",
    # `tests/` rather than the repo root: there is no `tests/__init__.py`, so
    # `tests.evals.dataset` is not importable, but `tests/evals/__init__.py`
    # exists and makes `evals` a package. Adding `tests/` gives
    # `evals.dataset` without introducing an `__init__.py` that would change
    # how pytest collects the suite.
    "tests",
):
    sys.path.insert(0, str(REPO_ROOT / _p))

from evals.dataset import CORPUS, all_cases  # noqa: E402
from evals.dataset import CORPUS as _EVAL_CORPUS  # noqa: E402

# The live pipeline's chunks carry the DOCUMENT TITLE; attribution needs the
# corpus VERSION KEY, so map title -> key for the fixed corpus.
_TITLE_TO_KEY = {entry.document_title: entry.version_key for entry in _EVAL_CORPUS}


def live_key_of(chunk: object) -> str:
    from evals.harness import corpus_key_of as _parse

    parsed = _parse(chunk)
    if parsed in {e.version_key for e in _EVAL_CORPUS}:
        return parsed
    return _TITLE_TO_KEY.get(getattr(chunk, "title", ""), "")


from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from platform_core.agent_runtime.generator import LlmAnswerGenerator  # noqa: E402
from platform_core.config import get_settings  # noqa: E402
from platform_core.evaluation.runner import CaseResult, EvalReport, EvaluationRunner  # noqa: E402
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant  # noqa: E402
from platform_core.knowledge import service as knowledge  # noqa: E402
from platform_core.llm.gitee_ai import GiteeAiClient  # noqa: E402
from platform_core.retrieval.hybrid import (  # noqa: E402
    PrincipalScope,
    ProviderEmbedder,
    hybrid_search,
)

PLATFORM_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = os.environ.get(
    "APP_TEST_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)

REPORT_PATH = REPO_ROOT / "tests" / "artifacts" / "eval_report.json"

# The principal the "unauthorized" corpus is withheld from. Every dataset case
# runs as `support_agent`, so a grant to this role closes the space to all of
# them - which is the point: the entry must be unreachable, not merely unused.
CLOSED_PRINCIPAL_ROLE = "tenant_owner"

_SYSTEM = "system"


def _ctx(tenant_id: uuid.UUID) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, actor_id=None, actor_kind=_SYSTEM)


async def _seed(platform, app, *, slug: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create the tenant, an open space and a closed space. Returns their ids.

    The tenant is created as the superuser, then everything else as the *app*
    role with the tenant bound: the knowledge tables are FORCE RLS, so an
    insert from an unbound connection is refused by the policy's WITH CHECK.
    """
    async with platform() as s:
        tenant_id = (
            await s.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) "
                    "VALUES (gen_random_uuid(), :slug, :slug, 'active') RETURNING id"
                ),
                {"slug": slug},
            )
        ).scalar()
        await s.commit()

    async with app() as s:
        await apply_rls_tenant(s, _ctx(tenant_id))
        open_space = (
            await s.execute(
                text(
                    "INSERT INTO knowledge_spaces (id, tenant_id, name) "
                    "VALUES (gen_random_uuid(), :t, 'eval-open') RETURNING id"
                ),
                {"t": tenant_id},
            )
        ).scalar()
        closed_space = (
            await s.execute(
                text(
                    "INSERT INTO knowledge_spaces (id, tenant_id, name) "
                    "VALUES (gen_random_uuid(), :t, 'eval-restricted') RETURNING id"
                ),
                {"t": tenant_id},
            )
        ).scalar()
        # One ACL row is what makes a space closed. A space with no rows is
        # readable by the whole tenant by design, so `unauthorized` cannot be
        # expressed by "put it somewhere else" - it needs a grant that the
        # caller does not hold.
        await s.execute(
            text(
                "INSERT INTO knowledge_acls "
                "(id, tenant_id, resource_type, resource_id, principal_type, principal_id) "
                "VALUES (gen_random_uuid(), :t, 'space', :space, 'role', :role)"
            ),
            {"t": tenant_id, "space": closed_space, "role": CLOSED_PRINCIPAL_ROLE},
        )
        await s.commit()

    return tenant_id, open_space, closed_space


async def _not_ready(app: Any, tenant_id: uuid.UUID, by_id: dict[uuid.UUID, str]) -> set[str]:
    """Corpus keys whose version did not reach `ingestion_status = 'ready'`.

    Reads the caller's own version ids under an explicit tenant binding. The
    binding is not optional: `document_versions` is FORCE-RLS'd, so an unbound
    SELECT returns zero rows and reports success - which would make this check
    silently always pass, the exact failure mode it exists to catch.
    """
    async with app() as s:
        await apply_rls_tenant(s, _ctx(tenant_id))
        rows = (
            (
                await s.execute(
                    text(
                        "SELECT id, ingestion_status FROM document_versions "
                        "WHERE id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": list(by_id)},
                )
            )
            .mappings()
            .all()
        )
    seen = {r["id"] for r in rows if r["ingestion_status"] == "ready"}
    return {key for vid, key in by_id.items() if vid not in seen}


async def _ingest_corpus(
    app, *, tenant_id: uuid.UUID, open_space: uuid.UUID, closed_space: uuid.UUID, embedder
) -> dict[str, uuid.UUID]:
    """Upload and ingest every corpus entry. Returns version_key -> version id."""
    versions: dict[str, uuid.UUID] = {}
    for entry in CORPUS:
        space = closed_space if entry.availability == "unauthorized" else open_space
        data = entry.text.encode("utf-8")
        async with app() as s:
            await apply_rls_tenant(s, _ctx(tenant_id))
            created = await knowledge.create_document(
                s,
                tenant_id=tenant_id,
                space_id=space,
                title=entry.document_title,
                canonical_uri=f"doc://eval-{entry.version_key}",
                data=data,
                content_type="text/markdown",
                filename=f"{entry.version_key}.md",
                classification="internal",
                version_label="v1",
            )
            # Commit before uploading: the worker claims from another
            # connection and an uncommitted row is invisible to it.
            await s.commit()
            knowledge.upload_object(created.object_key, data, "text/markdown")
            versions[entry.version_key] = created.version_id
        print(f"  uploaded {entry.version_key} ({entry.availability})", flush=True)

    # Ingest exactly the versions this run uploaded, claiming repeatedly until
    # each one has left the queue.
    #
    # Why not "one drain per document, and assert on that document's row".
    # `claim_ingestion_versions` is a global FIFO: it has no tenant filter (one
    # bulk worker serves every tenant, which is correct) and it takes the
    # *oldest* N claimable rows anywhere. With `batch` below the queue depth,
    # the rows past the batch never get a turn, and the loop re-claims from the
    # same head every round. Measured on a live queue: 13 claimable rows,
    # `claim(10)` returned 9, and the oldest row was absent - the signature of
    # `FOR UPDATE SKIP LOCKED` skipping a row another transaction holds.
    #
    # The failure it produced was
    #
    #     RuntimeError: ingesting refund-policy-v3 left ingestion_status='uploaded'
    #                    (stats=IngestStats(claimed=8, ready=8, ...))
    #
    # for a document that was uploaded, stored, and perfectly ingestable. Note
    # the shape: `ready=8` is a *batch* summary and says nothing about the row
    # asked about. The fix is to ask for the right rows instead of hoping the
    # FIFO reaches them - see `drain_versions` and migration 0039.
    #
    # A failure still names the document it came from: `drain_versions` reports
    # the ids that never settled, so this mapping keeps the readable key.
    from worker.ingestion_consumer import drain_versions

    by_id = {version_id: key for key, version_id in versions.items()}
    async with app() as s:
        try:
            stats = await drain_versions(s, list(versions.values()), embedder=embedder)
            await s.commit()
        except Exception as exc:
            # Re-read the rows to name the specific documents that did not
            # settle. `drain_versions` deliberately does not carry the ids in
            # its exception message beyond a repr, and a reader of this error
            # wants the corpus *keys* - `refund-policy-v3` is actionable,
            # `UUID('9a47...')` is not.
            await s.rollback()
            stuck = await _not_ready(app, tenant_id, by_id)
            raise RuntimeError(
                f"ingesting {sorted(stuck)} left ingestion_status != 'ready'; every "
                f"corpus entry must be indexed or the run measures nothing ({exc})"
            ) from exc
    print(
        f"  ingested {len(versions)} entries "
        f"(claimed={stats.claimed}, ready={stats.ready}, deferred={stats.deferred})",
        flush=True,
    )

    # Post-condition, checked on the caller's own rows rather than on the batch
    # statistics: `ready == 13` in a summary cannot tell you that *your*
    # thirteen are indexed, and the whole point of this script is that the
    # corpus it is about to query is the corpus it uploaded.
    not_ready = await _not_ready(app, tenant_id, by_id)
    if not_ready:
        raise RuntimeError(
            f"expected {len(versions)} ready versions, these did not reach ready: "
            f"{sorted(not_ready)}"
        )

    # `expired` is applied after ingestion because the worker marks a
    # successfully ingested version `active`. Setting it before would be
    # overwritten - and would also have the worker skip it.
    #
    # The `rowcount` assertion is not decoration. `document_versions` is FORCE
    # RLS and `set_config('app.tenant_id', ..., true)` is transaction-scoped:
    # an UPDATE issued after a COMMIT is unbound, matches nothing, and reports
    # **rowcount 0 with no error**. Writing this loop as "update, commit, then
    # verify" is exactly how that bug is born - it was, once, while writing
    # this script.
    expired = [e.version_key for e in CORPUS if e.availability == "expired"]
    async with app() as s:
        await apply_rls_tenant(s, _ctx(tenant_id))
        for key in expired:
            rowcount = (
                await s.execute(
                    text("UPDATE document_versions SET status = 'expired' WHERE id = :v"),
                    {"v": versions[key]},
                )
            ).rowcount
            if rowcount != 1:
                raise RuntimeError(f"could not mark {key} expired (rowcount={rowcount})")
        await s.commit()

    return versions


async def _cleanup(platform, *, tenant_id: uuid.UUID, slug: str) -> None:
    """Remove every row this run created. Order matters: chunks reference
    versions, versions reference documents."""
    async with platform() as s:
        for stmt in (
            "DELETE FROM chunks WHERE document_version_id IN "
            "(SELECT id FROM document_versions WHERE tenant_id = :t)",
            "DELETE FROM knowledge_acls WHERE tenant_id = :t",
            "DELETE FROM document_versions WHERE tenant_id = :t",
            "DELETE FROM documents WHERE tenant_id = :t",
            "DELETE FROM knowledge_spaces WHERE tenant_id = :t",
            "DELETE FROM tenants WHERE id = :t AND slug = :slug",
        ):
            params = {"t": tenant_id}
            if ":slug" in stmt:
                params["slug"] = slug
            await s.execute(text(stmt), params)
        await s.commit()


def _claim_text(result: CaseResult, claim_index: int) -> str:
    """The flagged claim's own sentence, so a candidate can be judged by eye."""
    return result.claim_texts.get(claim_index, "<no per-claim text recorded>")


def _flake_summary(runs: list[EvalReport], cases: list) -> dict[str, int]:
    """Cases whose result differed between runs: passed N, failed M.

    Only cases with `0 < failures < len(runs)` are flaky. A case that failed
    every run is a real failure, and one that passed every run is fine - the
    interesting population is the one a single run cannot classify.
    """
    failures: dict[str, int] = {}
    for report in runs:
        for result in report.results:
            if not result.passed:
                failures[result.case_id] = failures.get(result.case_id, 0) + 1
    total = len(runs)
    return {cid: n for cid, n in sorted(failures.items()) if 0 < n < total}


def _write_report(
    report: EvalReport,
    *,
    tenant_id: uuid.UUID,
    elapsed: float,
    samples: int = 1,
    flaky: dict[str, int] | None = None,
) -> None:
    """Serialize in the shape `release_check._report_from_artifact` reads.

    The per-case results are kept as well: the aggregate is what the gate
    decides on, but a reviewer asked to explain a failed release needs to see
    which case moved.
    """
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": report.run_id,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "total": report.total,
        "passed": report.passed,
        "failed": report.failed,
        "abstention_correct": report.abstention_correct,
        "abstention_false": report.abstention_false,
        # ADR 0009: abstentions excluded from the rate above because the
        # question and the corpus were in different languages, plus how many
        # cases declared themselves cross-lingual. Both are persisted because
        # `release_check` compares them - if either is dropped here it reads
        # back as 0 and the comparison would pass without meaning anything.
        "cross_lingual_unreachable": report.cross_lingual_unreachable,
        "declared_cross_lingual": report.declared_cross_lingual,
        "exemptible_cross_lingual": report.exemptible_cross_lingual,
        "citation_violations": report.citation_violations,
        "forbidden_claim_hits": report.forbidden_claim_hits,
        # ADR 0005: reported, never gated. A claim that negates a term its
        # cited excerpt affirms is worth seeing even when the citation
        # resolves, which is all `citation_violations` can tell you.
        "contradiction_candidates": report.contradiction_candidates,
        # Provenance: a quality number without its source is not evidence.
        "provenance": {
            "generated_by": "scripts/run_eval.py",
            "tenant_id": str(tenant_id),
            "elapsed_seconds": round(elapsed, 1),
            "model": get_settings().llm_model,
            "embedding_model": get_settings().llm_embedding_model,
            "retrieval": "hybrid_search (real ACL + status filters)",
            "note": "real tenant corpus, real embeddings, live model",
            "samples": samples,
            # ADR 0005: the model is stochastic, so a single run cannot tell a
            # fixed case from a lucky one. Reported, never gated.
            "flaky_cases": flaky or {},
        },
        # Attribution aggregates (plan 4.1): the headline is no longer a
        # single boolean - a reader can see WHERE the pipeline broke.
        "attribution_counts": report.attribution_counts,
        "retrieval_recall_mean": round(report.retrieval_recall_mean, 4),
        "recall_measured": report.recall_measured,
        "results": [asdict(r) for r in report.results],
    }
    REPORT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep-tenant", action="store_true", help="do not clean up")
    parser.add_argument("--limit", type=int, default=0, help="run only the first N cases")
    parser.add_argument(
        "--samples",
        type=int,
        default=1,
        help="run the case set this many times and report flakiness. The model is "
        "stochastic, so one run cannot tell a fixed case from a lucky one; ADR 0005 "
        "records a P0 gate that flips with sampling, and this measures it.",
    )
    args = parser.parse_args(argv)

    if not get_settings().llm_api_key:
        print(
            "FATAL: APP_LLM_API_KEY is not set; the model boundary fails closed.",
            file=sys.stderr,
        )
        return 2

    platform_engine = create_async_engine(PLATFORM_URL)
    app_engine = create_async_engine(APP_URL)
    platform = async_sessionmaker(platform_engine, expire_on_commit=False)
    app = async_sessionmaker(app_engine, expire_on_commit=False)

    slug = f"eval-run-{uuid.uuid4().hex[:8]}"
    tenant_id: uuid.UUID | None = None
    started = time.monotonic()
    try:
        print(f"tenant slug: {slug}")
        tenant_id, open_space, closed_space = await _seed(platform, app, slug=slug)
        print(f"tenant={tenant_id}")

        client = GiteeAiClient()
        print(f"ingesting {len(CORPUS)} corpus entries through the real worker…")
        await _ingest_corpus(
            app,
            tenant_id=tenant_id,
            open_space=open_space,
            closed_space=closed_space,
            embedder=client,
        )

        # --- the real pipeline, plugged into the runner's two seams --------
        query_embedder = ProviderEmbedder(provider=client, _dimensions=client.dimensions)
        generator = LlmAnswerGenerator(client)

        async def retrieve(question: str, scope: PrincipalScope):
            async with app() as s:
                await apply_rls_tenant(s, _ctx(tenant_id))
                return await hybrid_search(
                    s,
                    tenant_id=tenant_id,
                    query=question,
                    top_k=8,
                    principal=scope,
                    embedder=query_embedder,
                )

        async def answer(question: str, evidence):
            return await generator.generate(question, evidence)

        cases = all_cases()
        if args.limit:
            cases = cases[: args.limit]
        samples = max(1, args.samples)
        repeat = f" x{samples} samples" if samples > 1 else ""
        print(f"running {len(cases)} cases against {get_settings().llm_model}{repeat}…")

        report: EvalReport | None = None
        runs: list[EvalReport] = []
        for sample_index in range(samples):
            if samples > 1:
                print(f"  sample {sample_index + 1}/{samples}…")
            report = await EvaluationRunner(answer, retrieve, key_of=live_key_of).run(cases)
            runs.append(report)
            if samples > 1:
                # Printed per sample, not just for the last one: the question
                # ADR 0005 asks is whether the metric fires on the run where
                # the model actually contradicted itself, and that run may not
                # be the last.
                contradicted = []
                for r in report.results:
                    if not r.contradicted_claims:
                        continue
                    texts = "; ".join(_claim_text(r, i) for i in r.contradicted_claims)
                    contradicted.append(f"{r.case_id}: {texts}")
                forbidden = [r.case_id for r in report.results if r.forbidden_hit]
                print(f"      passed {report.passed}/{report.total}")
                if forbidden:
                    print(f"      forbidden claims: {forbidden}")
                if contradicted:
                    print(f"      contradiction candidates: {contradicted}")
                else:
                    print("      contradiction candidates: none")

        assert report is not None

        flaky = _flake_summary(runs, cases)

        elapsed = time.monotonic() - started
        _write_report(report, tenant_id=tenant_id, elapsed=elapsed, samples=samples, flaky=flaky)
        print(
            f"\n{report.passed}/{report.total} passed, "
            f"citation_violations={report.citation_violations}, "
            f"abstention_false={report.abstention_false}, "
            f"forbidden_claim_hits={report.forbidden_claim_hits}, "
            f"contradiction_candidates={report.contradiction_candidates}"
        )
        print(f"report written to {REPORT_PATH.relative_to(REPO_ROOT)} ({elapsed:.0f}s)")
        for result in report.results:
            if not result.passed:
                print(f"  FAILED {result.case_id}: {result.reason_codes}")
        if samples > 1:
            if flaky:
                print(f"\nflaky ({samples} samples) - passed some runs, failed others:")
                for case_id, failures in sorted(flaky.items(), key=lambda kv: -kv[1]):
                    print(f"  {case_id}: failed {failures} of {samples}")
            else:
                print(f"\nno flakiness: every case gave the same result across {samples} samples")
        return 0
    finally:
        if tenant_id is not None and not args.keep_tenant:
            await _cleanup(platform, tenant_id=tenant_id, slug=slug)
            print(f"cleaned up tenant {slug}")
        elif tenant_id is not None:
            print(f"left tenant {slug} in place (--keep-tenant)")
        await app_engine.dispose()
        await platform_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop))
