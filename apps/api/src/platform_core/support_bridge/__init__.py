"""Channel-agnostic primitives shared by every inbound customer channel.

`InboxEvent` persistence and dedup, the conversation-ref derivation, the
visitor credential, webhook signature verification, payload minimization,
continuity, per-channel formatting, satisfaction and media-as-attachment.

Named after the Chatwoot bridge it grew out of (ADR 0012 removed Chatwoot);
the primitives outlived the integration, and `/support` plus the email and
WeChat adapters are all built on them.
"""
