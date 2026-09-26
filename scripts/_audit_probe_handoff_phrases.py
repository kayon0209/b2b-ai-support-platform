"""Audit probe: does the phrase behind a "talk to a human" button actually
route to a human?

Before building a button on it, this checks the platform already agrees. If the
phrases route to `human_required`, the customer surface needs no new endpoint -
the button sends the customer's own request down the path that already handles
it, and the handoff is real rather than a message the UI invents for itself.
(The internal panel's button is the latter: it appends a local "you are in the
queue" bubble and never calls the API. Copying that would ship a lie.)

Run from the project root:
    .venv/Scripts/python.exe scripts/_audit_probe_handoff_phrases.py
"""

import sys

sys.path.insert(0, "apps/api/src")

from platform_core.agent_runtime.intent import classify  # noqa: E402

PHRASES = [
    "转人工",
    "人工客服",
    "我想找人工",
    "请转人工客服",
    "我要找客服",
    "talk to a human",
    "I want a human agent",
    "交期是多久？",
]

print(f"{'短语':<24} {'route':<16} {'kind':<22} action")
print("-" * 84)
for phrase in PHRASES:
    d = classify(phrase)
    print(f"{phrase:<24} {d.route.value:<16} {d.primary_kind.value:<22} {d.action.value}")
