"""Billing ledger: usage events and their monthly rollup.

Deliberately does **not** import `.service` at module scope. `models_registry`
imports this package for its side effect of registering `BillingEntry` with
`Base.metadata`, and that import happens *inside* `platform_core.db` (before
`db` has finished initialising). `service` pulls in `identity.usage`, which
pulls in `platform_core.api`, which imports `session_scope_with_url` from the
half-built `db` - a circular import that only shows up when `db` is the first
module loaded, which is exactly what a worker does.

So the model is re-exported eagerly (it is what the registry needs) and the
service symbols are resolved on first attribute access via `__getattr__`.
Callers still write `from platform_core.billing import monthly_rollup`.
"""

from typing import TYPE_CHECKING, Any

from platform_core.billing.models import BillingEntry, EntryKind

if TYPE_CHECKING:  # pragma: no cover - typing only
    from platform_core.billing.service import (
        BillingRollup,
        RecordOutcome,
        handle_usage_recorded,
        monthly_rollup,
        record_adjustment,
        record_usage,
    )

_SERVICE_SYMBOLS = frozenset(
    {
        "BillingRollup",
        "RecordOutcome",
        "handle_usage_recorded",
        "monthly_rollup",
        "record_adjustment",
        "record_usage",
    }
)

__all__ = [
    "BillingEntry",
    "BillingRollup",
    "EntryKind",
    "RecordOutcome",
    "handle_usage_recorded",
    "monthly_rollup",
    "record_adjustment",
    "record_usage",
]


def __getattr__(name: str) -> Any:
    if name in _SERVICE_SYMBOLS:
        from platform_core.billing import service

        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
