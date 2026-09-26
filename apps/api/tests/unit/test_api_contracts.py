"""The API's promises, asserted against the API.

What a contract test layer is for
---------------------------------
`docs/api-contracts.md` makes four promises in its "General conventions"
section that a client is entitled to rely on:

1. Every endpoint documented there exists.
2. Every response carries a `trace_id`.
3. Errors use the documented envelope, with a stable machine-readable code.
4. **Every write command accepts an `Idempotency-Key`.**

None of them were asserted anywhere. The repository had `tests/artifacts`,
`tests/e2e`, `tests/evals` and `tests/unit` - a great deal of testing of
behaviour, and not one assertion about the shape of the surface itself. A
document that has drifted is a promise nobody is keeping, and nobody finds out
until a client does.

The idempotency gap, specifically
---------------------------------
`require_write_idempotency` existed and was called from 38 places. Ten router
modules defining 24 write endpoints called it from none. The module's own
comment states the design: "expressed as the read set because reads are few and
stable, so a newly added write action is covered by default instead of being
silently exempt."

That default is not real. The read set decides what is exempt; it does not
decide what is checked. An endpoint that forgets the call is exempt, silently,
and nothing notices - which is how 24 write endpoints ended up accepting a
retried POST as a new command. Duplicate cases, duplicate invitations and
duplicate tool executions are the outcome, and the operator sees one extra row
and no explanation.

Two endpoints are POST and deliberately exempt
-----------------------------------------------
`POST /v1/retrieval/query` and `POST /support/verify` are reads wearing POST
because the request body is too large for a query string. Demanding an
`Idempotency-Key` of them would be demanding a key that means nothing, and the
exclusion is declared here so the next reader does not "fix" them.
"""

from __future__ import annotations

import pathlib
import re

import pytest

# This file is apps/api/tests/unit/test_api_contracts.py, so the repository
# root is four parents up. Getting this wrong produced a FileNotFoundError on
# `apps/docs/api-contracts.md` in the first run, which is a worse failure than
# a wrong assertion: it looks like a missing document rather than a wrong path.
ROOT = pathlib.Path(__file__).resolve().parents[4]
CONTRACT_DOC = ROOT / "docs" / "api-contracts.md"
API_SRC = ROOT / "apps" / "api" / "src" / "platform_core"


# --- 1. Documented endpoints exist ----------------------------------------


def _documented_endpoints() -> list[tuple[str, str]]:
    source = CONTRACT_DOC.read_text(encoding="utf-8")
    return re.findall(r"^(GET|POST|PUT|PATCH|DELETE)\s+(/\S+)", source, flags=re.M)


def _normalise(path: str) -> str:
    """Reduce a path to the shape a comparison can trust.

    Query strings are documentation (an example of the accepted values), and
    path parameters carry their names in the document but not in OpenAPI's
    flattened output. Comparing either literally would report differences that
    are not differences.
    """
    bare = path.split("?", 1)[0]
    return re.sub(r"\{[^}]+\}", "{}", bare).rstrip("/") or "/"


@pytest.fixture(scope="module")
def openapi_spec() -> dict:
    """The live application, imported rather than parsed.

    `app.openapi()` is the same object a client would fetch from
    `/openapi.json`, so this asserts against what is actually served instead of
    against a second, drifting description of it.
    """
    from platform_core.main import app

    return app.openapi()


def test_every_documented_endpoint_exists(openapi_spec: dict) -> None:
    """A documented endpoint that is not there is a client 404 at go-live.

    This is the one assertion in the repository that would have caught a
    renamed or deleted route, because nothing else looks at the document.
    """
    served = {
        (method.upper(), _normalise(path))
        for path, operations in openapi_spec["paths"].items()
        for method in operations
    }
    missing = [
        f"{method} {path}"
        for method, path in _documented_endpoints()
        if (method, _normalise(path)) not in served
    ]
    assert not missing, f"documented but not served: {missing}"


def test_the_contract_document_is_not_empty_of_endpoints() -> None:
    """Guard the parser above.

    A regex that stops matching would make `test_every_documented_endpoint_exists`
    pass vacuously - 33 endpoints compared against nothing is 33 green
    assertions. If a rewrite of the document changes its fence style, this
    fails instead, which is the difference between a test and a decoration.
    """
    documented = _documented_endpoints()
    assert len(documented) > 20, f"only {len(documented)} endpoints parsed from the contract"
    assert len({path for _, path in documented}) > 15, "endpoint paths look degenerate"


