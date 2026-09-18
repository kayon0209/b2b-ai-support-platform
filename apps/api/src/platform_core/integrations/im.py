"""Enterprise IM notification adapters (ticket 32, Phase 5 "additional channels").

Mode 1 only (agent notification + deep link) per docs/integrations.md: the
simplest integration step, with no identity or threading complexity.

Why there is more than one adapter
----------------------------------
This module used to hold a single `ImNotificationAdapter` whose docstring
claimed it posted "a Slack/Teams/Feishu compatible incoming-webhook shape".
That claim was false for two of the three named providers. Each of them wants a
different JSON body:

    im_webhook   {"text": "..."}                             (Slack)
    teams        {"@type": "MessageCard", "@context": ..., "summary": ...}
    feishu       {"msg_type": "text", "content": {"text": ...}}

A Teams or Feishu webhook rejects the Slack body. Teams rejects it with a 4xx,
which the generic adapter at least reports honestly. **Feishu answers with HTTP
200 and `{"code": 19024, "msg": "..."}`** - so a status-only success check
reports a notification as delivered that the provider discarded. That is the
failure this split exists to prevent, and it is why `_accepted` is a per-provider
decision rather than `result.ok`.

The generic adapter keeps the Slack shape and now says so, rather than
implying coverage it does not have.
"""

from typing import Any

from platform_core.integrations.sdk import ConnectorAdapter, ExecutionResult

# Deep links and titles are truncated in the adapter rather than trusted from
# the caller: a connector is a boundary, and a 40 kB error body pasted into a
# chat message is a way to leak whatever was in it.
MAX_TITLE = 120
MAX_BODY = 500


def _lines(*, title: str, body_text: str, case_ref: str | None, deep_link: str | None) -> list[str]:
    out = [f"*{title[:MAX_TITLE]}*"]
    if body_text:
        out.append(body_text[:MAX_BODY])
    if case_ref:
        out.append(f"Case: {case_ref}")
    if deep_link:
        out.append(deep_link)
    return out


