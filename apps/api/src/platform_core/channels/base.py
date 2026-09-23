"""The inbound channel adapter contract (ADR 0013).

An adapter does two things and nothing else: decide whether a delivery is
authentic, and translate it into the fields the pipeline already consumes. It
does not resolve tenants, open sessions, persist rows or queue work - those are
the router's job, and keeping them out of the adapter is what stops every new
channel from growing its own copy of them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol


class ChannelVerificationError(Exception):
    """The delivery is not provably from the channel. Ingest must stop.

    Deliberately the only exception an adapter raises for a bad request: the
    router turns it into one response, so a channel cannot accidentally invent a
    distinguishable error that leaks whether a signature or a payload was wrong.
    """


def header(request: ChannelRequest, name: str) -> str | None:
    """Case-insensitive header lookup, and the reason it is not optional.

    HTTP header names are case-insensitive, but frameworks disagree about the
    casing they hand you: httpx (and therefore Starlette's TestClient) lowercases
    them, while Starlette preserves what arrived. Reading a plain dict by the
    canonical name therefore *works in a hand-built unit test and fails against
    every real client* - which is exactly how this was found: the adapter passed
    in isolation and 401'd the moment a real request carried
    `x-webhook-signature`.
    """
    wanted = name.lower()
    for key, value in request.headers.items():
        if key.lower() == wanted:
            return value
    return None


@dataclass(frozen=True)
class ChannelRequest:
    """The parts of an HTTP request an adapter may look at.

    Narrower than the framework's request on purpose. An adapter that can reach
    the session or the app state can bypass the router's decisions, and a value
    is far easier to test than a live request.
    """

    method: str
    headers: Mapping[str, str]
    query: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class InboundMessage:
    """What a channel must produce, and nothing more.

    These are exactly the fields `inbox_consumer` reads, so this dataclass *is*
    the contract between a channel and the pipeline.

    `text` is deliberately not part of the minimised payload stored on the inbox
    row. It goes to `conversation_turns`, which redacts before storage and is
    governed by retention; the consumer finds it again by turn id, through the
    same path a question typed into `/support` already uses. Putting it in the
    inbox row instead would repeal the minimisation policy for a shorter code
    path, which is not a trade this repository makes.
    """

    #: Stable per-conversation channel key. Feeds `conversation_ref_for`, so it
    #: must not change between two messages of the same conversation - a
    #: changing key silently splits one conversation into two.
    conversation_key: str
    #: The channel's own message id. Doubles as the delivery id, so it must be
    #: stable across a provider retry or one question becomes several.
    message_id: str
    #: The stable "this customer" handle, when the channel has one.
    contact_id: str | None
    #: The body. Redacted on the way into storage, never stored raw.
    text: str
    #: Content types only - never URLs, filenames or bytes (docs/security.md).
    attachment_types: list[str] = field(default_factory=list)


class ChannelAdapter(Protocol):
    """What the router needs from a channel.

    `system` must equal the `connectors.provider` of the row that selects it.
    """

    system: str

    def verify(self, *, secret: bytes, request: ChannelRequest) -> None:
        """Raise `ChannelVerificationError` unless the delivery is authentic."""
        ...

    def translate(self, *, request: ChannelRequest) -> InboundMessage | None:
        """Turn a verified delivery into a message, or None if it is not one.

        None is a normal outcome rather than a failure: WeChat delivers
        subscribes, unsubscribes and images down the same endpoint, and none of
        them is a question. Persisting those as `message_created` would queue a
        run with nothing to answer.
        """
        ...

    def challenge(self, *, secret: bytes, request: ChannelRequest) -> str | None:
        """Answer a channel's endpoint-verification handshake, if it has one.

        WeChat proves ownership of the server URL by GETting it with a signature
        and an `echostr` that must be echoed back verbatim. A channel without a
        handshake returns None, and the router then treats GET as unsupported.
        """
        ...

    def acknowledgement(self, *, request: ChannelRequest, content: str) -> tuple[bytes, str] | None:
        """The channel's own reply to an accepted delivery, or None for JSON.

        Returns `(body, media_type)`. This exists because the response contract
        is part of the channel, not the pipeline: WeChat expects XML inside five
        seconds and cannot be answered with a JSON `202`, while email and a
        generic JSON provider need nothing beyond a status code. Letting each
        adapter own its own reply is what keeps the router free of channel
        conditionals.
        """
        ...