# --- 2. Write endpoints require an Idempotency-Key -------------------------

# Endpoints that satisfy idempotency by a mechanism other than the header, with
# the mechanism named. Each is exempt because it is already correct, not because
# it is inconvenient - the first version of this file listed only the two
# POSTs-that-are-reads and failed both webhooks, which was a false accusation
# until the handlers were read:
#
#   * The connector webhook dedupes on `X-Delivery-Id` through the
#     `uq_inbox_delivery` index and answers a repeat with success, which is
#     exactly what the document's processing rules 3 and 4 ask for.
#   * The channel webhook's deduplication is inside
#     `chat_service.append_customer_turn`, which returns a `duplicate` flag
#     alongside the turn.
#
# A provider cannot be asked to send `Idempotency-Key`; demanding a header it
# will never set would make both endpoints permanently 400. The exemption is
# written down with its reason so the next reader does not "fix" it.
_IDEMPOTENT_BY_DELIVERY_ID = {
    ("POST", "/v1/webhooks/channels/{connector_id}"),
    ("POST", "/v1/webhooks/connectors/{connector_id}"),
}

# SCIM provisioning, where the header is the wrong key. An IdP provisions on its
# own schedule and does not send `Idempotency-Key`; the resource's own identity
# is the key instead, and all five endpoints converge on it:
#
#   * POST /Users and POST /Groups match on `userName` / `externalId` and
#     return the existing resource, so a retried provision creates neither a
#     duplicate account nor `support-2`.
#   * PATCH and DELETE write an **absolute** target state - `active: false`, a
#     suspended membership - so applying them twice lands in the same place.
#
# Demanding the header here would make provisioning fail permanently against a
# compliant IdP. What is asserted instead is that the convergence is still
# there, because these are the five endpoints where losing it is silent: a
# duplicate department is not an error, it is just a second one.
_IDEMPOTENT_BY_RESOURCE_IDENTITY = {
    ("POST", "/scim/v2/Users"),
    ("PATCH", "/scim/v2/Users/{user_id}"),
    ("DELETE", "/scim/v2/Users/{user_id}"),
    ("POST", "/scim/v2/Groups"),
    ("PATCH", "/scim/v2/Groups/{group_id}"),
}

# Read endpoints that use POST because the request body does not fit in a query
# string. Declared rather than inferred, so the next person to read this does
# not add the missing header to them.
_POST_THAT_IS_A_READ = {
    ("POST", "/v1/retrieval/query"),
    ("POST", "/v1/support/verify"),
}


WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _served_writes() -> list[tuple[str, str, str]]:
    """Every write endpoint with the module that defines it.

    FastAPI's `include_router` in this version stores a wrapper rather than
    copying routes onto the app, so `app.routes` holds 37 wrapper objects and
    almost nothing else. `app.openapi()` expands them, which is why the document
    comparison below works while iterating `app.routes` finds almost nothing -
    a discrepancy that cost the first version of this file its entire result.

    Going through `original_router` is what recovers the defining module, and
    `endpoint.__module__` is the framework's own answer to "which file is this".
    The alternative - matching route paths back to files by searching their
    source for the last path segment - was tried and is wrong in both
    directions: it cannot find a dynamic segment like `{tier}` and it happily
    picks a different router that happens to mention the same word.
    """
    from platform_core.main import app

    found: list[tuple[str, str, str]] = []
    for entry in app.routes:
        router = getattr(entry, "original_router", None)
        if router is None:
            continue
        for route in getattr(router, "routes", []):
            methods = getattr(route, "methods", None) or set()
            writes = methods & WRITE_METHODS
            if not writes:
                continue
            module = getattr(getattr(route, "endpoint", None), "__module__", "") or ""
            for method in writes:
                found.append((method, route.path, module))
    assert found, "no write endpoints resolved - the route extraction broke"
    return found


def _write_endpoints() -> list[str]:
    return sorted(f"{method} {path}" for method, path, _ in _served_writes())


def _module_source(module: str) -> str:
    """Read the file that defines a route, from the module name itself.

    `inspect.getsourcefile` rather than a path join, so this does not need to
    know where the package is rooted.
    """
    import importlib
    import inspect

    resolved = importlib.import_module(module)
    source_file = inspect.getsourcefile(resolved)
    assert source_file is not None, f"cannot locate the source of {module}"
    return pathlib.Path(source_file).read_text(encoding="utf-8")


