"""Unit tests: write-tool selection and argument assembly (iteration plan 3.5).

The write path differs from the read path in exactly one way that matters to
these tests: selecting a write tool authorises a *proposal*, never an
execution. So the tests pin the two things a caller depends on — that the
selector fires only on the write route, and that every tool it can offer has a
deterministic way to be given arguments.

That second property is the one worth being explicit about. A write tool the
selector offers but cannot be given arguments for would be picked, fail to
build arguments, and hand off every single time — indistinguishable from not
shipping it, which is the defect shape this repository keeps finding.
"""

from __future__ import annotations

from typing import Any

import pytest

from platform_core.agent_runtime.intent import IntentDetection, classify
from platform_core.agent_runtime.orchestrator import _extract_write_args
from platform_core.integrations.models import Connector, ConnectorStatus
from platform_core.tool_gateway.registry import (
    WRITE_ARG_DEFAULTS,
    ConnectorExecutorResolver,
)
from platform_core.tool_gateway.selector import (
    _WRITE_SCENE_AFFINITY,
    select_read_tools,
    select_write_tools,
)

TENANT = "01900000-0000-7000-8000-0000000000e1"

# An utterance the taxonomy routes to the write path. "escalate" is one of the
# shared ACTION_VERBS in qa_path, and it is the verb a customer actually uses
# for both halves of this vocabulary.
ESCALATION = "Please escalate this defect to your engineering team"


def _write_detection(question: str) -> IntentDetection:
    """Classify, asserting the premise these tests rest on.

    Without this the assertions below would pass vacuously the day the
    taxonomy moves: `select_write_tools` returns `[]` for anything not routed
    to the write path, so a question that stopped routing there would make
    "no candidates" look like a correct answer.
    """
    detection = classify(question)
    assert detection.route.value == "business_write", (
        f"{question!r} no longer routes to the write path ({detection.route.value}); "
        "this test's premise moved and its assertions would pass vacuously"
    )
    return detection


def test_write_selection_is_gated_on_the_write_route() -> None:
    """The route is the gate on both sides: the vocabularies never cross.

    A read question must not select a write tool (that would propose a change
    nobody asked for) and a write question must not select a read tool (that
    would answer a request for action with a receipt for a lookup).
    """
    read_question = "Where is my order 88123?"
    read_detection = classify(read_question)
    assert read_detection.route.value == "business_read"
    assert select_read_tools(read_detection, read_question)
    assert select_write_tools(read_detection, read_question) == []

    write_detection = _write_detection(ESCALATION)
    assert select_write_tools(write_detection, ESCALATION)
    assert select_read_tools(write_detection, ESCALATION) == []


def test_escalating_a_defect_offers_both_the_ticket_and_the_notification() -> None:
    detection = _write_detection(ESCALATION)
    names = {c.tool_name for c in select_write_tools(detection, ESCALATION)}
    assert "im.send_notification" in names
    assert names & {"jira.create_issue", "linear.create_issue"}


def test_unavailable_write_tools_are_dropped_not_downgraded() -> None:
    detection = _write_detection(ESCALATION)
    candidates = select_write_tools(detection, ESCALATION, available={"jira.create_issue"})
    assert {c.tool_name for c in candidates} == {"jira.create_issue"}


def test_write_selection_is_deterministic() -> None:
    detection = _write_detection(ESCALATION)
    first = select_write_tools(detection, ESCALATION)
    second = select_write_tools(detection, ESCALATION)
    assert [c.tool_name for c in first] == [c.tool_name for c in second]
    # Pin the ordering contract itself rather than one arbitrary winner: score
    # descending, then name. Two equally-scoring tools must always come back
    # in the same order, or the audit trail records a coin flip.
    assert [(c.tool_name, c.score) for c in first] == sorted(
        ((c.tool_name, c.score) for c in first), key=lambda pair: (-pair[1], pair[0])
    )


def test_every_offered_write_tool_can_be_given_arguments() -> None:
    """A selector entry with no extraction path is a permanent handoff.

    Also checks that every offered tool has a declared source for the
    arguments the customer never supplies — the selector and the defaults
    table have to agree, because the orchestrator consults the second for
    every name the first returns.
    """
    for tool_name in _WRITE_SCENE_AFFINITY:
        assert tool_name in WRITE_ARG_DEFAULTS, f"{tool_name} has no declared write defaults"
        defaults = {argument: "x" for argument, _ in WRITE_ARG_DEFAULTS[tool_name]}
        assert _extract_write_args(tool_name, "please do the thing", defaults) is not None, (
            tool_name
        )


