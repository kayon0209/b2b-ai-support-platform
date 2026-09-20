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
  → resolve what the conversation is waiting on (may override the path)
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
2. `CASE_STATUS`: read an internal Case or linked issue. **Not currently
   produced by the classifier** — a case-status question is classified
   `BUSINESS_READ` and answered by the `case.read` tool, which is why the
   evaluation dataset declares `business_read` for it. Listed here because the
   class is defined in the taxonomy; noted because a documented class the
   classifier cannot emit reads as a capability that exists.
3. `BUSINESS_READ`: query CRM, entitlement, contract, asset or order data.
4. `BUSINESS_WRITE`: create/update an external record through a deterministic flow.
5. `SENSITIVE`: security, legal, HR, account ownership, personal data.
6. `OUT_OF_SCOPE`: unsupported or unrelated requests.
7. `HUMAN_REQUIRED`: explicit request, conflict, low evidence, policy requirement, or repeated failure.

### The one context-dependent decision

A confirmation is the single message whose meaning depends on what the
conversation is waiting for rather than on its own words. "确认" carries no verb
and no object, so it never classifies as a write request — but in a conversation
linked to an `eq_confirmation` case in `waiting_customer`, it is the answer
production is held for.

The decision is gated on four facts that must all hold: the write path is
enabled for the tenant, exactly one case is linked to this conversation, that
case is `eq_confirmation`, and it is `waiting_customer`; plus the message reading
as assent (`agent_runtime.confirmation`, lexical and deliberately narrow —
short, no negation, not a question).

**What it does is hand off, not act.** `case.eq_confirm` is `human_approval`,
the class this deployment reserves for `tenant_owner` and keeps unreachable by
the agent at every stage including propose, because the case status is what the
factory reads and a customer's word in a conversation is not a production
release. The AI relays and collects; a person records the confirmation. So the
run ends in a handoff with reason `EQ_CONFIRMATION_REQUIRES_HUMAN`.

That is still worth doing: without it the QA path answers a bare "确认" from the
corpus, or asks a customer who has just answered the platform's question to say
more. Both are plainly wrong, and neither is a handoff.

### Claims against the company (L6 争议归责)

A customer demanding compensation, a refund, a return or an escalation is not
asking what the policy says — they are claiming under it. The research report
classes these **L6, 必须转人工** and forbids the AI from any 归责表态 or 赔付承诺,
so there is no answer for the QA path to produce. Such a run abstains with
reason `COMPLAINT_REQUIRES_HUMAN` and hands off, before retrieval, before the
write path and before the clarification gate.

Detection is `agent_runtime.complaint` (lexical, narrow, with a question veto),
and it keys on the **claim**, not on `Scene.COMPLAINT`. That was measured, not
assumed: the scene pattern counts "still not" and "third time" as complaint
signals, so `"my order has still not arrived"` and `"The shipment still not
updated"` both classify as `COMPLAINT` — a scene gate sends an order-status
question to a human queue instead of to `order.get_status`. The scene is
therefore not consulted.

What stays answerable is the question *about* the same topic, which the report
puts at **L1** (检索 + 引用): `全测板开短路不良怎么赔付？` asks how compensation
works and is answered from the corpus; `板子短路了，我要索赔` claims under it and
is not. The veto is the discriminator, and it is the part most likely to be
removed by someone simplifying this later — `test_agent_complaint_handoff.py`
fails if it is.

This gate is deliberately **not** behind a feature flag, unlike the EQ branch
above. That branch adds a behaviour a tenant opts into; this one removes an
answer the report classes as a red line, and a red line behind a default-off
flag is not a control.

Not implemented, and recorded as a boundary rather than left implicit: the
report also asks the AI to collect structured evidence (order number, batch,
defect count, photos). The handoff notice asks for the order number and photos
because a person receives them either way, but there is no structured intake
form, so nothing parses or stores them as fields. Building the intake without
the form would be a capability with no consumer.

### Tier: whose complaint this is (难点 5)

Two identical complaints are not the same event if one of them is a key
account's. When the conversation's contact is bound to an `EnterpriseAccount`
whose tier is `strategic`/`enterprise` **and** whose contract is `active`, the
handoff reason becomes `STRATEGIC_ACCOUNT_REQUIRES_HUMAN` and the private note
names the account and tier, so the receiving team knows whose contract this is.
The act is the same either way - a person takes over - because the red line
does not depend on tier.

The binding (`enterprise_account_contacts`) is keyed on the **Chatwoot
contact**, not on the inbox: an inbox is a channel shared by everyone who walks
in through it, so binding an inbox would make every customer in that channel a
key account. The contact id is **not in the webhook payload** - measured over
every stored event: `contact_id` 0/29, `sender_id` 1/29 - so it is read from the
message via the Chatwoot API. An unresolvable contact degrades to "unbound",
never to a failed run: the tier sharpens a handoff that happens regardless.

`contract_status` is checked alongside tier, the same rule `sla_policy_for_tier`
applies to the SLA clock - a contract that ended no longer buys the dedicated
route.

Not implemented: the report's 专属对接人 is a named person, and
`EnterpriseAccount` has no owner field, so the handoff can name the *account*
but not route to a named individual. Adding that field is a schema decision
about where the owner of record lives.

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
