# Enterprise Integrations

## Integration principles

- Every external system is behind a Connector Adapter.
- Provider payloads never leak into core domain models.
- Read and write capabilities are separately authorized.
- Credentials are secret references, not application fields.
- Webhooks are signed, replay-protected and idempotent.

### Credential resolution

`credential_ref` is dereferenced by
`integrations/credentials.py::resolve_credentials`, which the executor
resolver calls through an injected function so the registry itself never
reads a secret.

The pilot supports one scheme:

```text
env://VAR_NAME
```

The variable holds either a JSON object used as the credentials mapping
(`{"api_token": "...", "user_email": "..."}`) or a bare token, returned as
`api_token`. Any other scheme — including `vault://` — resolves to **no**
credentials, so an adapter sends no Authorization header and the call fails
loudly instead of looking like a real attempt with an empty credential.

Production replaces this one function with a secret-manager client; call
sites do not change. Until then a connector with a `vault://` reference is
readable and configurable but cannot authenticate.
- Sync is incremental and resumable through cursors.
- External failure is isolated and visible.

## Integration Hub model

```text
Connector
- id
- tenant_id
- provider
- status: active|degraded|needs_reauth|disabled
- capabilities
- configuration
- credential_ref

SyncCursor
- connector_id
- resource_type
- cursor
- watermark

ExternalResourceRef
- tenant_id
- provider
- resource_type
- external_id
- internal_resource_type
- internal_resource_id
```

## CRM

### Pilot capabilities

- lookup account/contact;
- show plan, contract and entitlement summary;
- link Chatwoot contact and enterprise account;
- add a support interaction note after human approval;
- avoid bulk bidirectional synchronization initially.

### Canonical CRM projection

```text
CrmAccountSummary
CrmContactSummary
EntitlementSummary
RecentActivitySummary
```

Cache only fields needed for support, with TTL and source timestamp. Live-sensitive facts must state freshness.

### Controlled write: `crm.update_account`

The one CRM write tool, registered in the Tool Gateway as a
`confirmed_write` (propose -> human confirm -> execute -> verify).

- The tenant's CRM connector must claim `update_account`. A connector that
  only reads never becomes a write path; the resolver refuses to build an
  executor, and the gateway reports `TOOL_EXECUTOR_MISSING`.
- The writable set is closed (`tier`, `contract_status`). An unknown field is
  refused rather than forwarded, so a prompt injection cannot rewrite
  arbitrary account state.
- The postcondition is verified by re-reading the account, not by trusting
  the PATCH response. The read cache is invalidated first, otherwise the
  verification would compare against the pre-update projection and report a
  false failure.

## Issue trackers

Supported pattern for Jira/Linear:

- search existing issues before creation;
- create linked issue from a Case;
- sync status and assignee;
- append customer-safe updates;
- retain external URL and version;
- avoid copying restricted conversation content by default.

A Case remains the customer-support record; the issue tracker remains the engineering-work record.

## Enterprise IM

Priority options depend on target customers:

- Slack or Microsoft Teams for international SaaS.
- Feishu or DingTalk for China-based enterprises.
- WeChat only if customer-facing channel demand is validated.

IM integration modes:

1. Agent notification and deep link.
2. Internal approval workflow.
3. Shared support channel ingestion.
4. Full customer conversation channel.

Implement in that order; each step adds identity and threading complexity.

## SSO and provisioning

Pilot:

- Keycloak OIDC.
- Tenant-specific IdP configuration.
- Domain discovery.
- Explicit role mapping.

Productization:

- SAML.
- SCIM users and groups.
- deprovisioning and session revocation.
- group-to-role/department mapping.
- just-in-time provisioning policy.

Do not map an IdP group directly to unrestricted platform administrator without a tenant-controlled mapping and audit event.

## Connector execution behavior

Every external request defines:

- connect and total timeout;
- retryable status/error set;
- maximum attempts;
- rate-limit handling;
- idempotency support;
- postcondition verification;
- redacted telemetry;
- circuit-breaker policy.

For ambiguous writes:

1. Do not retry blindly.
2. Query by idempotency key or expected resource identity.
3. Verify whether the action completed.
4. Mark `UNKNOWN` and require human review if still ambiguous.

## Versioning and contract tests

- Store representative redacted provider fixtures.
- Run adapters against sandbox environments where available.
- Pin API versions.
- Monitor deprecation headers and announcements.
- Fail on unknown required enum/state values rather than silently mapping them.
- Feature-flag new provider behavior by tenant.

## Recommended integration order

1. Chatwoot.
2. OIDC/Keycloak.
3. One CRM read adapter.
4. Jira or Linear.
5. One IM notification/approval adapter.
6. One controlled write tool.
7. SAML/SCIM.
8. Additional customer conversation channels.

This order maximizes enterprise value while containing identity and reliability risk.