def test_a_tool_with_no_deterministic_extraction_is_not_offered() -> None:
    """`crm.update_account` takes a free-form field patch.

    Deriving that patch from an utterance needs a model, and the platform's
    rule is that deterministic code decides what is written to an external
    system. So it is deliberately absent from the selector rather than
    present-but-always-handing-off: a candidate that can never win is noise
    in the selection audit. A customer asking for an account change is
    handled by the action-request refusal instead.
    """
    assert "crm.update_account" not in _WRITE_SCENE_AFFINITY
    assert _extract_write_args("crm.update_account", "please update our address", {}) is None


def test_asking_for_a_case_is_not_offered_case_create() -> None:
    """`case.create` exists, is reachable from the console, and is not proposed.

    This is the load-bearing half of ADR 0008. `ACTION_VERBS` contains `create`,
    so "please create a ticket" routes to the write path - which means the
    *absence* of a candidate is the outcome the handoff depends on. If
    `case.create` were ever added to the selectors it would be picked, then fail
    argument construction (the account id is a human decision and `subject` is
    free text), and hand off anyway - a trade that only costs audit clarity.

    Asserting the route first matters: `select_write_tools` returns `[]` for
    anything not routed to `business_write`, so without it this test would pass
    vacuously the day the classifier stopped routing case requests there.
    """
    for question in (
        "Please create a case for this complaint",
        "I want to report a quality problem, open a ticket",
        "create a case",
        "report a quality problem",
        "open a ticket",
    ):
        detection = _write_detection(question)
        # Not "no candidates": a complaint utterance legitimately reaches
        # `im.send_notification` through scene affinity, and that is existing
        # designed behaviour. The claim is narrower and that is the point -
        # `case.create` is never among them.
        selected = {c.tool_name for c in select_write_tools(detection, question)}
        assert "case.create" not in selected, (question, selected)

    # Named explicitly, so the reason is the tool rather than a coincidence of
    # scene affinity when the vocabularies change.
    assert "case.create" not in _WRITE_SCENE_AFFINITY
    assert "case.create" not in WRITE_ARG_DEFAULTS
    assert _extract_write_args("case.create", "please create a case", {}) is None


def test_chinese_case_requests_reach_the_write_path() -> None:
    """Was a pinned gap; the gap is closed, so this is now the requirement.

    `ACTION_VERBS` is entirely latin words, so a Chinese customer asking for a
    ticket used to route to `knowledge_qa` and get an answer retrieved from the
    knowledge base instead of a handoff. The English equivalents always routed
    correctly, so the asymmetry was in the verb vocabulary and not in the write
    path - which is why the fix lives in `intent.py` and this file only asserts
    the outcome.

    The mechanism is a Chinese-specific detector, not new entries in
    `ACTION_VERBS`: `is_action_request` requires `rest[0] in _OBJECT_MARKERS`,
    `_looks_like_a_write` tokenises with `[a-z']+`, and `\\b` cannot match
    between two CJK characters - all three are inapplicable to Chinese by
    construction, so adding words to the list provably cannot fire. See
    `docs/research/chinese-intent-measurement.md`.
    """
    for question in ("帮我建一张工单", "请开一张工单", "把这张单转给人工"):
        detection = classify(question)
        assert detection.route.value in {"business_write", "human_required"}, question


def test_case_create_is_reachable_through_the_tool_gateway_not_the_agent() -> None:
    """Registered as a confirmed write, and proposable by a human.

    The counterpart to the test above: not offered to the smart agent does not
    mean not shipped. It is in the platform catalog (so a `support_agent` can
    propose it through `POST /v1/tool-proposals`) and it resolves without a
    connector (so the HTTP surface can execute it).
    """
    from platform_core.tool_gateway.registry import PLATFORM_TOOLS, TOOL_CATALOG

    assert "case.create" in TOOL_CATALOG
    assert "case.create" in PLATFORM_TOOLS
    risk, schema, permissions, requires_confirmation = TOOL_CATALOG["case.create"]
    assert (risk, permissions, requires_confirmation) == (
        "confirmed_write",
        ["tool.write.confirmed"],
        True,
    )
    # The account is required, the tenant is not an argument.
    assert "enterprise_account_id" in schema["required"]
    assert "tenant_id" not in schema["properties"]


