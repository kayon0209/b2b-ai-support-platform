"""Audit probe: the two language decisions, side by side.

(a) Is a message long enough to try to answer? (Chinese and English measured
    in their own scripts.)
(b) Which language does the customer-visible notice come back in?

Run from the project root:
    .venv/Scripts/python.exe scripts/_audit_probe_language.py
"""

import sys

sys.path.insert(0, "apps/api/src")

from platform_core.agent_runtime.conversation import needs_clarification  # noqa: E402
from platform_core.agent_runtime.hours import offline_notice  # noqa: E402
from platform_core.agent_runtime.qa_path import (  # noqa: E402
    ABSTAIN_CLARIFICATION,
    ABSTAIN_EMOTION_ESCALATION,
    safe_abstention_text,
    system_outage_notice,
    unverified_read_notice,
)

print("== (a) 长度门槛：中文短问句必须放行，问候语仍要挡 ==")
CASES = [
    ("交期是多久？", "太短"),
    ("常规交期是几个工作日？", "太短"),
    ("怎么退款", "太短"),
    ("发票怎么开", "太短"),
    ("SO-9001 到哪了", "太短"),
    ("在吗", "放行"),
    ("你好", "放行"),
    ("?", "放行"),
    ("it?", "放行"),
    ("hello?", "放行"),
    ("Where is my order?", "太短"),
]
for question, _ in CASES:
    ask, reason = needs_clarification(question, [])
    verdict = "太短" if ask else "放行"
    print(f"  {verdict:<5} {(reason or '-'):<26} {question!r}")

print()
print("== (b) 话术语言：由提问的语言决定 ==")
ZH, EN = "交期是多久？", "What is the launch date?"
rows = [
    (
        "澄清",
        safe_abstention_text(ABSTAIN_CLARIFICATION, ZH),
        safe_abstention_text(ABSTAIN_CLARIFICATION, EN),
    ),
    (
        "身份闸门",
        safe_abstention_text("IDENTITY_REQUIRED", ZH),
        safe_abstention_text("IDENTITY_REQUIRED", EN),
    ),
    (
        "身份不符",
        safe_abstention_text("IDENTITY_MISMATCH", ZH),
        safe_abstention_text("IDENTITY_MISMATCH", EN),
    ),
    (
        "情绪升级",
        safe_abstention_text(ABSTAIN_EMOTION_ESCALATION, ZH),
        safe_abstention_text(ABSTAIN_EMOTION_ESCALATION, EN),
    ),
    ("系统故障", system_outage_notice(ZH), system_outage_notice(EN)),
    ("未核实记录", unverified_read_notice(ZH), unverified_read_notice(EN)),
    ("非服务时间", offline_notice(question=ZH), offline_notice(question=EN)),
]
for label, zh, en in rows:
    print(f"  [{label}]")
    print(f"    ZH: {zh[:64]}")
    print(f"    EN: {en[:64]}")

print()
print("== 默认（调用方拿不到 question 时）必须仍是英文，不出现空串 ==")
print("  ", repr(safe_abstention_text("IDENTITY_REQUIRED")[:48]))
print("  ", repr(safe_abstention_text("NO_SUCH_REASON_CODE")[:48]))