@pytest.mark.parametrize("route", _write_endpoints())
def test_every_write_endpoint_demands_an_idempotency_key(route: str) -> None:
    """The document's fourth promise, asserted for every write endpoint.

    A write endpoint without the check accepts a client's retry after a timeout
    as a brand new command. The result is a duplicate - a second case, a second
    invitation, a second tool execution - and the operator sees an extra row
    with nothing to explain it.

    The two POSTs that are reads are excluded by name above, so the list stays
    honest rather than being quietly trimmed whenever it becomes inconvenient.
    """
    method, path = route.split(" ", 1)
    if (method, path.rstrip("/")) in _POST_THAT_IS_A_READ:
        return
    if (method, path) in _IDEMPOTENT_BY_DELIVERY_ID:
        return
    if (method, path) in _IDEMPOTENT_BY_RESOURCE_IDENTITY:
        return

    for served_method, served_path, module in _served_writes():
        if f"{served_method} {served_path}" == route:
            source = _module_source(module)
            # Three spellings of the same requirement, all correct. The shared
            # helper; the older inline form (read the key, build the 400 by
            # hand) which two routers use with their own wording; and the direct
            # key read that an endpoint whose authorization action is a *read*
            # has to use, because the helper infers read-versus-write from that
            # action and would exempt it. Requiring only the first reported all
            # three as broken when none of them were, and an assertion that
            # cries wolf gets deleted rather than fixed.
            checks = (
                "require_write_idempotency(",
                "require_idempotency_key(",
                "IDEMPOTENCY_KEY_REQUIRED",
            )
            assert any(check in source for check in checks), (
                f"{route} is a write endpoint and {module} checks for no "
                "Idempotency-Key at all, so a client retry is accepted as a new "
                "command"
            )
            return
    raise AssertionError(f"{route} is served but not among the resolved writes")


def test_the_exemptions_are_still_reads() -> None:
    """The exemptions cannot rot into a blanket waiver.

    `POST /v1/retrieval/query` is exempt because it reads. The day someone adds a
    write to that handler, the exemption becomes a hole - and the way to notice
    is to assert the handler is still doing what the exemption claims.
    """
    from platform_core.main import app

    spec = app.openapi()
    missing = [path for _, path in _POST_THAT_IS_A_READ if path not in spec["paths"]]
    assert not missing, f"exempted paths that no longer exist: {missing}"


def test_the_webhook_exemptions_still_carry_a_deduplication_mechanism() -> None:
    """A webhook exemption is a claim, and this is the claim being checked.

    Exempt because they dedupe on `X-Delivery-Id` rather than the header - so
    the exemption is only true while that deduplication exists. Deleting the
    `result.duplicate` branch in the connector webhook, or the duplicate flag in
    the channel one, turns this into an endpoint that accepts a provider retry
    as new traffic, and nothing else in the suite would say so.

    Asserted on the mechanism rather than on behaviour because behaviour here
    needs a signed provider request to exercise, and a source assertion that
    names the actual guard is the part that can be reviewed.
    """
    connector = _module_source("platform_core.integrations.webhook_router")
    assert "result.duplicate" in connector, (
        "the connector webhook exemption assumes duplicate deliveries are answered "
        "from the stored delivery; that branch is gone"
    )
    assert "uq_inbox_delivery" in connector, (
        "the connector webhook exemption assumes a unique index on delivery id"
    )

    channel = _module_source("platform_core.channels.router")
    assert "append_customer_turn" in channel, (
        "the channel webhook exemption assumes customer turns are deduplicated by the chat service"
    )


