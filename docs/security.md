# Security and Trust Plan

## Security objectives

1. Prevent cross-tenant access in APIs, retrieval, caches, files, logs and metrics.
2. Prevent unauthorized or unconfirmed business mutations.
3. Minimize customer data sent to models and third-party systems.
4. Make every sensitive decision and action auditable.
5. Fail closed when identity, policy, evidence or tool results are ambiguous.

## Trust boundaries

- Browser/customer widget
- Platform-owned customer channels (`/support`, email and WeChat adapters)
- Custom API and workers
- Identity provider
- Model providers
- Object/vector/search storage
- CRM/ERP/issue trackers/IM
- Observability and analytics systems

Data crossing a boundary must be authenticated, authorized, minimized and traced.

## Tenant isolation

### Database

- All tenant-owned tables have non-null `tenant_id`.
- Enable PostgreSQL RLS in production and tests.
- Set tenant context per transaction from authenticated server-side context.
- Database roles used by the application cannot bypass RLS.
- Background jobs carry a signed or persisted tenant context reference, not arbitrary tenant input.

### Retrieval

- Tenant and ACL filters are applied before lexical/vector candidate retrieval.
- Cache keys include tenant, principal scope and retrieval configuration version.
- Reranking receives only authorized candidates.
- Citation resolution rechecks document authorization.

### Object storage

- Object keys begin with tenant UUID.
- Use pre-signed short-lived URLs.
- Validate content type and file signature.
- Malware-scan uploads before parsing.
- Encrypt at rest and maintain tenant-aware retention rules.

## Authentication and SSO

Pilot:

- Keycloak OIDC Authorization Code + PKCE.
- MFA delegated to the enterprise IdP.
- Short-lived access tokens and rotated refresh tokens.
- Service-to-service client credentials with narrow audiences.

Productization:

- SAML federation.
- SCIM user/group provisioning.
- Domain discovery and tenant-specific IdP routing.
- Just-in-time provisioning controlled by tenant policy.

## Authorization

Use three layers:

1. RBAC for understandable base roles.
2. ABAC for department, enterprise account, classification, region and contract conditions.
3. Resource ACL for knowledge spaces/documents and exceptional access.

Recommended roles:

- Tenant Owner
- Security Administrator
- Support Administrator
- Knowledge Manager
- Support Agent
- Support Viewer
- Integration Service Account
- Auditor

Deny overrides allow. Permission checks are centralized but domain modules own the policy facts required for decisions.

## Tool and action security

- Tools are deny-by-default.
- Input/output validated against versioned JSON Schema.
- Secrets resolved server-side and never exposed to the model.
- High-risk tools require human confirmation or approval.
- Confirmation binds actor, tenant, tool version, argument hash and expiry.
- Every write uses an idempotency key.
- Postconditions are verified through a read or provider receipt.
- Ambiguous results remain `UNKNOWN`; never report success.

## Prompt-injection controls

Treat customer messages, documents, connector data and tool results as untrusted content.

- Separate instructions from retrieved data structurally.
- Never execute instructions found inside documents.
- Allow tools only from server-side routing and policy.
- Do not let retrieved text change permissions, tenant context or tool schemas.
- Strip or neutralize active content from HTML and documents.
- Evaluate indirect prompt injection with adversarial fixtures.

## Sensitive-data handling

Classify fields as:

- Public
- Internal
- Confidential
- Restricted/regulated

Before model invocation:

- remove credentials, tokens and hidden metadata;
- redact unnecessary personal identifiers;
- send only the minimum relevant excerpts;
- select providers according to tenant data-region and retention policy;
- prohibit training on customer data contractually and technically where possible.

## Logging policy

Allowed by default:

- event type, tenant surrogate ID, trace ID;
- model name and version;
- token counts and latency;
- tool name, decision and status;
- document/version IDs and hashes;
- stable error codes.

Not allowed by default:

- raw prompts and outputs;
- full messages or documents;
- access tokens, cookies or secrets;
- raw CRM/ERP payloads;
- attachment contents;
- personal contact details.

Use field allowlists and centralized redaction before log emission.

## Audit

Audit events are append-only and include:

- authentication and membership changes;
- policy allow/deny decisions for sensitive resources;
- knowledge publication, expiry and ACL changes;
- AI customer-visible replies;
- handoff and control ownership changes;
- tool proposals, confirmations, executions and verification;
- connector credential and configuration changes;
- exports and administrative searches.

Audit storage has separate retention and restricted access.

## Threat scenarios and controls

| Scenario | Control |
|---|---|
| Client changes tenant ID | Derive tenant server-side; RLS |
| Vector search leaks another tenant | Pre-filter, RLS, cross-tenant tests |
| Document tells agent to ignore policy | Treat retrieval as data, not instructions |
| Duplicate webhook sends two replies | Delivery deduplication and outbound idempotency |
| Human takes over during generation | Versioned control lease re-check before send |
| Tool timeout actually completed remotely | Idempotency key and postcondition verification |
| Expired document remains indexed | Effective-date filter and version lifecycle |
| Logs expose customer messages | Allowlists, redaction and restricted raw-event store |
| Connector token stolen | Secret manager, encryption, rotation, narrow scopes |

## Security release gates

- Zero cross-tenant findings in automated suites.
- Zero unconfirmed high-risk write executions.
- Secret scan passes.
- Dependency vulnerabilities have accepted risk or remediation.
- Threat model updated for new boundaries.
- Backup and audit access controls verified.
