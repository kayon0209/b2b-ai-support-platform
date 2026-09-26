"""Feature list 11.2: per-task model routing.

Pinned because the value of this feature is entirely in its fallback:

- **Unset means "use the default", not "disabled".** Every existing deployment
  runs with no per-task override, and turning routing on must be one variable,
  not a code change. A regression here would surface as the whole platform
  losing its model.
- **Routing is per task, not global.** Classification and generation are the
  two calls with different requirements, and a router that returns the same
  model for both is indistinguishable from having no router - so the difference
  is asserted, not just the plumbing.
"""

from __future__ import annotations

import pytest

from platform_core.config import get_settings
from platform_core.llm.factory import ChatTask, chat_model_for


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch):
    """Settings are cached; each test needs its own view of the environment."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_generation_uses_the_default_model(monkeypatch) -> None:
    monkeypatch.setenv("APP_LLM_MODEL", "generation-model")
    get_settings.cache_clear()
    assert chat_model_for(ChatTask.GENERATE) == "generation-model"


def test_classification_falls_back_to_the_default_when_unset(monkeypatch) -> None:
    """The opt-in property: an unconfigured deployment is unchanged."""
    monkeypatch.setenv("APP_LLM_MODEL", "only-model")
    monkeypatch.delenv("APP_LLM_MODEL_CLASSIFY", raising=False)
    get_settings.cache_clear()
    assert chat_model_for(ChatTask.CLASSIFY) == "only-model"


def test_classification_uses_its_own_model_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("APP_LLM_MODEL", "generation-model")
    monkeypatch.setenv("APP_LLM_MODEL_CLASSIFY", "cheap-model")
    get_settings.cache_clear()
    assert chat_model_for(ChatTask.CLASSIFY) == "cheap-model"


def test_the_two_tasks_can_be_routed_apart(monkeypatch) -> None:
    """A router that cannot do this is not routing."""
    monkeypatch.setenv("APP_LLM_MODEL", "generation-model")
    monkeypatch.setenv("APP_LLM_MODEL_CLASSIFY", "cheap-model")
    get_settings.cache_clear()
    assert chat_model_for(ChatTask.CLASSIFY) != chat_model_for(ChatTask.GENERATE)


def test_an_empty_override_is_treated_as_unset(monkeypatch) -> None:
    """A blank env var must not become a model name of ""."""
    monkeypatch.setenv("APP_LLM_MODEL", "only-model")
    monkeypatch.setenv("APP_LLM_MODEL_CLASSIFY", "")
    get_settings.cache_clear()
    assert chat_model_for(ChatTask.CLASSIFY) == "only-model"


def test_the_task_enum_names_are_stable(monkeypatch) -> None:
    """They appear in config variable names; renaming breaks deployments."""
    assert ChatTask.CLASSIFY.value == "classify"
    assert ChatTask.GENERATE.value == "generate"
