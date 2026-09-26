"""case.create: open a case from the platform's own tables.

Stage 3's quality-complaint semi-automation: a customer complaint arrives in a
conversation, a person opens a case for it, and the adjudication of that
complaint stays human. This tool is the "open a case" half.

**Why this is `confirmed_write` and not `human_approval`.** `case.eq_confirm`
is `human_approval` because the case status *is* the signal the factory reads -
recording a confirmation is one step from releasing production. Creating a case
does not move anything in the outside world: it writes a row, and the SLA clock
it starts constrains us. The research report's risk inventory puts only "EQ
放行、赔付、退款" under `HUMAN_APPROVAL`; stage 3's phrasing is "`case.create` +
evidence attachments -> **人工裁定**", where the human judgement lands on the
complaint's resolution, not on the act of opening a ticket. `support_agent`
already holds `CASE_CREATE`, so opening a case is a documented routine action,
and promoting it to `human_approval` would contradict that table. See
`docs/adr/0008-case-create-risk-class.md`.

**Why `enterprise_account_id` is required and must come from a person.**
`create_case` resolves the account to an SLA tier and computes both deadlines
from it, and the tier is *snapshotted* onto the row (the service comment: a
deadline is recomputed on a priority change, so re-reading the account later
would let a mid-case contract change move a clock that is already running).
A wrong account therefore grants a wrong clock that never self-corrects. The
plan's most complaint-prone customers are the top of the four account tiers, so
the error lands on the most expensive tier. There is no account-reference
resolver comparable to `_find_locked`: account names and abbreviations have no
uniqueness guarantee here, so "guess the account" would mean feeding a string
similarity score into an SLA deadline.

Silent degradation to `None` is rejected because its failure mode is invisible:
`create_case` succeeds without an account, the case exists, the panel is green,
and the ticket can never escalate for a missed first response - nothing errors,
which is what makes it expensive. Three guards enforce the requirement: the
schema's `required` list (so a proposal missing it is never created), an
explicit check here (so a direct executor call is refused too), and a negative
test.

**Why the agent does not propose this one.** Under the rule above the account
id is a human decision, and `subject`/`description` are free text, so a
deterministic extractor cannot produce the arguments. Listing it in the write
selector would mean a candidate that is picked and then always fails, which is
indistinguishable in the selection audit from never having shipped the tool -
the same reasoning that excludes `crm.update_account`. The tool is reachable
through the console instead: a `support_agent`/`support_admin` proposes and a
`support_admin`/`tenant_owner` confirms. An utterance like "帮我建一张工单"
routes to `BUSINESS_WRITE` (since `create` is in `ACTION_VERBS`) and ends in a
clean handoff.

**Tenant identity comes from the resolver, not from the arguments.** A create
has no row for RLS to filter on, so the executor has to name the tenant it is
writing into - and the one place that is resolved server-side is the
`ConnectorExecutorResolver` that builds this executor. Reading `tenant_id` out
of `parameters` would be the exact shortcut AGENTS.md forbids ("Passing
client-supplied `tenant_id` through without server-side resolution"), so the
argument is not accepted at all and the schema does not declare it.

The input schema lives in `registry.TOOL_CATALOG`, not here, for the reason
recorded in `case_eq_confirm.py`: importing `cases.models` at registry-import
time configures the Case mapper before `enterprise_accounts` exists and
SQLAlchemy raises `NoReferencedTableError`. The schema is the part that needs
no model.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.cases.models import Case, CaseCategory
from platform_core.cases.service import CaseError, CaseService

# Mirrors `cases.router.VALID_PRIORITIES`. Duplicated rather than imported
# because the router module pulls in the HTTP layer; the two are kept in step
# by a test that asserts this set equals the router's.
_ALLOWED_PRIORITIES = frozenset({"p0", "p1", "p2", "p3"})
_DEFAULT_PRIORITY = "p2"


class CaseCreateExecutor:
    """ToolExecutor for case.create. Session-bound; never touches a connector."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: Any,
        actor_id: uuid.UUID | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._actor_id = actor_id

    async def execute(
        self, tool_name: str, parameters: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        if self._tenant_id is None:
            # Not a fallback case: without a server-resolved tenant there is
            # nowhere legitimate to write, and inventing one would be worse
            # than refusing.
            return {"ok": False, "error_code": "TENANT_UNRESOLVED"}

        account_id_raw = parameters.get("enterprise_account_id")
        account_id: uuid.UUID | None = None
        if account_id_raw is not None and str(account_id_raw).strip():
            try:
                account_id = uuid.UUID(str(account_id_raw).strip())
            except (ValueError, AttributeError):
                return {
                    "ok": False,
                    "error_code": "ACCOUNT_ID_MALFORMED",
                    "enterprise_account_id": str(account_id_raw),
                }

        if account_id is None:
            # Refused, not defaulted. The schema's `required` list should have
            # stopped this before a proposal row existed, so reaching here means
            # a caller went around the gateway - which is exactly when a loud
            # refusal is worth more than the quiet success a `None` would give.
            return {"ok": False, "error_code": "ENTERPRISE_ACCOUNT_REQUIRED"}

        subject = " ".join(str(parameters.get("subject") or "").split())
        if not subject:
            return {"ok": False, "error_code": "SUBJECT_REQUIRED"}

        priority = str(parameters.get("priority") or _DEFAULT_PRIORITY).strip().lower()
        if priority not in _ALLOWED_PRIORITIES:
            return {"ok": False, "error_code": "PRIORITY_INVALID", "priority": priority}

        try:
            case = await CaseService(self._session).create_case(
                tenant_id=uuid.UUID(str(self._tenant_id)),
                subject=subject,
                description=str(parameters.get("description") or ""),
                priority=priority,
                category=str(parameters.get("category") or CaseCategory.GENERAL.value).strip(),
                actor_id=self._actor_id,
                enterprise_account_id=account_id,
                conversation_ref_id=_optional_uuid(parameters.get("conversation_ref_id")),
            )
        except CaseError as exc:
            # `ACCOUNT_NOT_FOUND` from the service means "no such account, or not
            # this tenant's" (RLS cannot see the latter, and distinguishing them
            # would make this a way to enumerate account ids). Passed through
            # unchanged so one situation keeps one code.
            return {"ok": False, "error_code": exc.code}

        return {
            "ok": True,
            "case_id": str(case.id),
            "status": case.status,
            "priority": case.priority,
            "category": case.category,
            "sla_tier": case.sla_tier,
            "first_response_due_at": _iso(case.first_response_due_at),
            "resolution_due_at": _iso(case.resolution_due_at),
        }

    async def verify_postcondition(
        self, tool_name: str, parameters: dict[str, Any], output: dict[str, Any] | None
    ) -> bool | None:
        """Re-read the case and confirm it carries the SLA snapshot it claims.

        Read back rather than trusting the returned dict, for the same reason as
        the other platform tools: a postcondition is worth having because it is
        observed. The account and tier are part of the check because a case
        created without them is the failure this tool has to not have - a row
        that exists but carries no SLA tier is a green panel over a ticket that
        will never escalate for a missed first response.
        """
        if not isinstance(output, dict) or not output.get("ok"):
            return False
        case_id = output.get("case_id")
        if not isinstance(case_id, str):
            return False
        row = (
            await self._session.execute(select(Case).where(Case.id == uuid.UUID(case_id)))
        ).scalar_one_or_none()
        if row is None:
            return False
        return row.enterprise_account_id is not None and row.sla_tier is not None


def _optional_uuid(raw: Any) -> uuid.UUID | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return uuid.UUID(str(raw).strip())
    except ValueError:
        return None


def _iso(value: Any) -> str | None:
    """Render a deadline for the receipt.

    The deadlines are epoch seconds, not datetimes - the repo stores timestamps
    as UTC integers and renders them at the boundary - so this must not assume
    `.isoformat()`. Rendered as ISO 8601 so an operator reading the receipt
    does not have to convert a timestamp in their head.
    """
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None
