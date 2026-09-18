"""Unit tests: the SCIM protocol layer.

All pure functions, and all of them are places where getting it subtly wrong
produces a plausible-looking answer rather than an error: a filter that matches
too much, a page offset off by one, a `remove` that removes nothing. So each has
a test rather than a comment.
"""

from __future__ import annotations

import uuid

import pytest

from platform_core.identity import scim

# --- filters ----------------------------------------------------------------


def test_no_filter_is_no_filter() -> None:
    assert scim.parse_filter(None) is None
    assert scim.parse_filter("") is None
    assert scim.parse_filter("   ") is None


def test_username_equality_parses() -> None:
    parsed = scim.parse_filter('userName eq "Ada@Example.com"')
    assert parsed == scim.ParsedFilter(attribute="username", value="Ada@Example.com")


def test_an_unsupported_operator_is_refused_by_name() -> None:
    """`co`/`sw`/`and` would need a real parser, and a hand-rolled one that gets
    an operator wrong is how a filter returns rows it should not."""
    with pytest.raises(scim.ScimError) as exc:
        scim.parse_filter('userName co "ada"')
    assert exc.value.scim_type == scim.SCIM_INVALID_FILTER


def test_an_unsupported_attribute_is_refused_with_the_supported_list() -> None:
    """The error names what *is* supported, so an IdP administrator can fix
    their configuration instead of watching the filter silently match
    everything."""
    with pytest.raises(scim.ScimError) as exc:
        scim.parse_filter('nickName eq "ada"')
    assert "username" in exc.value.detail


def test_an_empty_filter_value_is_refused() -> None:
    with pytest.raises(scim.ScimError):
        scim.parse_filter('userName eq ""')


# --- paging -----------------------------------------------------------------


def test_paging_normalises_scims_one_based_index() -> None:
    """SCIM counts from 1 and SQL counts from 0. The off-by-one is the classic
    "page two repeats page one" bug."""
    offset, limit, start = scim.paging(2, 10, 0)
    assert (offset, limit, start) == (1, 10, 2)


def test_paging_defaults_are_bounded() -> None:
    offset, limit, start = scim.paging(None, None, 0)
    assert (offset, start) == (0, 1)
    assert limit == scim.DEFAULT_COUNT


def test_paging_caps_an_oversized_count() -> None:
    """An IdP asking for 100,000 resources at once is a load test nobody
    scheduled."""
    _, limit, _ = scim.paging(1, 100_000, 0)
    assert limit == scim.MAX_RESULTS


def test_paging_refuses_a_zero_index() -> None:
    with pytest.raises(scim.ScimError):
        scim.paging(0, 10, 0)


def test_paging_refuses_a_negative_count() -> None:
    with pytest.raises(scim.ScimError):
        scim.paging(1, -1, 0)


# --- resources --------------------------------------------------------------


def test_user_resource_carries_the_schema_and_location() -> None:
    resource = scim.user_resource(
        user_id=uuid.UUID(int=1), email="ada@example.com", display_name="Ada", active=True
    )
    assert resource["schemas"] == [scim.SCIM_USER_SCHEMA]
    assert resource["userName"] == "ada@example.com"
    assert resource["meta"]["location"].endswith("/scim/v2/Users/" + str(uuid.UUID(int=1)))


def test_user_payload_requires_an_email() -> None:
    """`users.primary_email` is UNIQUE and NOT NULL, so a resource without one
    cannot be represented - and inventing an address would collide with a real
    one eventually."""
    with pytest.raises(scim.ScimError):
        scim.user_from_payload({"userName": "ada"})
    with pytest.raises(scim.ScimError):
        scim.user_from_payload({})


def test_an_absent_active_flag_means_active() -> None:
    """The spec's default for a new resource. Treating absent as inactive would
    de-provision every user whose IdP omits the field."""
    _, _, active = scim.user_from_payload({"userName": "ada@example.com"})
    assert active is True


def test_an_explicit_false_active_flag_is_honoured() -> None:
    _, _, active = scim.user_from_payload({"userName": "ada@example.com", "active": False})
    assert active is False


def test_group_slug_prefers_external_id() -> None:
    """`externalId` is the IdP's stable handle, so a rename of the group does
    not re-point the mapping."""
    name, slug = scim.group_from_payload(
        {"displayName": "Support EMEA", "externalId": "Support-EMEA"}
    )
    assert name == "Support EMEA"
    assert slug == "support-emea"


def test_group_slug_falls_back_to_the_display_name() -> None:
    _, slug = scim.group_from_payload({"displayName": "Support EMEA"})
    assert slug == "support-emea"


def test_a_group_with_no_usable_slug_is_refused() -> None:
    with pytest.raises(scim.ScimError):
        scim.group_from_payload({"displayName": "???"})


