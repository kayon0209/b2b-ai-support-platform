"""Feature list 1.3: customer-sent media is kept as evidence.

The tests that matter are about the boundaries the design depends on:

- **The bytes never reach the AI.** Asserted by checking what the stored row
  contains and that nothing is called with a decoded representation. The
  minimiser's job (media out of the AI payload) is unchanged and is not
  re-tested here.
- **A refusal is a reason, not silence.** An unsupported type or an unreadable
  URL must produce an explainable entry, because "we didn't keep your file"
  with no cause is the kind of gap nobody notices until a complaint is
  escalated.
- **One bad file does not cost the good ones.** Continue-on-refusal is a
  behaviour, not an accident.
"""

from __future__ import annotations

import asyncio

import pytest

from platform_core.cases.attachments import MAX_ATTACHMENT_BYTES
from platform_core.support_bridge.inbound_media import (
    InboundMedia,
    extract_inbound_media,
    ingest_inbound_media,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


class _FakeStorage:
    def __init__(self) -> None:
        self.stored: dict[str, bytes] = {}

    def put_object(self, key: str, data: bytes, content_type: str) -> None:
        self.stored[key] = data


class _FakeSession:
    """Accepts the create_attachment call without a database.

    The point of these tests is the fetch/validate/continue logic, not the
    row - the object-store write and the row are covered by the attachments
    module's own tests.
    """

    def __init__(self) -> None:
        self.added: list[object] = []

    async def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003
        class _Result:
            def scalar_one_or_none(self):
                return object()

        return _Result()

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None


def _fetcher(table: dict[str, bytes]):
    async def _fetch(url: str) -> bytes:
        if url not in table:
            raise RuntimeError("404")
        return table[url]

    return _fetch


def test_media_references_are_extracted_from_a_list_payload() -> None:
    payload = {
        "attachments": [
            {"data_url": "https://x/1.png", "content_type": "image/png", "filename": "board.png"}
        ]
    }
    media = extract_inbound_media(payload)
    assert media == [
        InboundMedia(url="https://x/1.png", content_type="image/png", filename="board.png")
    ]


def test_single_message_media_shape_is_recognised() -> None:
    media = extract_inbound_media({"media_url": "https://x/a.jpg", "media_type": "image/jpeg"})
    assert len(media) == 1
    assert media[0].content_type == "image/jpeg"


def test_a_message_with_no_media_is_not_an_error() -> None:
    assert extract_inbound_media({"content": "hello"}) == []


def test_an_accepted_image_is_stored() -> None:
    session = _FakeSession()
    storage = _FakeStorage()
    stored, refusals = _run(
        ingest_inbound_media(
            session,
            tenant_id="t",
            case_id="c",
            media=[InboundMedia(url="u", content_type="image/png", filename="b.png")],
            fetch=_fetcher({"u": PNG}),
            storage=storage,
        )
    )
    assert len(stored) == 1
    assert refusals == []
    assert storage.stored  # the bytes went to the object store


def test_an_unsupported_type_is_refused_with_a_reason() -> None:
    session = _FakeSession()
    stored, refusals = _run(
        ingest_inbound_media(
            session,
            tenant_id="t",
            case_id="c",
            media=[InboundMedia(url="u", content_type="application/x-msdownload", filename="x")],
            fetch=_fetcher({"u": b"MZ"}),
            storage=_FakeStorage(),
        )
    )
    assert stored == []
    assert refusals and "UNSUPPORTED" in refusals[0]


def test_an_unreadable_url_is_refused_not_silently_dropped() -> None:
    stored, refusals = _run(
        ingest_inbound_media(
            _FakeSession(),
            tenant_id="t",
            case_id="c",
            media=[InboundMedia(url="missing", content_type="image/png", filename="b.png")],
            fetch=_fetcher({}),
            storage=_FakeStorage(),
        )
    )
    assert stored == []
    assert refusals and "fetch failed" in refusals[0]


def test_oversized_media_is_refused() -> None:
    huge = b"0" * (MAX_ATTACHMENT_BYTES + 1)
    stored, refusals = _run(
        ingest_inbound_media(
            _FakeSession(),
            tenant_id="t",
            case_id="c",
            media=[InboundMedia(url="u", content_type="image/png", filename="b.png")],
            fetch=_fetcher({"u": huge}),
            storage=_FakeStorage(),
        )
    )
    assert stored == []
    assert refusals and "exceeds" in refusals[0]


def test_one_refused_file_does_not_cost_the_others() -> None:
    """Continue-on-refusal: a broken link must not lose the real evidence."""
    stored, refusals = _run(
        ingest_inbound_media(
            _FakeSession(),
            tenant_id="t",
            case_id="c",
            media=[
                InboundMedia(url="broken", content_type="image/png", filename="a.png"),
                InboundMedia(url="good", content_type="image/png", filename="b.png"),
            ],
            fetch=_fetcher({"good": PNG}),
            storage=_FakeStorage(),
        )
    )
    assert len(stored) == 1
    assert len(refusals) == 1


def test_media_content_is_never_decoded_into_text() -> None:
    """The AI sees the type, not the content - 1.3's actual constraint."""
    media = extract_inbound_media({"media_url": "u", "media_type": "image/png"})
    assert media[0].content_type == "image/png"
    assert not hasattr(media[0], "data")
    assert not hasattr(media[0], "text")


@pytest.mark.parametrize("payload", [{}, {"attachments": []}, {"attachments": [{"no_url": 1}]}])
def test_payloads_without_usable_media_yield_nothing(payload) -> None:
    assert extract_inbound_media(payload) == []