class _WebhookNotifier(ConnectorAdapter):
    """Shared plumbing: URL lookup, truncation, postcondition.

    Subclasses supply only the two things that actually differ per provider:
    the payload shape and how that provider reports acceptance.
    """

    capabilities = ("send_notification",)

    def _body(
        self, *, title: str, body_text: str, case_ref: str | None, deep_link: str | None
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _accepted(self, result: ExecutionResult) -> bool | None:
        """Did the provider accept it, judged by the provider's own signal?

        `None` means "cannot tell" and is not the same as `False`: the gateway
        treats an unverifiable outcome as UNKNOWN, and reporting False would
        claim a rejection the platform did not observe.
        """
        return result.ok

    async def health_check(self) -> bool:
        return bool(self.context.configuration.get("webhook_url"))

    async def fetch(self, resource: str, cursor: str | None = None) -> tuple[list[Any], str | None]:
        return [], None  # notification-only adapter

    async def send_notification(
        self,
        *,
        title: str,
        # Optional: a breach notice with nothing to add is a real notification,
        # and requiring a body would push callers into passing filler text.
        body_text: str = "",
        case_ref: str | None = None,
        deep_link: str | None = None,
    ) -> dict[str, Any]:
        url = self.context.configuration.get("webhook_url", "")
        if not url:
            # Same shape as the success and failure paths. A consumer reading
            # `output["ambiguous"]` must not have to special-case the refusal
            # that is easiest to hit.
            return {"ok": False, "error_code": "IM_NOT_CONFIGURED", "ambiguous": False}

        payload = self._body(
            title=title, body_text=body_text, case_ref=case_ref, deep_link=deep_link
        )
        result = await self.http_request(
            "POST", url, json_body=payload, max_retries=1, total_timeout=5.0
        )
        accepted = self._accepted(result)
        error_code = result.error_code
        if accepted is None and error_code is None:
            # A 2xx that the provider did not confirm. Reporting success would
            # claim a delivery nobody observed, so it is reported as a failure
            # with a code that says exactly what is missing.
            error_code = "IM_UNVERIFIED"
        return {
            "ok": accepted is True,
            "error_code": error_code,
            "ambiguous": result.ambiguous,
        }

    async def execute(
        self, tool_name: str, parameters: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        if tool_name != "im.send_notification":
            return None
        return await self.send_notification(
            title=parameters.get("title", "Support update"),
            body_text=parameters.get("body_text", ""),
            case_ref=parameters.get("case_ref"),
            deep_link=parameters.get("deep_link"),
        )

    async def verify_postcondition(
        self, tool_name: str, parameters: dict[str, Any], output: dict[str, Any] | None
    ) -> bool | None:
        if not output:
            return None
        if output.get("ok") is not True:
            return False
        # Webhook acceptance is the provider receipt; there is nothing further
        # to read back from a chat message.
        return True


class ImNotificationAdapter(_WebhookNotifier):
    """Slack-style incoming webhook (`{"text": ...}`).

    configuration keys:
      webhook_url: full incoming webhook URL (secret-ish; resolved from
        configuration by the platform, never logged)
      display_name: optional bot display name
    """

    provider = "im_webhook"

    def _body(
        self, *, title: str, body_text: str, case_ref: str | None, deep_link: str | None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "text": "\n".join(
                _lines(title=title, body_text=body_text, case_ref=case_ref, deep_link=deep_link)
            )
        }
        display = self.context.configuration.get("display_name")
        if display:
            payload["username"] = display
        return payload


class TeamsNotificationAdapter(_WebhookNotifier):
    """Microsoft Teams incoming webhook (a MessageCard).

    A Teams connector URL refuses `{"text": ...}` - the body must declare
    `@type` and a `summary` - so this cannot be served by the generic adapter
    and is its own provider.
    """

    provider = "teams"

    def _body(
        self, *, title: str, body_text: str, case_ref: str | None, deep_link: str | None
    ) -> dict[str, Any]:
        body = "\n\n".join(
            _lines(title=title, body_text=body_text, case_ref=case_ref, deep_link=deep_link)
        )
        # `summary` is required by the schema and is what a notification
        # preview shows; falling back to the title keeps it non-empty.
        return {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": title[:MAX_TITLE] or "Support update",
            "title": title[:MAX_TITLE],
            "text": body,
        }


class FeishuNotificationAdapter(_WebhookNotifier):
    """Feishu / Lark custom bot webhook.

    Two differences from the generic adapter, and the second is why this class
    has to exist:

    1. the body is `{"msg_type": "text", "content": {"text": ...}}`;
    2. **Lark answers a malformed or rejected message with HTTP 200** and a
       non-zero `code` in the body. A status-only check therefore reports
       success for a message the provider threw away, which is worse than a
       failure - the tenant believes the on-call agent was paged.
    """

    provider = "feishu"

    def _body(
        self, *, title: str, body_text: str, case_ref: str | None, deep_link: str | None
    ) -> dict[str, Any]:
        return {
            "msg_type": "text",
            "content": {
                "text": "\n".join(
                    _lines(title=title, body_text=body_text, case_ref=case_ref, deep_link=deep_link)
                )
            },
        }

    def _accepted(self, result: ExecutionResult) -> bool | None:
        if not result.ok:
            return False
        if result.data is None:
            # A 2xx with a body we could not parse. The provider's signal is
            # the only way to know, and it is absent: UNKNOWN, not success.
            return None
        code = result.data.get("code")
        if code is None:
            # Older deployments answer with `StatusCode`/`StatusMessage`.
            code = result.data.get("StatusCode")
        if code is None:
            return None
        return str(code) == "0"


__all__ = [
    "FeishuNotificationAdapter",
    "ImNotificationAdapter",
    "TeamsNotificationAdapter",
]
