"""Audit probe: reproduce read-tool selection for the same question in two languages.

Run from the project root:
    .venv/Scripts/python.exe scripts/_audit_probe_selector.py
"""

import sys

sys.path.insert(0, "apps/api/src")

from platform_core.agent_runtime.intent import classify  # noqa: E402
from platform_core.tool_gateway.selector import select_read_tools  # noqa: E402

QUESTIONS = [
    "我的订单 SO-9001 到哪了？",
    "我的订单 SO-9001 到哪了",
    "我的订单到哪了？",
    "Where is my order SO-9001?",
    "SO-9001 什么时候发货",
    "帮我查一下订单 SO-9001 的状态",
    "订单 SO-9001 现在什么状态",
    "四层板的交期是多久？",
]

for q in QUESTIONS:
    d = classify(q)
    cands = select_read_tools(d, q)
    print("Q:", q)
    print("   route=", d.route.value, "scene=", d.scene.value)
    for c in cands:
        print(f"   score={c.score:.2f}  {c.tool_name}   ({c.reason})")
    print("   CHOSEN ->", cands[0].tool_name if cands else "(none)")
    print()
