"""Feature list 1.3: media a customer sends over a channel is kept.

What this does and, more importantly, what it refuses to do:

- The bytes are stored as a **case attachment reference** - the same path an
  agent uses to file a photograph of a defective board. Nothing here parses
  the content, describes it, or embeds it. The AI never sees the media; it
  sees the *type* ("the customer sent an image"), which is the fact it can act
  on and the only one it needs.
- Every refusal is returned as a reason rather than swallowed, so "we did not
  keep your file" is explainable in the audit log instead of silent.

The minimiser deliberately strips media from the payload before an AI run, and
that stays true: this module runs *alongside* the run, not inside it. Keeping
a customer's photograph is not the same as letting a model read it, and
conflating the two is how a data-minimisation design quietly becomes a data
ingestion one.

Fetching is injected (`fetch`) rather than imported, because the only honest
test of "an unreadable attachment is refused, not dropped" is one that never
touches the network.
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol

from platform_core.cases.attachments import (
    MAX_ATTACHMENT_BYTES,
    AttachmentError,
    create_attachment,
    validate_attachment,
)

# How a channel announces media. Both shapes appear in the wild: a list under
# `attachments`, and single-message media as `media_url` + `media_type`.
_ATTACHMENT_KEYS = ("attachments", "media")


class _Fetcher(Protocol):
    def __call__(self, url: str) -> Awaitable[bytes]: ...


@dataclass(frozen=True)
class InboundMedia:
    """A reference to media on a channel - never the bytes themselves."""

    url: str
    content_type: str | None
    filename: str | None


def extract_inbound_media(payload: dict[str, Any]) -> list[InboundMedia]:
    """Media references in a channel payload, or [] when there is none.

    Returns an empty list rather than raising, because a message with no
    attachments is the common case and is not an error.
    """
    found: list[InboundMedia] = []
    raw_items: list[Any] = []
    for key in _ATTACHMENT_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            raw_items.extend(value)
        elif isinstance(value, dict):
            raw_items.append(value)
    if not raw_items:
        url = payload.get("media_url")
        if isinstance(url, str) and url:
            content_type = payload.get("media_type")
            found.append(
                InboundMedia(
                    url=url,
                    content_type=content_type if isinstance(content_type, str) else None,
                    filename=None,
                )
            )
        return found

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        url = item.get("data_url") or item.get("url") or item.get("file_url")
        if not isinstance(url, str) or not url:
            continue
        content_type = item.get("content_type") or item.get("file_type")
        name = item.get("filename") or item.get("file_name")
        found.append(
            InboundMedia(
                url=url,
                content_type=content_type if isinstance(content_type, str) else None,
                filename=name if isinstance(name, str) else None,
            )
        )
    return found


async def ingest_inbound_media(
    session: Any,
    *,
    tenant_id: Any,
    case_id: Any,
    media: list[InboundMedia],
    fetch: _Fetcher,
    storage: Any,
    uploaded_by: str = "customer",
) -> tuple[list[Any], list[str]]:
    """Fetch, validate and store each item. Returns (stored, refusals).

    Continues past a refused item on purpose: one oversized photograph should
    not cost the customer the two PDFs they sent with it.
    """
    stored: list[Any] = []
    refusals: list[str] = []
    for item in media:
        try:
            data = await fetch(item.url)
        except Exception as exc:  # noqa: BLE001 - a channel fetch failing is data
            refusals.append(f"{item.url}: fetch failed ({type(exc).__name__})")
            continue
        if len(data) > MAX_ATTACHMENT_BYTES:
            refusals.append(f"{item.url}: exceeds {MAX_ATTACHMENT_BYTES} bytes")
            continue
        try:
            content_type = validate_attachment(content_type=item.content_type, data=data)
        except AttachmentError as exc:
            refusals.append(f"{item.url}: {exc.code}")
            continue
        try:
            row = await create_attachment(
                session,
                tenant_id=tenant_id,
                case_id=case_id,
                filename=item.filename or "attachment",
                content_type=content_type,
                data=data,
                uploaded_by=uploaded_by,
                storage=storage,
            )
        except AttachmentError as exc:
            refusals.append(f"{item.url}: {exc.code}")
            continue
        stored.append(row)
    return stored, refusals


__all__ = [
    "InboundMedia",
    "extract_inbound_media",
    "ingest_inbound_media",
]
