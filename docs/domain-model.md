# Domain Model

## Ownership rule

The source system owns its resources. The custom platform references Chatwoot entities through external mappings and never duplicates them as authoritative records.

## Identity and tenancy

### Tenant

Represents a customer organization using the platform.

```text
Tenant
- id: UUID
- slug
- name
- status
- default_timezone
- data_region
- created_at
```

### EnterpriseAccount

Represents the tenant's own customer/account hierarchy. Do not confuse it with a Chatwoot Account.

```text
EnterpriseAccount
- id
- tenant_id
- parent_id?
- external_crm_ref?
- name
- tier
- contract_status
- attributes: JSONB
```

### Membership and authorization

```text
User
Membership(user_id, tenant_id, department_id?, status)
Role
Permission
MembershipRole
Policy(condition JSONB, effect allow|deny)
ExternalIdentity(system, subject, user_id, tenant_id)
```

Deny rules take precedence. Service accounts are explicit actors and use short-lived credentials.

## External resource mapping

```text
ExternalResourceRef
- id
- tenant_id
- system
- resource_type
- external_id
- external_url?
- source_version?
- last_synced_at?
- metadata: JSONB

UNIQUE(tenant_id, system, resource_type, external_id)
```

## Support and Case

Chatwoot owns Conversation and Message. The custom platform owns Case.

```text
Case
- id
- tenant_id
- enterprise_account_id?
- requester_user_ref?
- subject
- description
- category
- priority: p0|p1|p2|p3
- status
- assignee_ref?
- team_ref?
- sla_policy_id
- first_response_due_at?
- resolution_due_at?
- resolved_at?
- version

CaseConversation
- case_id
- tenant_id
- conversation_ref_id
- relationship: origin|follow_up|related
```

### Case states

```text
NEW
→ TRIAGED
→ IN_PROGRESS
→ WAITING_CUSTOMER | WAITING_INTERNAL | WAITING_VENDOR
→ RESOLVED
→ CLOSED

RESOLVED/CLOSED → REOPENED → IN_PROGRESS
```

All transitions are explicit commands and audited. SLA pause behavior is determined by policy, not inferred from labels.

## Conversation control

```text
ConversationControlLease
- tenant_id
- conversation_ref_id
- owner_type: ai|human|queue
- owner_ref?
- mode
- lease_version
- expires_at?
- changed_reason
- updated_at
```

A customer-visible AI send requires compare-and-set on `lease_version` immediately before dispatch.

## Knowledge

```text
KnowledgeSpace
- id
- tenant_id
- name
- status
- default_policy_id?

KnowledgeSource
- id
- tenant_id
- space_id
- type: upload|website|notion|confluence|drive|api
- config_ref
- sync_cursor?
- status

Document
- id
- tenant_id
- space_id
- source_id
- canonical_uri
- title
- owner_ref?
- classification

DocumentVersion
- id
- tenant_id
- document_id
- version_label
- content_hash
- effective_at?
- expires_at?
- status: draft|processing|active|superseded|expired|failed
- object_uri
- parser_version

Chunk
- id
- tenant_id
- document_version_id
- section_path
- ordinal
- text
- text_hash
- metadata: JSONB
- embedding
- search_vector

KnowledgeAcl
- resource_type: space|document|version
- resource_id
- principal_type: user|role|department|enterprise_account
- principal_id
- permission
```

Only active, effective, unexpired and authorized versions are retrievable.

### Ingestion states

```text
UPLOADED
→ PARSING
→ CHUNKING
→ EMBEDDING
→ INDEXING
→ READY

Any active state → FAILED
FAILED → QUEUED_FOR_RETRY
READY → SUPERSEDED | EXPIRED
```

Jobs are idempotent by `(document_version_id, pipeline_version)`.

## AI runtime

```text
AgentDefinition
PromptTemplate
PromptVersion
ModelConfiguration
RetrievalConfiguration
AgentRun
- id
- tenant_id
- conversation_ref_id
- case_id?
- route
- status
- prompt_version_id
- model_config_id
- retrieval_config_id?
- policy_version
- trace_id
- input_hash
- output_hash?
- token_usage
- latency_ms

Citation
- agent_run_id
- document_version_id
- chunk_id
- excerpt_hash
- source_uri
- claim_index?
```

## Tools

```text
ToolDefinition
- id
- tenant_id?          # null only for platform catalog definitions
- name
- version
- risk
- input_schema
- output_schema
- required_permissions
- timeout_ms
- idempotent
- requires_confirmation

ToolExecution
- id
- tenant_id
- agent_run_id?
- actor_id
- tool_definition_id
- idempotency_key
- status
- sanitized_input
- sanitized_output?
- confirmation_id?
- started_at
- completed_at?
- verification_status?

ActionConfirmation
- id
- tenant_id
- actor_id
- action_hash
- scope
- expires_at
- confirmed_at?
```

## Integration

```text
Connector
ConnectorCredentialRef
WebhookSubscription
SyncCursor
WebhookDelivery
ConnectorRun
DeadLetterItem
```

Credentials are never stored directly in application JSON. Store only a reference to a secret manager entry.

## Audit

```text
AuditEvent
- id
- tenant_id
- occurred_at
- actor_type
- actor_id?
- action
- resource_type
- resource_id?
- decision: allowed|denied|completed|failed
- reason_code
- trace_id
- before_hash?
- after_hash?
- metadata: redacted JSONB
```

Audit events are append-only and protected from normal application updates.
