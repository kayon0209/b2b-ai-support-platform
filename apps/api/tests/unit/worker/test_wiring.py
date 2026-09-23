"""Unit tests: worker dependency composition.

The bug these tests exist to prevent: `runner.main()` built its deps with
`embedder=None, generator=None, sender=None`. The orchestrator treats a
missing generator as abstention and a missing sender as "un-sent, not
failed", so the worker would claim real customer messages, mark the inbox
rows COMPLETED, and reply to nobody — with no error anywhere.

Every assertion here is about refusing to run in a state that silently
does nothing.
"""

from typing import Any

import pytest
from pydantic import SecretStr

from platform_core.agent_runtime.generator import LlmAnswerGenerator
from platform_core.agent_runtime.orchestrator import OrchestratorDeps
from platform_core.integrations.resilience import CircuitBreaker
from platform_core.llm.provider import ConcreteModelBundle
from platform_core.retrieval.hybrid import ProviderEmbedder
from worker.wiring import (
    WorkerConfigurationError,
    audit_wiring,
    build_interactive_deps,
)


class _StubChat:
    """Minimal ChatProvider: only identity matters for wiring."""

    _chat_model = "stub-model"

    async def chat(self, messages: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("not called")

    async def embed(self, texts: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("not called")

    async def rerank(self, query: str, documents: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("not called")


@pytest.fixture
def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the LLM boundary and an outbound channel are configured.

    The stubs are the real types, not `object()`, so a bare object cannot
    fail for the wrong reason and hide whether the wiring itself is correct.
    """
    import platform_core.config as config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "llm_api_key", SecretStr("stub-llm-key"), raising=False)
    # An SMTP host and a from-address are what `build_channel_sender` needs to
    # register the email transport (ADR 0014).
    monkeypatch.setattr(settings, "email_smtp_host", "smtp.test", raising=False)
    monkeypatch.setattr(settings, "email_from_address", "support@acme.test", raising=False)

    monkeypatch.setattr(
        "worker.wiring.get_model_bundle",
        lambda: ConcreteModelBundle(
            chat=_StubChat(),  # type: ignore[arg-type]
            embedding=_StubChat(),  # type: ignore[arg-type]
            rerank=_StubChat(),  # type: ignore[arg-type]
            breaker=CircuitBreaker(),
        ),
    )


def test_missing_chat_provider_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """No LLM key must abort, not degrade into a silent no-op.

    A worker without a chat provider abstains on every run. It would still
    consume inbox rows and mark them COMPLETED, which reads as success.
    """
    import worker.wiring as wiring

    monkeypatch.setattr(wiring, "get_model_bundle", lambda: None)

    with pytest.raises(WorkerConfigurationError) as excinfo:
        build_interactive_deps()

    assert "APP_LLM_API_KEY" in str(excinfo.value)
    # SystemExit subclass, so a container exits non-zero with a message
    # rather than a traceback.
    assert isinstance(excinfo.value, SystemExit)


def test_draft_only_mode_allows_no_chat_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit draft-only deployment is allowed to run without a model."""
    import worker.wiring as wiring

    monkeypatch.setattr(wiring, "get_model_bundle", lambda: None)

    deps = build_interactive_deps(require_chat=False)

    assert deps.generator is None
    assert deps.channel_sender is None
    wiring_report = audit_wiring(deps)
    assert wiring_report.can_generate is False


def test_configured_worker_binds_every_collaborator(_configured: None) -> None:
    """The happy path must produce a worker that can generate AND send."""
    from platform_core.channels.outbound import ChannelSender

    deps = build_interactive_deps()
    report = audit_wiring(deps)

    assert isinstance(deps.generator, LlmAnswerGenerator)
    assert isinstance(deps.embedder, ProviderEmbedder)
    assert isinstance(deps.channel_sender, ChannelSender)
    assert deps.channel_sender.systems == ("email",)

    assert report.can_generate is True
    assert report.can_send is True
    assert report.has_embedding is True
    assert report.has_rerank is True


def test_no_outbound_transport_disables_send_but_not_generate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no channel transport the worker can still draft, and says so.

    `can_send=False` is the signal an operator needs: runs will record their
    outcome without a customer-visible reply. It must never be reported as a
    delivery - that is the `worker_cannot_send` defect.
    """
    import worker.wiring as wiring

    monkeypatch.setattr(
        wiring,
        "get_model_bundle",
        lambda: ConcreteModelBundle(
            chat=_StubChat(),  # type: ignore[arg-type]
            embedding=_StubChat(),  # type: ignore[arg-type]
            rerank=_StubChat(),  # type: ignore[arg-type]
            breaker=CircuitBreaker(),
        ),
    )
    monkeypatch.setattr(wiring.get_settings(), "email_smtp_host", None, raising=False)
    monkeypatch.setattr(wiring.get_settings(), "email_from_address", None, raising=False)
    monkeypatch.setattr(wiring.get_settings(), "wechat_app_id", None, raising=False)
    monkeypatch.setattr(wiring.get_settings(), "wechat_app_secret", None, raising=False)

    deps = build_interactive_deps()
    report = audit_wiring(deps)

    assert report.can_generate is True
    assert report.can_send is False
    assert deps.channel_sender is not None
    assert deps.channel_sender.systems == ()


def test_required_sender_fails_closed_when_token_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment that demands outbound capability must not start without it."""
    import worker.wiring as wiring

    monkeypatch.setattr(
        wiring,
        "get_model_bundle",
        lambda: ConcreteModelBundle(
            chat=_StubChat(),  # type: ignore[arg-type]
            embedding=_StubChat(),  # type: ignore[arg-type]
            rerank=_StubChat(),  # type: ignore[arg-type]
            breaker=CircuitBreaker(),
        ),
    )
    monkeypatch.setattr(wiring.get_settings(), "email_smtp_host", None, raising=False)
    monkeypatch.setattr(wiring.get_settings(), "email_from_address", None, raising=False)
    monkeypatch.setattr(wiring.get_settings(), "wechat_app_id", None, raising=False)
    monkeypatch.setattr(wiring.get_settings(), "wechat_app_secret", None, raising=False)

    with pytest.raises(WorkerConfigurationError) as excinfo:
        build_interactive_deps(require_sender=True)

    assert "outbound transport" in str(excinfo.value)


def test_audit_reports_generation_from_generator_not_bundle() -> None:
    """`has_chat` must track the generator, which is what the orchestrator needs.

    A bundle present but no generator is exactly the state that abstains on
    every run, so the audit must not call it healthy.
    """
    deps = OrchestratorDeps(
        generator=None,
        embedder=None,
        extra={"bundle": object()},
    )
    assert audit_wiring(deps).can_generate is False
