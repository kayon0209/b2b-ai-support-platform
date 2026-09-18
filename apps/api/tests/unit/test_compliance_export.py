"""Unit tests: the export window, and the rule that it widens nothing.

The window rules exist to stop a single request from reading everything a tenant
has ever written, and the policy invariant exists because an export endpoint is
the classic way for a role to acquire access it never had. Neither is observable
from a response body, so both are asserted here.
"""

from __future__ import annotations

import pytest

from platform_core.audit.service import MAX_METADATA_BYTES, minimise_metadata
from platform_core.compliance.service import (
    DEFAULT_SECTIONS,
    MAX_WINDOW_SECONDS,
    ComplianceError,
    parse_request,
)
from platform_policy import Action
from platform_policy.engine import RBAC_TABLE

NOW = 1_800_000_000


def test_the_window_start_is_required() -> None:
    """No default: how far back an extract reaches is the caller's decision, and
    a default would be a policy choice the platform made on their behalf."""
    with pytest.raises(ComplianceError) as exc:
        parse_request(since=None, until=NOW, sections=None, now=NOW)
    assert exc.value.code == "EXPORT_SINCE_REQUIRED"


def test_the_window_end_defaults_to_now() -> None:
    request = parse_request(since=NOW - 100, until=None, sections=None, now=NOW)
    assert request.until == NOW


def test_an_inverted_window_is_refused() -> None:
    with pytest.raises(ComplianceError) as exc:
        parse_request(since=NOW, until=NOW - 1, sections=None, now=NOW)
    assert exc.value.code == "EXPORT_WINDOW_INVERTED"


def test_a_window_wider_than_the_limit_is_refused() -> None:
    with pytest.raises(ComplianceError) as exc:
        parse_request(since=NOW - MAX_WINDOW_SECONDS - 1, until=NOW, sections=None, now=NOW)
    assert exc.value.code == "EXPORT_WINDOW_TOO_WIDE"


def test_the_limit_itself_is_allowed() -> None:
    """The boundary is inclusive, so a year-long request does not fail by one
    second and send the caller hunting for a rounding rule."""
    request = parse_request(since=NOW - MAX_WINDOW_SECONDS, until=NOW, sections=None, now=NOW)
    assert request.window_seconds == MAX_WINDOW_SECONDS


def test_sections_default_to_audit_and_cases() -> None:
    request = parse_request(since=NOW - 100, until=NOW, sections=None, now=NOW)
    assert request.sections == DEFAULT_SECTIONS


def test_an_unknown_section_is_refused_rather_than_ignored() -> None:
    """Silently ignoring it would return a document missing the part the caller
    asked for, and they would have no way to know."""
    with pytest.raises(ComplianceError) as exc:
        parse_request(since=NOW - 100, until=NOW, sections=["audit", "billing"], now=NOW)
    assert exc.value.code == "EXPORT_UNKNOWN_SECTION"
    assert "billing" in str(exc.value)


def test_an_explicit_empty_section_list_is_refused() -> None:
    with pytest.raises(ComplianceError) as exc:
        parse_request(since=NOW - 100, until=NOW, sections=[], now=NOW)
    assert exc.value.code == "EXPORT_NO_SECTIONS"


# --- the access invariant ---------------------------------------------------


def test_export_is_only_held_where_it_grants_nothing_new() -> None:
    """The export returns audit events and Case content together. A role must
    already be able to read both before it can take a copy out, so the endpoint
    can never be how a role acquires reach it did not have."""
    for role, actions in RBAC_TABLE.items():
        if Action.COMPLIANCE_EXPORT not in actions:
            continue
        assert Action.AUDIT_READ in actions, role
        assert Action.CASE_READ in actions, role


def test_a_support_agent_cannot_export() -> None:
    """No AUDIT_READ, so no export - the invariant above, checked at the one
    role whose absence is the point."""
    assert Action.COMPLIANCE_EXPORT not in RBAC_TABLE["support_agent"]


def test_the_auditor_and_the_owner_can_export() -> None:
    """An auditor's purpose is to take a copy out for review; the owner is the
    party a data-protection request is addressed to."""
    assert Action.COMPLIANCE_EXPORT in RBAC_TABLE["auditor"]
    assert Action.COMPLIANCE_EXPORT in RBAC_TABLE["tenant_owner"]


# --- audit metadata: parameters yes, payloads no ----------------------------


def test_metadata_is_empty_when_none_is_supplied() -> None:
    """Every pre-existing caller passes no metadata, so their rows must be
    byte-identical to what they were before the parameter existed."""
    assert minimise_metadata(None) == {}
    assert minimise_metadata({}) == {}


def test_metadata_redacts_by_key() -> None:
    """The same redaction the logs use: a credential that reaches an audit row
    has to be masked by key, not by the caller remembering not to pass it."""
    redacted = minimise_metadata({"api_token": "lin_api_key_123", "window": 60})
    assert redacted["window"] == 60
    assert "lin_api_key_123" not in str(redacted)


def test_oversized_metadata_is_refused_rather_than_truncated() -> None:
    """A cap that silently drops fields produces an audit row that looks
    complete and is not - which is worse than refusing the write."""
    with pytest.raises(ValueError) as exc:
        minimise_metadata({"blob": "x" * (MAX_METADATA_BYTES + 1)})
    assert "before/after" in str(exc.value)


def test_realistic_export_metadata_fits() -> None:
    """The cap is generous for what it is for, so a legitimate caller never
    meets it."""
    metadata = {
        "since": NOW - 86_400,
        "until": NOW,
        "sections": ["audit", "cases"],
        "row_counts": {"audit": 4_812, "cases": 963, "case_escalations": 12},
        "truncated": {"audit": False, "cases": False},
    }
    assert minimise_metadata(metadata) == metadata
