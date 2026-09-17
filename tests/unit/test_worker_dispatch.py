"""Worker queue dispatch (`APP_WORKER_QUEUE`).

The defect this covers
----------------------
`infra/compose/docker-compose.yml` declares an `ai-worker-ingestion` service
with `APP_WORKER_QUEUE: ingestion`, but `runner.main()` never read that
variable: it had exactly two branches, `--outbox-only` and
`build_interactive_deps()`. So the ingestion container started, ran the
*interactive* worker, and no document was ever indexed - while compose, the
logs, and the container health all looked correct.

Two properties are asserted here, and they are different concerns:

1. **Dispatch.** Each queue name selects the right worker class.
2. **No silent fallback.** An unknown value is fatal. A typo in a deployment
   manifest must not downgrade to the interactive worker, which would consume
   customer messages instead of indexing documents - a wrong-but-running
   process is worse than one that refuses to start.

These are unit tests: they never construct a worker, so no database, model
provider, or credential is needed.
"""

import pytest

from worker.runner import QUEUE_ENV_VAR, resolve_queue
from worker.wiring import WorkerConfigurationError


def test_no_setting_defaults_to_the_interactive_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local `python -m worker.runner` must keep its original meaning.

    The env var is additive: a developer who runs the worker by hand today
    gets the customer-facing loop, not a queue that was never configured.
    """
    monkeypatch.delenv(QUEUE_ENV_VAR, raising=False)
    assert resolve_queue([]) == "interactive"


def test_ingestion_is_selected_by_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(QUEUE_ENV_VAR, "ingestion")
    assert resolve_queue([]) == "ingestion"


def test_outbox_is_selected_by_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(QUEUE_ENV_VAR, "outbox")
    assert resolve_queue([]) == "outbox"


def test_blank_value_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose injects `${VAR:-}`, so "set but empty" is a real state.

    Treating it as a value would make the service fail to start for a
    variable the operator deliberately left blank.
    """
    monkeypatch.setenv(QUEUE_ENV_VAR, "   ")
    assert resolve_queue([]) == "interactive"


def test_value_is_case_and_whitespace_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(QUEUE_ENV_VAR, "  Ingestion  ")
    assert resolve_queue([]) == "ingestion"


def test_outbox_only_flag_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag is the more specific instruction.

    A deployment that passes `--outbox-only` is asking for a one-shot relay
    regardless of what its manifest says about the default queue.
    """
    monkeypatch.setenv(QUEUE_ENV_VAR, "interactive")
    assert resolve_queue(["--outbox-only"]) == "outbox"


def test_outbox_only_flag_wins_even_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(QUEUE_ENV_VAR, raising=False)
    assert resolve_queue(["--outbox-only"]) == "outbox"


def test_an_unknown_queue_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not silently run a different worker.

    This is the regression guard for the original bug's shape: a process that
    starts cleanly and does the wrong job. `ingest` instead of `ingestion`
    would otherwise index nothing, forever, with no error anywhere.
    """
    monkeypatch.setenv(QUEUE_ENV_VAR, "ingest")
    with pytest.raises(WorkerConfigurationError) as exc:
        resolve_queue([])
    assert "ingest" in str(exc.value)
    # The message must name the valid options; an operator reading a container
    # log needs the fix, not just the complaint.
    assert "interactive" in str(exc.value)
    assert "ingestion" in str(exc.value)
    assert "outbox" in str(exc.value)


def test_the_batch_client_size_is_not_used_for_ingestion() -> None:
    """Ingestion defaults to a smaller batch than the inbox worker.

    Ingestion is the bulk, lowest-priority class: each claimed document costs
    a storage read and one or more embedding calls, so a large batch holds a
    worker for minutes. The inbox default (20) would do exactly that.
    """
    from worker.runner import DEFAULT_BATCH, IngestionWorker, WorkerConfig
    from worker.wiring import IngestionDeps

    worker = IngestionWorker(IngestionDeps(embedder=object()))
    assert worker._config.batch < DEFAULT_BATCH  # noqa: SLF001 - the knob is the point
    assert WorkerConfig().batch == DEFAULT_BATCH
