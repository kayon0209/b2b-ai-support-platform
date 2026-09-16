# ADR 0001: Use Chatwoot as the Customer-Support Kernel

- Status: Accepted
- Date: 2026-09-16

## Context

The product needs enterprise customer conversations, contacts, human agents, assignments, handoff, channels and operational reporting. Candidate foundations included Chatwoot, TGO, Dify, Rasa, Typebot and Chaskiq.

Building these capabilities from scratch would delay validation. Combining multiple repositories would introduce conflicting languages, queues, user models, conversation models, knowledge stores and release processes.

## Decision

Use Chatwoot as the sole customer-support kernel and keep it close to upstream.

Build an independent FastAPI platform for:

- tenant and enterprise account extensions;
- Case/Ticket and SLA;
- identity/policy overlay;
- knowledge ingestion and permission-aware RAG;
- AI runtime, citations and abstention;
- controlled enterprise tools;
- integrations, audit and evaluation.

Integration occurs only through documented APIs, signed webhooks, embedded Dashboard Apps and versioned events. Custom services do not access Chatwoot tables.

## Why Chatwoot

- Most complete open-source support workspace among candidates.
- Mature conversation, contact, inbox, assignment and channel models.
- Human handoff and support collaboration are already implemented.
- Large community and established deployment patterns.
- API/webhook extension points allow an AI sidecar architecture.

## Why not TGO as the sole base

TGO has a highly relevant FastAPI/React/RAG/MCP architecture, but it is newer, operationally broader and less proven as an enterprise support kernel. Its design remains a useful reference.

## Why not Dify as the sole base

Dify is an LLM application platform, not a support desk. It lacks the authoritative conversation, assignment, human-agent, SLA and channel domain required here.

## Consequences

### Positive

- Faster MVP and enterprise pilot.
- Lower risk in traditional support features.
- AI and enterprise differentiation remain under our control.
- Chatwoot can be upgraded independently if the integration boundary is respected.

### Negative

- Rails/Vue and FastAPI/React coexist.
- Two PostgreSQL databases and two Redis instances are operated.
- Tenant and identity mappings require care.
- Some enterprise UI appears through embedded or separate React surfaces.

## Constraints

- No direct Chatwoot database access.
- No extensive fork without a separate ADR.
- Conversation and Case remain distinct.
- Chatwoot Account IDs are external references, not internal tenant IDs.
- Chatwoot and custom Redis instances remain isolated.
- Any copied source code requires explicit license review.

## Revisit criteria

Revisit only if:

- Chatwoot licensing blocks the intended commercial model;
- required upgrades cannot be maintained with a small patch set;
- measured performance fails after supported scaling;
- core enterprise workflows cannot be implemented through supported extension points;
- total integration cost exceeds a validated custom replacement plan.
