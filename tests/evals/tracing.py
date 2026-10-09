"""Local-only DeepEval tracing helpers for evaluation harnesses.

DeepEval is an optional evaluation dependency and is never imported by the
production API or worker. The eval process disables dotenv/keyfile discovery
and hosted credentials before importing it, so traces remain local.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

_F = TypeVar("_F", bound=Callable[..., Any])

os.environ["DEEPEVAL_DISABLE_DOTENV"] = "1"
os.environ["DEEPEVAL_DISABLE_LEGACY_KEYFILE"] = "1"
os.environ["DEEPEVAL_TELEMETRY_OPT_OUT"] = "1"
os.environ["CONFIDENT_API_KEY"] = ""
os.environ["CONFIDENT_TRACE_VERBOSE"] = "0"
os.environ["CONFIDENT_TRACE_FLUSH"] = "0"
os.environ["DEEPEVAL_LOCAL_STORE"] = "json"
os.environ.setdefault(
    "DEEPEVAL_RESULTS_FOLDER",
    str(Path(__file__).resolve().parents[1] / "artifacts" / "deepeval"),
)
os.environ.setdefault(
    "DEEPEVAL_CACHE_FOLDER",
    str(Path(__file__).resolve().parents[1] / "artifacts" / "deepeval" / "state"),
)

try:
    from deepeval.tracing import observe as _deepeval_observe
except ImportError:  # optional dependency; ordinary unit tests remain importable
    _deepeval_observe = None


def tracing_available() -> bool:
    return _deepeval_observe is not None


def require_tracing() -> None:
    if _deepeval_observe is None:
        raise RuntimeError(
            "DeepEval tracing is unavailable; install requirements-evals.txt to run traced evals"
        )


def local_observe(*, span_type: str, name: str) -> Callable[[_F], _F]:
    """Decorate an eval-only function; pass through when the optional SDK is absent."""

    def decorate(function: _F) -> _F:
        if _deepeval_observe is None:
            return function
        observed = _deepeval_observe(type=span_type, name=name)(function)
        return observed  # type: ignore[return-value]

    return decorate