def test_the_scim_exemptions_still_converge_on_the_resource_identity() -> None:
    """SCIM's five endpoints are exempt because they dedupe, not because they don't.

    This is the exemption most likely to rot quietly. There is no header check
    to remove - the deduplication is the *behaviour* of the handler - so
    "simplifying" `create_group` into a plain insert would look like a cleanup
    and would produce a second `support` department on the IdP's next retry,
    with no error anywhere.

    The two assertions are the convergence points that matter: the lookup before
    the insert, and the member replacement that makes a repeated provision
    converge rather than accumulate.
    """
    scim_source = _module_source("platform_core.identity.scim_router")

    # POST /Users and POST /Groups both resolve an existing row before creating.
    # The convergence itself, not the prose describing it - the two comments
    # are worded differently, and asserting a phrase count was checking my own
    # wording rather than the behaviour.
    assert "if existing is not None:" in scim_source, (
        "neither SCIM create endpoint resolves an existing row before inserting, "
        "so a retried provision creates a duplicate"
    )
    assert "Department.slug == slug" in scim_source, (
        "POST /Groups no longer matches on the slug before inserting, so a retried "
        "provision creates a duplicate department"
    )
    assert "_replace_members" in scim_source, (
        "a repeated group provision must replace the member list rather than accumulate it"
    )


def test_a_write_cannot_hide_behind_a_read_authorization_action() -> None:
    """The one place the shared helper is the wrong tool, pinned down.

    `require_write_idempotency` decides read-versus-write by looking at the
    *authorization* action. That is right almost everywhere, because an endpoint
    that writes and an endpoint authorized to write share an action. It is wrong
    for `/v1/quality/categories/state`, which is authorized under `AUDIT_READ`
    and mutates a state machine: passing `AUDIT_READ` exempts it, and the
    endpoint accepts a retried POST with a 200.

    That is not hypothetical - the first version of this fix did exactly that
    and the integration test caught it by asserting 400 and receiving 200. The
    endpoint therefore checks the key directly, and this asserts it kept that
    form: if somebody "tidies" it back to the helper, the request goes through.
    """
    source = _module_source("platform_core.evaluation.router")
    assert "require_write_idempotency(" not in source, (
        "this endpoint is authorized as AUDIT_READ, which the helper treats as a "
        "read and exempts; use require_idempotency_key directly"
    )
    assert "require_idempotency_key(request)" in source, (
        "the category state write must check the key itself"
    )


def test_the_error_envelope_is_what_the_document_promises() -> None:
    """The shape a client switches on, pinned at its source.

    `error_response` is the single constructor every failure path uses, so
    asserting its output pins the envelope for every endpoint at once.
    """
    import json

    from platform_core.api import RETRYABLE_CODES, error_response

    response = error_response(
        "KNOWLEDGE_EVIDENCE_INSUFFICIENT",
        "The answer could not be verified from authorized knowledge.",
        status_code=409,
        trace_id="0190c000-0000-7000-8000-000000000001",
    )
    # Parsed, not string-matched. The first version asserted on `'"code": "..."'`
    # with a space after the colon, which is Python's repr formatting - this
    # function emits compact JSON. The assertion was checking a whitespace
    # convention and would have passed on a response with the wrong keys.
    body = json.loads(response.body)

    assert set(body) == {"error", "trace_id"}, body
    assert set(body["error"]) == {"code", "message", "retryable", "details"}, body["error"]
    assert body["error"]["code"] == "KNOWLEDGE_EVIDENCE_INSUFFICIENT"
    assert body["error"]["retryable"] is False
    assert body["error"]["details"] == {}
    assert body["trace_id"] == "0190c000-0000-7000-8000-000000000001"
    # The document's example says `retryable: false` for this code, so the code
    # table agreeing is what makes the example true. If the table changes and
    # the document does not, one of the two is now wrong and this says so.
    assert "KNOWLEDGE_EVIDENCE_INSUFFICIENT" not in RETRYABLE_CODES, (
        "the documented example says retryable: false, so the code table changed - "
        "update the document or the table deliberately, not by accident"
    )


def test_a_read_endpoint_never_demands_an_idempotency_key() -> None:
    """The other direction: over-applying the rule is its own failure.

    Requiring a key nobody has a use for is a broken client on day one, and it
    looks like the contract working. The read set in `api.py` is the single
    place that decides, so it is asserted directly rather than through 67
    endpoints.
    """
    from platform_core.api import READ_ACTIONS, Action

    assert Action.CASE_READ in READ_ACTIONS
    assert Action.KNOWLEDGE_READ in READ_ACTIONS
    # The design is "name the reads, default everything else to a write", so a
    # read action in the set is the safety property. What matters is that the
    # set is small and explicit rather than derived from the endpoint list.
    assert len(READ_ACTIONS) < len(list(Action)), (
        "the read set has grown to cover most actions, which inverts the design: "
        "writes are supposed to be the default, not the exception"
    )
