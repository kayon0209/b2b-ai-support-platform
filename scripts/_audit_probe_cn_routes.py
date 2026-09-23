"""Audit probe: which Chinese phrasings reach the read path at all.

`select_read_tools` returns nothing unless the route is `business_read`, so a
question can fail for two different reasons and they need telling apart:
wrong route, or wrong tool among candidates. This prints both.
"""

import sys

sys.path.insert(0, "apps/api/src")

from platform_core.agent_runtime.intent import classify  # noqa: E402
from platform_core.tool_gateway.selector import select_read_tools  # noqa: E402

QUESTIONS = [
    "我的订单 SO-9001 到哪了？",
    "SO-9001 什么时候发货",
    "SO-9001 发货了吗",
    "订单 SO-9001 现在什么状态",
    "帮我查一下订单 SO-9001 的状态",
    "帮我查一下发票 INV-9001 的状态",
    "查一下发票 INV-9001 的状态",
    "我的发票 INV-9001 现在什么状态",
    "运单 SH-7001 的物流到哪了？",
    "帮我查一下运单 SH-7001 的进度",
    "库存还有多少",
    "帮我查一下库存 A-100 的余额",
]

for q in QUESTIONS:
    d = classify(q)
    cands = select_read_tools(d, q)
    chosen = cands[0].tool_name if cands else "(none)"
    scores = " ".join(f"{c.tool_name}={c.score:.2f}" for c in cands) or "-"
    print(f"{q:<34} route={d.route.value:<15} chosen={chosen:<20} {scores}")
