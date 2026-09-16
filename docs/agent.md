# AI Agent Runtime Design

## Purpose

The runtime answers enterprise support questions, assists human agents, and invokes approved enterprise tools without violating tenant, data, or action policies.

## Operating modes

| Mode | Customer-visible | May call tools | Owner |
|---|---:|---:|---|
| `AI_ACTIVE` | Yes | Read and approved writes | AI |
| `AI_WAITING_TOOL` | Status only | Existing execution only | AI |
| `AI_LOW_CONFIDENCE` | Safe clarification or handoff | Read-only | AI |
| `QUEUED_FOR_HUMAN` | Handoff acknowledgement | No new writes | Queue |
| `HUMAN_ACTIVE` | No autonomous reply | No | Human |
| `HUMAN_WITH_COPILOT` | Drafts only | Read-only by default | Human |
| `RESOLVED` | No | No | System |
| `REOPENED` | Based on routing policy | Based on mode | System |

Human ownership always takes precedence. A lease with version and expiry prevents concurrent AI and human replies.

## Request pipeline

```text
Message received
  → verify webhook and persist inbox event
  → resolve tenant, actor, contact, conversation and control lease
  → redact/minimize sensitive data
  → classify intent and risk
  → select path: answer / deterministic flow / handoff
  → retrieve authorized evidence or authorize tool
  → generate draft
  → validate citations, policy and output schema
  → re-check control lease
  → send through Chatwoot API
  → persist run, citations, outcome and metrics
```

## Routing classes

1. `KNOWLEDGE_QA`: policies, manuals, product documentation.
2. `CASE_STATUS`: read an internal Case or linked issue.
3. `BUSINESS_READ`: query CRM, entitlement, contract, asset or order data.
4. `BUSINESS_WRITE`: create/update an external record through a deterministic flow.
5. `SENSITIVE`: security, legal, HR, account ownership, personal data.
6. `OUT_OF_SCOPE`: unsupported or unrelated requests.
7. `HUMAN_REQUIRED`: explicit request, conflict, low evidence, policy requirement, or repeated failure.

## Evidence policy

- Retrieval runs only after tenant and ACL filters are constructed.
- Each factual claim must be supported by one or more citations when the answer comes from enterprise knowledge.
- Citations store document ID, version ID, chunk ID, source URI, excerpt hash and retrieval score.
- Similarity scores are ranking signals, not confidence probabilities.
- Missing, conflicting, expired or unauthorized evidence triggers clarification or handoff.
- The model must not cite a document version that was not present in the runtime context.

## Abstention rules

The agent must not guess when:

- no authorized evidence supports the requested fact;
- two active sources conflict materially;
- a document is expired and no replacement exists;
- the user requests restricted data;
- a required business system is unavailable;
- a write tool returns an ambiguous result;
- the action exceeds the user's permission or confirmation scope.

Safe response pattern:

1. State what cannot be verified.
2. Avoid inventing an explanation.
3. Ask for the minimum useful clarification or offer human handoff.
4. Preserve the evidence and failure reason for the receiving agent.

## Tool execution contract

Each tool declares:

```yaml
name: crm.get_account
version: 1
risk: read
required_permissions:
  - crm.account.read
input_schema: {}
output_schema: {}
timeout_ms: 3000
idempotent: true
requires_confirmation: false
postcondition_check: crm.get_account
```

Risk levels:

- `read`: automatic after authorization.
- `low_write`: may be automatic under tenant policy.
- `confirmed_write`: explicit user confirmation required.
- `human_approval`: authorized human approval required.
- `prohibited`: not callable by the AI.

Execution sequence:

```text
propose → authorize → preview → confirm → execute → verify → audit → summarize
```

An execution is successful only after its postcondition has been verified. Transport success is insufficient.

## Context management

Context layers, in priority order:

1. System safety and tenant policy.
2. Current actor permissions and enterprise account.
3. Current Case and SLA state.
4. Recent relevant conversation turns.
5. Authorized retrieved evidence.
6. Read-only tool results.
7. Older summary memory.

Do not copy the full conversation into every model request. Use token budgets and structured summaries. Never summarize away unresolved commitments, tool outcomes, approvals or ownership changes.

## Prompt and model versioning

Every run records:

- prompt template ID and version;
- model provider, model and parameters;
- retrieval configuration version;
- policy bundle version;
- tool schema versions;
- code release version;
- input and output hashes;
- latency and token/cost usage.

Prompts are immutable after publication. Changes create a new version and require evaluation gates.

## Copilot behavior

In `HUMAN_WITH_COPILOT` mode the agent may:

- summarize the conversation;
- retrieve evidence;
- draft replies with citations;
- suggest routing, priority and case fields;
- suggest tools and provide previews.

It must not send a customer-visible reply or execute writes unless a separate human-authorized action explicitly requests it.
