"""Enterprise IM notification adapter (ticket 32).

Mode 1 only (agent notification + deep link) per docs/integrations.md:
simplest integration step, no identity/threading complexity. Adapter
posts a redacted notification to a webhook URL (Slack/Teams/Feishu
compatible incoming-webhook shape).
"""

from typing import Any

from platform_core.integrations.sdk import ConnectorAdapter


class ImNotificationAdapter(ConnectorAdapter):
    """Posts compact, redacted support notifications to an IM webhook.

    configuration keys:
      webhook_url: full incoming webhook URL (secret-ish; resolved from
        configuration by the platform, never logged)
      display_name: optional bot display name
    """

    provider = "im_webhook"
    capabilities = ("send_notification",)

    async def health_check(self) -> bool:
        return bool(self.context.configuration.get("webhook_url"))

    async def fetch(
        self, resource: str, cursor: str | None = None
    ) -> tuple[list[Any], str | None]:
        return [], None  # notification-only adapter

    async def send_notification(
        self,
        *,
        title: str,
        body_text: str,
        case_ref: str | None = None,
        deep_link: str | None = None,
    ) -> dict[str, Any]:
        """Send one notification. Text is truncated; no customer PII is
        included by the caller's contract — the adapter only truncates."""
        url = self.context.configuration.get("webhook_url", "")
        if not url:
            return {"ok": False, "error_code": "IM_NOT_CONFIGURED"}

        lines = [f"*{title[:120]}*"]
        if body_text:
            lines.append(body_text[:500])
        if case_ref:
            lines.append(f"Case: {case_ref}")
        if deep_link:
            lines.append(deep_link)
        payload = {"text": "\n".join(lines)}
        display = self.context.configuration.get("display_name")
        if display:
            payload["username"] = display

        result = await self.http_request(
            "POST", url, json_body=payload, max_retries=1, total_timeout=5.0
        )
        return {
            "ok": result.ok,
            "error_code": result.error_code,
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
        # Webhook acceptance is the provider receipt (2xx); treat as verified.
        return True
