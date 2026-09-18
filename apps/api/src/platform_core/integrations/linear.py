"""Linear adapter: issue creation and issue listing.

`docs/development-plan.md` Phase 3 priority 2 is "Jira **or** Linear ticket
integration". Jira shipped; Linear is the other half of the "or", and until now
a tenant running Linear had no ticket integration at all.

Linear is GraphQL-only - there is no REST endpoint for issues - which shapes
this adapter in three ways worth naming:

**One endpoint, two operations.** `POST https://api.linear.app/graphql` with a
`query` (and `variables`). The tool and the fetch differ only in the document
they send, so the transport concerns (auth header, error extraction) are shared.

**Authentication is a bare API key, not a bearer token.** Linear expects
`Authorization: <key>`; sending `Bearer <key>` is rejected. The scheme is
derived from the credential's own `scheme` field when present, so an OAuth
token (which *is* a bearer token) can be configured without a code change.

**Failure is a 200 with an `errors` array.** GraphQL reports a rejected query,
a bad variable and an authorisation problem as HTTP 200 with a populated
`errors` list. A status-only check would read every one of those as success -
the same trap the Feishu webhook adapter exists to avoid. `_errors` is checked
before `data` is read, so a failed mutation can never be reported as a created
issue.

Pagination is cursor-based (`pageInfo.endCursor` / `hasNextPage`), which maps
onto the SDK's `fetch(resource, cursor)` contract directly. Unlike Jira's
`nextPageToken`, Linear's cursor is opaque and safe to store verbatim - which
is what `integrations/sync_service.py` does with it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from platform_core.integrations.sdk import ConnectorAdapter, ExecutionResult

DEFAULT_ENDPOINT = "https://api.linear.app/graphql"
PAGE_SIZE = 50


@dataclass(frozen=True)
class LinearIssueSummary:
    """Canonical projection (docs/api-contracts.md: domain modules consume
    canonical models, so the provider's payload shape stops at this boundary)."""

    external_ref: str
    key: str
    title: str
    status: str
    assignee: str | None
    url: str | None


_ISSUES_QUERY = """
query Issues($after: String, $first: Int) {
  issues(first: $first, after: $after, orderBy: updatedAt) {
    nodes { id identifier title url state { name } assignee { name } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_CREATE_ISSUE_MUTATION = """
mutation IssueCreate($title: String!, $description: String, $teamId: String!) {
  issueCreate(input: {title: $title, description: $description, teamId: $teamId}) {
    success
    issue { id identifier title url }
  }
}
"""

_ISSUE_QUERY = """
query Issue($id: String!) {
  issue(id: $id) { id identifier title url state { name } }
}
"""


class LinearAdapter(ConnectorAdapter):
    """Read surface (`fetch`) plus the `ToolExecutor` write surface.

    configuration keys:
      endpoint:    GraphQL endpoint (defaults to Linear's)
      team_id:     the team new issues are filed under (required to create)
    credentials:
      api_token:   a personal API key or an OAuth token
      scheme:      optional, e.g. "Bearer" for an OAuth token. Defaults to
                   Linear's bare-key form.
    """

    provider = "linear"
    capabilities = ("create_issue", "search_issues")

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        self._endpoint = (context.configuration.get("endpoint") or DEFAULT_ENDPOINT).rstrip("/")
        self._team_id = context.configuration.get("team_id", "")

    def _headers(self) -> dict[str, str]:
        token = self.context.credentials.get("api_token", "")
        scheme = self.context.credentials.get("scheme", "").strip()
        authorization = f"{scheme} {token}".strip() if scheme else token
        return {"Authorization": authorization, "Content-Type": "application/json"}

    async def health_check(self) -> bool:
        return bool(self.context.credentials.get("api_token"))

    @staticmethod
    def _errors(result: ExecutionResult) -> str | None:
        """The provider's own failure signal, or None when it reported none.

        GraphQL carries errors in a 200 response body, so this has to be read
        before `data` is trusted. `CONNECTOR_AUTH_EXPIRED` is returned as an
        error code by `http_request` for a 401/403, which the auth-reporting
        wrapper recognises - so a rejected key still parks the connector.
        """
        if not result.ok:
            return result.error_code or "CONNECTOR_UNAVAILABLE"
        if not result.data:
            return "CONNECTOR_UNPARSEABLE_RESPONSE"
        errors = result.data.get("errors")
        if errors:
            first = errors[0] if isinstance(errors, list) and errors else errors
            message = str(first.get("message", "")) if isinstance(first, dict) else str(first)
            lowered = message.lower()
            if "auth" in lowered or "permission" in lowered or "api key" in lowered:
                return "CONNECTOR_AUTH_EXPIRED"
            return f"CONNECTOR_REJECTED: {message[:120]}"
        return None

    async def _query(self, document: str, variables: dict[str, Any]) -> ExecutionResult:
        return await self.http_request(
            "POST",
            self._endpoint,
            headers=self._headers(),
            json_body={"query": document, "variables": variables},
            max_retries=1,
        )

    # --- read surface ---

    async def fetch(
        self, resource: str, cursor: str | None = None
    ) -> tuple[list[LinearIssueSummary], str | None]:
        """Return `(canonical issues, next_cursor)`.

        The cursor is Linear's own `endCursor`, stored verbatim by the caller.
        An empty page with `hasNextPage` false returns `None`, which is what
        tells a sync it has reached the end rather than that it is mid-walk.
        """
        if resource != "issues":
            return [], None
        result = await self._query(_ISSUES_QUERY, {"after": cursor, "first": PAGE_SIZE})
        failure = self._errors(result)
        if failure is not None:
            return [], None
        payload = (result.data or {}).get("data", {}).get("issues", {})
        issues = [self._project_issue(node) for node in payload.get("nodes", [])]
        page = payload.get("pageInfo") or {}
        next_cursor = page.get("endCursor") if page.get("hasNextPage") else None
        return issues, next_cursor

    # --- ToolExecutor write surface ---

    async def execute(
        self, tool_name: str, parameters: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        if tool_name != "linear.create_issue":
            return None
        return await self.create_issue(
            title=parameters.get("title", ""),
            description=parameters.get("description", ""),
            case_ref=parameters.get("case_ref", ""),
        )

    async def create_issue(
        self, *, title: str, description: str = "", case_ref: str = ""
    ) -> dict[str, Any]:
        if not self._team_id:
            # Refused before the call, not after: Linear requires a team, and
            # sending the mutation without one produces a GraphQL error the
            # operator would have to read to learn a configuration fact.
            return {
                "ok": False,
                "error_code": "LINEAR_TEAM_NOT_CONFIGURED",
                "ambiguous": False,
            }
        if not title.strip():
            return {"ok": False, "error_code": "LINEAR_TITLE_REQUIRED", "ambiguous": False}

        body = description[:8000]
        if case_ref:
            body = f"{body}\n\nCase: {case_ref}"
        result = await self._query(
            _CREATE_ISSUE_MUTATION,
            {"title": title[:255], "description": body, "teamId": self._team_id},
        )
        failure = self._errors(result)
        if failure is not None:
            return {
                "ok": False,
                "error_code": failure,
                "ambiguous": result.ambiguous,
            }

        payload = (result.data or {}).get("data", {}).get("issueCreate") or {}
        if not payload.get("success"):
            # A rejected mutation is a business failure inside a 200. Without
            # this check the caller would read {"ok": true} for an issue that
            # was never created.
            return {
                "ok": False,
                "error_code": "LINEAR_MUTATION_REJECTED",
                "ambiguous": False,
            }
        issue = payload.get("issue") or {}
        return {
            "ok": True,
            "issue_id": str(issue.get("id", "")),
            "issue_key": str(issue.get("identifier", "")),
            "url": issue.get("url"),
            "ambiguous": False,
        }

    async def verify_postcondition(
        self, tool_name: str, parameters: dict[str, Any], output: dict[str, Any] | None
    ) -> bool | None:
        """Verify by reading the issue back.

        `None` (UNKNOWN) when the outcome cannot be established - an ambiguous
        transport failure or an unverifiable read - because the gateway treats
        UNKNOWN as "a human must look" and False as "it did not happen".
        """
        if not output:
            return None
        if output.get("ok") is not True:
            return False
        issue_id = output.get("issue_id")
        if not issue_id:
            return None
        check = await self._query(_ISSUE_QUERY, {"id": str(issue_id)})
        failure = self._errors(check)
        if failure is not None:
            if failure in ("CONNECTOR_UNAVAILABLE", "CONNECTOR_AUTH_EXPIRED"):
                return None
            return False
        return bool((check.data or {}).get("data", {}).get("issue"))

    @staticmethod
    def _project_issue(raw: dict[str, Any]) -> LinearIssueSummary:
        return LinearIssueSummary(
            external_ref=str(raw.get("id", "")),
            key=str(raw.get("identifier", "")),
            title=str(raw.get("title", "")),
            status=str((raw.get("state") or {}).get("name", "")),
            assignee=(raw.get("assignee") or {}).get("name"),
            url=raw.get("url"),
        )


__all__ = ["LinearAdapter", "LinearIssueSummary"]