def test_a_malformed_member_id_is_refused_not_skipped() -> None:
    """Skipping one leaves a user in the group in the IdP and out of it here,
    which is exactly the drift SCIM exists to remove."""
    with pytest.raises(scim.ScimError):
        scim.member_ids({"members": [{"value": "not-a-uuid"}]})


def test_member_ids_accepts_both_shapes() -> None:
    one = uuid.uuid4()
    assert scim.member_ids({"members": [{"value": str(one)}]}) == [one]
    assert scim.member_ids({"members": [str(one)]}) == [one]


# --- PATCH ------------------------------------------------------------------


def test_a_replace_operation_sets_the_attribute() -> None:
    merged = scim.apply_patch(
        {"Operations": [{"op": "replace", "path": "active", "value": False}]},
        mutable=scim.MUTABLE_USER,
    )
    assert merged["active"] is False


def test_a_remove_operation_actually_removes() -> None:
    """This is the deprovisioning path. Treating PATCH as a JSON merge patch
    would make `remove` a no-op, and an account disabled in the IdP would stay
    active here."""
    merged = scim.apply_patch(
        {"Operations": [{"op": "remove", "path": "active"}], "active": True},
        mutable=scim.MUTABLE_USER,
    )
    assert "active" not in merged


def test_operations_is_never_carried_into_the_result() -> None:
    """It is a command, not an attribute."""
    merged = scim.apply_patch(
        {"Operations": [{"op": "replace", "path": "active", "value": True}]},
        mutable=scim.MUTABLE_USER,
    )
    assert "Operations" not in merged


def test_an_immutable_attribute_is_refused_rather_than_ignored() -> None:
    """A request that tries to change an id must fail loudly: silently ignoring
    it would leave the IdP believing it had changed something."""
    with pytest.raises(scim.ScimError) as exc:
        scim.apply_patch(
            {"Operations": [{"op": "replace", "path": "id", "value": "other"}]},
            mutable=scim.MUTABLE_USER,
        )
    assert exc.value.scim_type == scim.SCIM_MUTABILITY


def test_a_pathless_add_checks_every_named_attribute() -> None:
    with pytest.raises(scim.ScimError) as exc:
        scim.apply_patch(
            {
                "Operations": [
                    {"op": "add", "value": {"displayName": "Ada", "role": "tenant_owner"}}
                ]
            },
            mutable=scim.MUTABLE_USER,
        )
    assert exc.value.scim_type == scim.SCIM_MUTABILITY


def test_a_pathless_replace_merges_every_named_attribute() -> None:
    merged = scim.apply_patch(
        {"Operations": [{"op": "replace", "value": {"displayName": "Ada L"}}]},
        mutable=scim.MUTABLE_USER,
    )
    assert merged["displayName"] == "Ada L"


def test_a_sub_attribute_path_is_matched_on_its_first_segment() -> None:
    """`name.familyName` is not used by anything this endpoint owns; matching
    only the first segment means an unsupported sub-path is refused rather than
    silently ignored."""
    with pytest.raises(scim.ScimError):
        scim.apply_patch(
            {"Operations": [{"op": "replace", "path": "meta.familyName", "value": "x"}]},
            mutable=scim.MUTABLE_USER,
        )


def test_an_unknown_operation_is_refused() -> None:
    with pytest.raises(scim.ScimError):
        scim.apply_patch(
            {"Operations": [{"op": "delete", "path": "active"}]}, mutable=scim.MUTABLE_USER
        )


def test_an_empty_operations_list_is_refused() -> None:
    with pytest.raises(scim.ScimError):
        scim.apply_patch({"Operations": []}, mutable=scim.MUTABLE_USER)


def test_a_group_patch_cannot_touch_user_attributes() -> None:
    """The mutable sets are per resource type, so a Group PATCH cannot reach a
    User attribute through a path that happens to exist on both."""
    with pytest.raises(scim.ScimError):
        scim.apply_patch(
            {"Operations": [{"op": "replace", "path": "userName", "value": "x"}]},
            mutable=scim.MUTABLE_GROUP,
        )


# --- envelopes --------------------------------------------------------------


def test_list_response_is_a_list_response() -> None:
    body = scim.list_response([], total=7, start=1, limit=10)
    assert body["schemas"] == [scim.SCIM_LIST_SCHEMA]
    assert body["totalResults"] == 7
    assert body["itemsPerPage"] == 0


def test_error_response_carries_the_scim_type() -> None:
    body = scim.error_response("409", "slug taken", scim_type=scim.SCIM_UNIQUENESS)
    assert body["schemas"] == [scim.SCIM_ERROR_SCHEMA]
    assert body["scimType"] == scim.SCIM_UNIQUENESS
    # `status` is a string in the SCIM error schema, not a number.
    assert body["status"] == "409"
