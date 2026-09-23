"""Attachment metadata on inbound events (feature list 1.3).

This trade runs on board photos, Gerber archives and BOM spreadsheets, so a
platform that only knows about text cannot tell that evidence was supplied.
The extraction is deliberately narrow - content types only - and the tests are
mostly about what is *not* kept.
"""

from platform_core.support_bridge.minimize import minimize_inbound_payload


def _message(**extra: object) -> dict:
    payload = {"id": 1, "content": "板子短路了", "message_type": "incoming"}
    payload.update(extra)
    return payload


def test_attachment_types_are_recorded() -> None:
    out = minimize_inbound_payload(
        "message_created",
        _message(
            attachments=[
                {"file_type": "image/png"},
                {"file_type": "image/png"},  # duplicate
                {"file_type": "application/pdf"},
            ]
        ),
    )

    # De-duplicated: "the customer sent images and a PDF", not a list with
    # repeats that implies more files than there are.
    assert out["attachment_types"] == ["image/png", "application/pdf"]


def test_no_attachment_urls_or_filenames_are_kept() -> None:
    """The minimisation rule, and the IP rule.

    A Gerber or a board drawing is the customer's property. Storing a URL, a
    filename or the bytes themselves would put customer IP at rest in a table
    the platform does not need it in - the files stay in Chatwoot.
    """
    out = minimize_inbound_payload(
        "message_created",
        _message(
            attachments=[
                {
                    "file_type": "image/png",
                    "data_url": "https://files.example/x/abc",
                    "filename": "defective-board-rev2.png",
                }
            ]
        ),
    )

    assert out["attachment_types"] == ["image/png"]
    serialised = repr(out)
    assert "https://files.example" not in serialised
    assert "defective-board-rev2" not in serialised


def test_a_message_without_attachments_has_no_key() -> None:
    assert "attachment_types" not in minimize_inbound_payload("message_created", _message())
    assert "attachment_types" not in minimize_inbound_payload(
        "message_created", _message(attachments=[])
    )


def test_malformed_attachments_do_not_break_minimisation() -> None:
    """Outside input: an unusable attachment is skipped, never fatal."""
    out = minimize_inbound_payload(
        "message_created",
        _message(attachments=["not-a-dict", {}, {"file_type": 42}, {"file_type": "text/csv"}]),
    )

    assert out["attachment_types"] == ["text/csv"]
    assert out["message_id"] == "1"


def test_the_type_list_is_bounded() -> None:
    """One event row is metadata for routing, not a document store."""
    out = minimize_inbound_payload(
        "message_created",
        _message(attachments=[{"file_type": f"type/{i}"} for i in range(50)]),
    )

    assert len(out["attachment_types"]) == 5
    assert all(len(item) <= 63 for item in out["attachment_types"])
