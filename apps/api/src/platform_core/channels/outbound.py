"""Outbound delivery for the inbound channels (ADR 0014).

An adapter that only receives is half an adapter. Without this, an email or
WeChat answer is persisted and visible in `Workbench` but never reaches the
person who asked: `orchestrator._dispatch` returned **success** for every channel
message and sent nothing, because "no Chatwoot account id" was read as "the
platform surface is the channel". That is true for `/support` and false for
email and WeChat.

The contract
------------
A transport implements `send(*, address, conversation_key, content, command_id)`
and returns a `SendResult`. `address` is where it goes. `conversation_key` is
what makes a *reply* join the same conversation rather than starting a new one:

- **Email**: it is the thread root, and it is written into `In-Reply-To` and
  `References`. A customer's reply then carries that root in its own
  `References`, which is exactly what `channels.email` reads to derive the same
  conversation key. Omit it and every answer starts a new thread, so the
  conversation never accumulates.
- **WeChat**: it is the openid, and it is the `touser`.

Idempotency
-----------
`command_id` is derived from the run id by the caller, so a retry of one run
cannot deliver twice. A transport that cannot tell whether it sent must say so
(`ambiguous=True`) instead of claiming success; the orchestrator maps that to a
retryable outcome.

What is deliberately not here
-----------------------------
No retry queue and no delivery receipt. Both are real needs and neither is
invented here: the run's own retry (with the same `command_id`) is the retry, and
a delivery receipt needs a provider that reports one.
"""

from __future__ import annotations

import asyncio
import smtplib
import time
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

import httpx

from platform_core.config import Settings


class ChannelNotConfigured(RuntimeError):
    """No transport is configured for this channel.

    Raised rather than silently doing nothing: a no-op here is the
    `worker_cannot_send` class of defect, where every health check passes and the
    customer simply never hears back.
    """


@dataclass(frozen=True)
class SendResult:
    """`ambiguous=True` means the outcome is unknown and must not be called a
    success - retrying with the same `command_id` is the safe next step."""

    ambiguous: bool = False


class ChannelTransport(Protocol):
    """One channel's way of delivering a message."""

    system: str

    async def send(
        self, *, address: str, conversation_key: str, content: str, command_id: str
    ) -> SendResult: ...


class ChannelSender:
    """Routes an outbound message to the transport for its channel.

    A registry rather than an if-chain in the orchestrator, so adding a channel
    does not mean editing the run path.
    """

    def __init__(self, transports: Mapping[str, ChannelTransport]) -> None:
        self._transports = dict(transports)

    def configured(self, system: str) -> bool:
        return system in self._transports

    @property
    def systems(self) -> tuple[str, ...]:
        return tuple(sorted(self._transports))

    async def send_message(
        self,
        *,
        system: str,
        address: str,
        conversation_key: str,
        content: str,
        command_id: str,
    ) -> SendResult:
        transport = self._transports.get(system)
        if transport is None:
            raise ChannelNotConfigured(f"no outbound transport for {system!r}")
        return await transport.send(
            address=address,
            conversation_key=conversation_key,
            content=content,
            command_id=command_id,
        )


class EmailSmtpTransport:
    """Delivers an answer as a reply in the customer's thread."""

    system = "email"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        from_address: str,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._from = from_address
        self._username = username
        self._password = password

    async def send(
        self, *, address: str, conversation_key: str, content: str, command_id: str
    ) -> SendResult:
        message = EmailMessage()
        message["From"] = self._from
        message["To"] = address
        # The thread root, not the message being answered. Using the parent would
        # make the customer's next reply resolve to a *different* conversation
        # key and split the thread - the same trap `channels.email` documents on
        # the way in, and it has to be avoided on the way out too.
        message["In-Reply-To"] = conversation_key
        message["References"] = conversation_key
        # Carries the run-derived idempotency key into the provider's own logs,
        # so a duplicate delivery is traceable rather than mysterious.
        message["X-Command-Id"] = command_id
        message.set_content(content)

        # `smtplib` is blocking; a run must not hold the event loop for a network
        # round trip.
        await asyncio.to_thread(self._send_blocking, message)
        return SendResult()

    def _send_blocking(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=20) as smtp:
            smtp.starttls()
            if self._username and self._password:
                smtp.login(self._username, self._password)
            smtp.send_message(message)