def test_write_arguments_merge_the_configured_half() -> None:
    assert _extract_write_args(
        "jira.create_issue", "  Please escalate   this defect ", {"project": "HQ"}
    ) == {"project": "HQ", "summary": "Please escalate this defect"}
    assert _extract_write_args("linear.create_issue", "bug", {"team": "ENG"}) == {
        "team": "ENG",
        "title": "bug",
    }
    assert _extract_write_args(
        "im.send_notification", "notify the on-call engineer", {"channel": "#alerts"}
    ) == {"channel": "#alerts", "text": "notify the on-call engineer"}
    # Whitespace only is not a subject, and a proposal with an empty summary
    # is one a human has to write from scratch.
    assert _extract_write_args("jira.create_issue", "   ", {"project": "HQ"}) is None


def test_a_long_subject_is_truncated_to_a_usable_summary() -> None:
    long_question = "Please escalate " + ("very long detail " * 40)
    args = _extract_write_args("jira.create_issue", long_question, {"project": "HQ"})
    assert args is not None
    assert len(args["summary"]) == 200


class _Scalars:
    def __init__(self, rows: list[Connector]) -> None:
        self._rows = rows

    def scalars(self) -> _Scalars:
        return self

    def all(self) -> list[Connector]:
        return self._rows


class _ConnectorSession:
    """Stand-in that honours the ACTIVE filter the resolver applies in SQL."""

    def __init__(self, rows: list[Connector]) -> None:
        self._rows = rows

    async def execute(self, stmt: Any) -> _Scalars:
        return _Scalars([r for r in self._rows if r.status == ConnectorStatus.ACTIVE.value])


def _connector(provider: str, capabilities: list[str], configuration: dict[str, Any]) -> Connector:
    connector = Connector(
        tenant_id=TENANT,
        provider=provider,
        name=f"{provider}-primary",
        status=ConnectorStatus.ACTIVE.value,
        capabilities=capabilities,
        configuration=configuration,
        credential_ref=f"vault://kv/{provider}",
    )
    # Normally server-generated; assigned so the resolver can read `.id`.
    connector.id = TENANT  # type: ignore[assignment]
    return connector


def _resolver(rows: list[Connector]) -> ConnectorExecutorResolver:
    return ConnectorExecutorResolver(_ConnectorSession(rows), tenant_id=TENANT)  # type: ignore[arg-type]


async def test_default_write_arguments_reads_the_connector_configuration() -> None:
    resolver = _resolver([_connector("jira", ["create_issue"], {"default_project": "HQ"})])
    assert await resolver.default_write_arguments("jira.create_issue") == {"project": "HQ"}


async def test_a_missing_configured_default_yields_no_arguments() -> None:
    """None, not an empty dict.

    The caller must hand off rather than propose a write with a missing
    required argument — the gateway would reject it as TOOL_ARGS_INVALID, but
    only after the proposal row existed, leaving an operator to explain a
    proposal that could never have run.
    """
    resolver = _resolver([_connector("jira", ["create_issue"], {})])
    assert await resolver.default_write_arguments("jira.create_issue") is None


async def test_a_read_only_connector_supplies_no_write_arguments() -> None:
    """The capability check applies here too.

    Otherwise a lookup-only Jira would hand the agent the project key for a
    write it cannot perform, and the failure would surface at execute time
    instead of at the point where handing off was still cheap.
    """
    resolver = _resolver([_connector("jira", ["read_issue"], {"default_project": "HQ"})])
    assert await resolver.default_write_arguments("jira.create_issue") is None


async def test_a_tool_with_no_configured_arguments_needs_no_connector() -> None:
    """An empty mapping, not None: the tool needs nothing from configuration,
    so a tenant with no connector at all is not a reason to hand off here.
    Whether it can *execute* is the executor resolver's question, not this
    one's."""
    resolver = _resolver([])
    assert await resolver.default_write_arguments("crm.update_account") == {}


@pytest.mark.parametrize("tool_name", sorted(WRITE_ARG_DEFAULTS))
def test_write_defaults_name_real_schema_arguments(tool_name: str) -> None:
    """Every configured argument must be one the tool's schema declares.

    A typo here would be invisible: the default would be merged into the
    arguments, the schema would reject the unknown property, and the failure
    would look like a customer-supplied argument problem.
    """
    from platform_core.tool_gateway.registry import TOOL_CATALOG

    schema = TOOL_CATALOG[tool_name][1]
    declared = set(schema.get("properties", {}))
    for argument, _ in WRITE_ARG_DEFAULTS[tool_name]:
        assert argument in declared, f"{tool_name} declares no {argument!r}"
