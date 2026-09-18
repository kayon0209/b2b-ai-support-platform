"""Compliance export: one bounded, audited extract of a tenant's own data.

What this is for
----------------
`docs/development-plan.md` Phase 5 lists "Retention and compliance controls".
The retention half existed (`evaluation/pii.py` plus the retention sweep); the
control that was missing is the tenant's ability to get its own data *out* -
the thing an audit, a data-protection request, or a contract termination needs.

Four decisions, and each is about a way an export goes wrong:

**Bounded window, required.** An unbounded export is a full-table scan of every
audit row a tenant has ever written, and it is also the most convenient way to
exfiltrate the lot in one request. `since` and `until` are mandatory and the
window has a maximum width. The bound is a limit on *how much one request can
read*, not on how much data the tenant can have - a wider range is several
requests, which is a feature: each one lands in the audit trail.

**`truncated`, never a silent stop.** Every section reports whether it hit the
row ceiling. A compliance extract that quietly returns the first 5,000 rows and
looks complete is worse than one that fails, because the person who relies on
it has no signal that anything is missing.

**The export is a data-access event, so it is audited - and the audit row does
not contain what was exported.** Recording the content would make the trail a
second copy of the data it describes. It records the window, the sections, the
row counts and the actor.

**This grants no data a role cannot already read.** The `COMPLIANCE_EXPORT`
action is held only by roles that hold both `AUDIT_READ` and `CASE_READ`, and a
test asserts that invariant. An export endpoint is exactly the kind of feature
that quietly widens access; the rule is written down so it cannot.

Usage figures are deliberately **not** included: `/v1/tenant/usage` already
serves them as a first-class resource, and duplicating that projection here
would create two answers to the same question.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.audit import service as audit_service
from platform_core.cases import service as case_service
from platform_core.evaluation.pii import DEFAULT_RETENTION, RetentionPolicy
from platform_core.identity.tenant_context import TenantContext

# One year. Long enough for any annual review, short enough that nobody's
# "export everything" is a single 40-year scan.
MAX_WINDOW_SECONDS = 366 * 24 * 3600

# Per section, per request. Chosen so a default reply stays a few megabytes:
# an extract that has to be paginated is one a person can actually open.
MAX_ROWS_PER_SECTION = 5_000

AUDIT_EXPORTED = "compliance.export.created"

# Section name -> whether it is included when the caller does not choose.
DEFAULT_SECTIONS: tuple[str, ...] = ("audit", "cases")
KNOWN_SECTIONS: frozenset[str] = frozenset({"audit", "cases"})


class ComplianceError(Exception):
    """An export request that must be refused, with a caller-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class ExportRequest:
    since: int
    until: int
    sections: tuple[str, ...]
    max_rows: int = MAX_ROWS_PER_SECTION

    @property
    def window_seconds(self) -> int:
        return self.until - self.since


def parse_request(
    *,
    since: int | None,
    until: int | None,
    sections: list[str] | None,
    now: int | None = None,
) -> ExportRequest:
    """Validate the window and the section list.

    `until` defaults to now and `since` does not default at all. A default
    `since` would be a policy decision about how far back the platform thinks a
    request should reach, and the useful answer depends on the request; making
    the caller state it is one parameter and removes the guess.
    """
    if since is None:
        raise ComplianceError("EXPORT_SINCE_REQUIRED")
    if until is None:
        until = now if now is not None else int(time.time())
    if since > until:
        raise ComplianceError("EXPORT_WINDOW_INVERTED")
    if until - since > MAX_WINDOW_SECONDS:
        raise ComplianceError("EXPORT_WINDOW_TOO_WIDE")

    # `None` means unspecified; an explicit empty list means "no sections",
    # which is refused below rather than silently replaced by the defaults -
    # returning everything when the caller asked for nothing is a surprise,
    # not a convenience.
    chosen = DEFAULT_SECTIONS if sections is None else tuple(sections)
    unknown = sorted(set(chosen) - KNOWN_SECTIONS)
    if unknown:
        raise ComplianceError("EXPORT_UNKNOWN_SECTION", ",".join(unknown))
    if not chosen:
        raise ComplianceError("EXPORT_NO_SECTIONS")
    return ExportRequest(since=since, until=until, sections=chosen)


async def build_export(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    request: ExportRequest,
    policy: RetentionPolicy = DEFAULT_RETENTION,
    now: int | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Assemble the extract and record that it was taken.

    Everything here runs in the caller's transaction, which is RLS-bound to this
    tenant. The `tenant_id` filters are explicit as well: they are redundant
    with the policy, and redundancy is the point - an export that relied only on
    RLS would leak every tenant's data the day a session is used without the
    binding.
    """
    generated_at = now if now is not None else int(time.time())
    tenant_id = ctx.tenant_id

    sections: dict[str, Any] = {}
    truncated: dict[str, bool] = {}

    if "audit" in request.sections:
        events, cut = await audit_service.export_events(
            session,
            tenant_id=tenant_id,
            since=request.since,
            until=request.until,
            limit=request.max_rows,
        )
        sections["audit"] = events
        truncated["audit"] = cut

    if "cases" in request.sections:
        cases, escalations, cut = await case_service.export_cases(
            session,
            tenant_id=tenant_id,
            since=request.since,
            until=request.until,
            limit=request.max_rows,
        )
        sections["cases"] = cases
        sections["case_escalations"] = escalations
        truncated["cases"] = cut

    counts = {name: len(rows) for name, rows in sections.items() if isinstance(rows, list)}

    # The audit row records the request, not the response. Counts and the
    # window are what an investigator needs; the content would make the trail a
    # second copy of the data it is describing.
    await audit_service.record(
        session,
        ctx=ctx,
        action=AUDIT_EXPORTED,
        resource_type="compliance_export",
        resource_id=uuid.uuid4(),
        decision="completed",
        reason_code="OK",
        after={
            "since": request.since,
            "until": request.until,
            "sections": list(request.sections),
        },
        # Readable, because for this event the *parameters* are the content:
        # "which window, how many rows" is the question an investigator asks,
        # and a hash of it answers nothing. The exported data itself is not
        # here - that would make the trail a second copy of what it describes.
        metadata={
            "since": request.since,
            "until": request.until,
            "sections": list(request.sections),
            "row_counts": counts,
            "truncated": truncated,
        },
        trace_id=trace_id,
    )

    return {
        "manifest": {
            "generated_at": generated_at,
            "since": request.since,
            "until": request.until,
            "sections": list(request.sections),
            "row_counts": counts,
            # `truncated` is a first-class part of the response rather than a
            # footnote: it is the difference between "this is your data" and
            # "this is the first 5,000 rows of your data".
            "truncated": truncated,
            "max_rows_per_section": request.max_rows,
            # The retention policy that applies to this data, so the recipient
            # knows how long the platform will keep it without having to ask.
            "retention": {
                "superseded_version_days": policy.superseded_version_days,
                "dead_letter_days": policy.dead_letter_days,
                "inbox_event_days": policy.inbox_event_days,
            },
            "excludes": ["usage (see /v1/tenant/usage)"],
        },
        "sections": sections,
    }


__all__ = [
    "AUDIT_EXPORTED",
    "DEFAULT_SECTIONS",
    "KNOWN_SECTIONS",
    "MAX_ROWS_PER_SECTION",
    "MAX_WINDOW_SECONDS",
    "ComplianceError",
    "ExportRequest",
    "build_export",
    "parse_request",
]
