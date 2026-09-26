"""Inbound customer channels: email and WeChat (ADR 0013).

A channel is a `connectors` row plus an adapter. The adapter verifies the
delivery and translates it; `router.py` does everything else, so a new channel
does not grow its own copy of tenant resolution, persistence or queueing.
"""