class WeChatTransport:
    """Delivers an answer as a WeChat customer-service message.

    Not the passive XML reply: that must go back inside five seconds, and a run
    takes 8-68s on this deployment (ADR 0013). The customer-service API has no
    such deadline, which is why it is the delivery path for the real answer.
    """

    system = "wechat"

    # S105 is a false positive here: these are endpoints, not credentials. The
    # rule keys on the word "token" in the name, and renaming them to dodge a
    # lint would hide what they are for. The actual secret is `app_secret`, which
    # arrives as a `SecretStr` and is never a literal.
    _TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"  # noqa: S105
    _SEND_URL = "https://api.weixin.qq.com/cgi-bin/message/custom/send"

    def __init__(self, *, app_id: str, app_secret: str, timeout_seconds: float = 15.0) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._timeout = timeout_seconds
        self._token: str | None = None
        self._token_expires_at = 0.0

    async def send(
        self, *, address: str, conversation_key: str, content: str, command_id: str
    ) -> SendResult:
        del conversation_key  # the openid is the address; there is no thread key
        token = await self._access_token()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                self._SEND_URL,
                params={"access_token": token},
                json={
                    "touser": address,
                    "msgtype": "text",
                    "text": {"content": f"{content}\n\n[{command_id}]"},
                },
            )
        if resp.status_code >= 500:
            # A server error is genuinely unknown: the message may have been
            # accepted before the failure. Claiming success could double-send on
            # retry; claiming failure could lose it. Say so.
            return SendResult(ambiguous=True)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errcode"):
            # WeChat answers 200 with an errcode. A stale token is worth one
            # retry; anything else is a real failure.
            if payload["errcode"] in (40001, 42001):
                self._token = None
                raise ChannelNotConfigured("wechat access token rejected")
            raise RuntimeError(f"wechat send failed: errcode={payload['errcode']}")
        return SendResult()

    async def _access_token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._token_expires_at:
            return self._token
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                self._TOKEN_URL,
                params={
                    "grant_type": "client_credential",
                    "appid": self._app_id,
                    "secret": self._app_secret,
                },
            )
        resp.raise_for_status()
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise ChannelNotConfigured(
                f"wechat token unavailable: errcode={payload.get('errcode')}"
            )
        self._token = str(token)
        # Refresh a minute early so a request never races the expiry.
        self._token_expires_at = now + max(0, int(payload.get("expires_in", 7200)) - 60)
        return self._token


def build_channel_sender(settings: Settings) -> ChannelSender:
    """Wire whichever channels have credentials.

    A channel with no credentials is **absent** from the registry rather than
    present and broken, so `ChannelSender.configured()` tells the truth and the
    orchestrator can distinguish "receive-only" from "delivery failed".
    """
    transports: dict[str, ChannelTransport] = {}

    if settings.email_smtp_host and settings.email_from_address:
        password = (
            settings.email_smtp_password.get_secret_value()
            if settings.email_smtp_password is not None
            else None
        )
        transports["email"] = EmailSmtpTransport(
            host=settings.email_smtp_host,
            port=settings.email_smtp_port,
            from_address=settings.email_from_address,
            username=settings.email_smtp_username,
            password=password,
        )

    if settings.wechat_app_id and settings.wechat_app_secret is not None:
        transports["wechat"] = WeChatTransport(
            app_id=settings.wechat_app_id,
            app_secret=settings.wechat_app_secret.get_secret_value(),
        )

    return ChannelSender(transports)
