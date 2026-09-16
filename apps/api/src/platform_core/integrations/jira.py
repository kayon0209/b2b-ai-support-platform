"""Jira adapter (ticket 31, docs/integrations.md issue trackers).

Pattern: search before create, link issue to Case, sync status, append
customer-safe updates. A Case remains the support record; Jira remains
the engineering record. Restricted conversation content is never copied.

The adapter also implements the ToolExecutor protocol so
jira.create_issue can be registered in the Tool Gateway as a
confirmed_write tool with postcondition verification.
"""

import base64
import time
from dataclasses import dataclass
from typing import Any

from platform_core.integrations.sdk import ConnectorAdapter, ConnectorContext


@dataclass(frozen=True)
class JiraIssueSummary:
    """Canonical issue projection."""

    external_ref: str
    key: str
    title: str
    status: str
    assignee: str | None
    url: str | None
    fetched_at: int


class JiraAdapter(ConnectorAdapter):
    provider = "jira"
    capabilities = ("search_issues", "create_issue", "read_issue", "comment_issue")

    def __init__(self, context: ConnectorContext) -> None:
        super().__init__(context)
        cfg = context.configuration
        self._base = cfg.get("base_url", "").rstrip("/")
        self._project = cfg.get("project_key", "SUP")

    def _headers(self) -> dict[str, str]:
        token = self.context.credentials.get("api_token", "")
        email = self.context.credentials.get("user_email", "")

        basic = base64.b64encode(f"{email}:{token}".encode()).decode()
        return {"Authorization": f"Basic {basic}", "Content-Type": "application/json"}

    async def health_check(self) -> bool:
        if not self._base:
            return False
        result = await self.http_request(
            "GET", f"{self._base}/rest/api/3/myself", headers=self._headers(), max_retries=0
        )
        return result.ok

    async def fetch(
        self, resource: str, cursor: str | None = None
    ) -> tuple[list[JiraIssueSummary], str | None]:
        """Return (canonical issue projections, next_cursor).

        The base class declares `list[dict[str, Any]]`, but docs/api-contracts.md
        is explicit that "domain modules consume canonical models" - so the
        adapter projects provider payloads into `JiraIssueSummary` at this
        boundary rather than leaking a dict whose shape only the adapter knows.
        """
        if resource != "issues":
            return [], None
        jql = cursor or f"project = {self._project} ORDER BY updated ASC"
        result = await self.http_request(
            "GET",
            f"{self._base}/rest/api/3/search",
            headers=self._headers(),
            json_body={"jql": jql, "maxResults": 50},
        )
        if not result.ok or not result.data:
            return [], None
        issues = [self._project_issue(i) for i in result.data.get("issues", [])]
        next_cursor = result.data.get("nextPageToken")
        return issues, next_cursor

    async def search_issues(self, query: str, max_results: int = 10) -> list[JiraIssueSummary]:
        """Search before create (docs/integrations.md: avoid duplicates)."""
        jql = f'project = {self._project} AND summary ~ "{query}"'
        result = await self.http_request(
            "GET",
            f"{self._base}/rest/api/3/search",
            headers=self._headers(),
            json_body={"jql": jql, "maxResults": max_results},
        )
        if not result.ok or not result.data:
            return []
        return [self._project_issue(i) for i in result.data.get("issues", [])]

    async def create_issue(self, title: str, description: str, case_ref: str) -> dict[str, Any]:
        """Create a linked issue. Returns the external id and URL."""
        payload = {
            "fields": {
                "project": {"key": self._project},
                "summary": title[:255],
                "issuetype": {"name": "Task"},
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": f"[Case {case_ref}] {description}"}
                            ],
                        }
                    ],
                },
            }
        }
        result = await self.http_request(
            "POST",
            f"{self._base}/rest/api/3/issue",
            headers=self._headers(),
            json_body=payload,
        )
        if not result.ok:
            return {"ok": False, "error_code": result.error_code, "ambiguous": result.ambiguous}
        issue_id = str((result.data or {}).get("id", ""))
        issue_key = str((result.data or {}).get("key", ""))
        return {
            "ok": True,
            "issue_id": issue_id,
            "issue_key": issue_key,
            "url": f"{self._base}/browse/{issue_key}" if issue_key else None,
        }

    # --- ToolExecutor protocol (registered as jira.create_issue tool) ---

    async def execute(
        self, tool_name: str, parameters: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        if tool_name != "jira.create_issue":
            return None
        return await self.create_issue(
            parameters.get("title", ""),
            parameters.get("description", ""),
            parameters.get("case_ref", ""),
        )

    async def verify_postcondition(
        self, tool_name: str, parameters: dict[str, Any], output: dict[str, Any] | None
    ) -> bool | None:
        """Verify via a read: the created issue key must exist.

        None when we cannot determine (e.g. ambiguous transport outcome).
        """
        if not output:
            return None
        if output.get("ok") is not True:
            return False
        issue_key = output.get("issue_key")
        if not issue_key:
            return None
        check = await self.http_request(
            "GET",
            f"{self._base}/rest/api/3/issue/{issue_key}",
            headers=self._headers(),
            max_retries=1,
        )
        if check.ok:
            return True
        if check.error_code in ("CONNECTOR_UNAVAILABLE", "CONNECTOR_AUTH_EXPIRED"):
            return None  # cannot verify -> UNKNOWN
        return False

    def _project_issue(self, raw: dict[str, Any]) -> JiraIssueSummary:
        fields = raw.get("fields", {})
        return JiraIssueSummary(
            external_ref=str(raw.get("id", "")),
            key=str(raw.get("key", "")),
            title=str(fields.get("summary", "")),
            status=str((fields.get("status") or {}).get("name", "")),
            assignee=(fields.get("assignee") or {}).get("displayName"),
            url=f"{self._base}/browse/{raw.get('key', '')}",
            fetched_at=int(time.time()),
        )
