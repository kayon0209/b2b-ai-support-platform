"""Audit probe: verify every fix against the live API, end to end.

One conversation per check, walked the way the customer surface walks it:
open a session, verify identity, ask. Asserts what the audit found broken, so a
regression shows up here rather than in a bug report.

Run from the project root (API must be up):
    .venv/Scripts/python.exe scripts/_audit_probe_fixes.py
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:8000"
TIMEOUT = 30

# Every run gets its own conversations. The visitor id is what the platform
# derives the conversation from, so a fixed one resumes the *previous* run's
# thread - and `wait_for_agent` then finds an agent turn that was written by the
# code under test last time, which is how a fixed probe reported a failure that
# had already been fixed (and would report a pass for a regression).
RUN = uuid.uuid4().hex[:8]

PASSED: list[str] = []
FAILED: list[str] = []


def _post(path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers["Idempotency-Key"] = str(uuid.uuid4())
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _get(path: str, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{BASE}{path}", headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def check(label: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(label)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"\n        {detail}" if detail else ""))


def new_session(visitor: str) -> dict:
    status, body = _post(
        "/v1/support/sessions",
        {"tenant_slug": "admin-demo", "visitor_id": f"{visitor}-{RUN}"},
    )
    assert status == 200, (status, body)
    return body


def verify_identity(token: str) -> str:
    status, body = _post(
        "/v1/support/verify", {"order_id": "SO-9001", "phone_tail": "8888"}, token
    )
    assert status == 200, (status, body)
    return body["token"]


def wait_for_agent(token: str, seconds: int = 60) -> list[dict]:
    """Poll until an `agent` turn exists, or the deadline passes."""
    deadline = time.time() + seconds
    items: list[dict] = []
    while time.time() < deadline:
        _status, body = _get("/v1/support/timeline", token)
        items = body.get("items", [])
        if any(t["role"] == "agent" for t in items):
            return items
        time.sleep(3)
    return items


def agent_text(items: list[dict]) -> str:
    return " ".join(t["text"] for t in items if t["role"] == "agent")


def roles(items: list[dict]) -> list[str]:
    return [t["role"] for t in items]


print("=" * 78)
print(f"修复验证：对着真实 API 走一遍客户旅程（本轮 RUN={RUN}）")
print("=" * 78)

# ---------------------------------------------------------------- 品牌名
print("\n[P1-4] 品牌名不再渲染成 HTML 源码")
session = new_session("fix-brand-1")
brand = session.get("branding", {}).get("display_name", "")
check(
    "display_name 是干净的名字",
    brand == "Acme & Co",
    f"实测 {brand!r}",
)
check(
    "会话响应带上了服务时间",
    "support_window" in session,
    f"support_window={session.get('support_window')!r}",
)

# ---------------------------------------------------------------- 时间线字段
print("\n[P1-3] 时间线每次都返回品牌与服务时间（刷新路径也拿得到）")
token = session["token"]
_status, timeline = _get("/v1/support/timeline", token)
check("timeline 带 branding", bool(timeline.get("branding", {}).get("display_name")), str(timeline.get("branding")))
check("timeline 带 conversation.owner", "owner" in timeline.get("conversation", {}), str(timeline.get("conversation")))
check("timeline 带 support_window", "open" in timeline.get("support_window", {}), str(timeline.get("support_window")))

# ---------------------------------------------------------------- P1-1 短中文
print("\n[P1-1] 中文短问句不再被判「太短」，且用中文回答")
session = new_session("fix-short-1")
token = session["token"]
status, posted = _post("/v1/support/messages", {"text": "交期是多久？"}, token)
check("发送成功", status == 200, f"HTTP {status} {posted}")
check("已排队给 AI", posted.get("status") == "queued", f"status={posted.get('status')!r}")
items = wait_for_agent(token, seconds=90)
reply = agent_text(items)
check(
    "回答是中文（不再是英文澄清话术）",
    any("\u4e00" <= ch <= "\u9fff" for ch in reply[:40]) and "more detail" not in reply,
    f"reply={reply[:110]!r}",
)

# ---------------------------------------------------------------- P0-1 订单卡片
print("\n[P0-1] 中文问订单能取到记录（不再选错工具）")
session = new_session("fix-order-1")
token = verify_identity(session["token"])
status, posted = _post("/v1/support/messages", {"text": "我的订单 SO-9001 到哪了？"}, token)
check("发送成功", status == 200, f"HTTP {status}")
items = wait_for_agent(token, seconds=90)
reply = agent_text(items)
cards = [t for t in items if t["role"] == "tool" and t.get("card")]
check("出现了订单数据卡", bool(cards), f"roles={roles(items)}")
if cards:
    check("卡片是 SO-9001", cards[0]["card"].get("title") == "SO-9001", str(cards[0]["card"].get("title")))
check(
    "回答不再是「系统没有响应」",
    "not responding" not in reply and "没有响应" not in reply,
    f"reply={reply[:140]!r}",
)
check(
    "回答是中文",
    any("\u4e00" <= ch <= "\u9fff" for ch in reply[:60]),
    f"reply={reply[:90]!r}",
)

# ---------------------------------------------------------------- P0-2 转人工后不再静默
print("\n[P0-2] 转人工之后，再提问必须有可见反馈（不再被静默吞掉）")
session = new_session("fix-handoff-1")
token = session["token"]
# Q1: explicit request for a person -> human_required -> handoff + release to queue.
status, posted = _post("/v1/support/messages", {"text": "转人工"}, token)
check("Q1（转人工）发送成功", status == 200, f"HTTP {status}")
wait_for_agent(token, seconds=60)
_status, after_q1 = _get("/v1/support/timeline", token)
owner_after_q1 = after_q1.get("conversation", {}).get("owner")
check(
    "转人工后会话归属不再是 ai",
    owner_after_q1 in ("queue", "human"),
    f"owner={owner_after_q1!r} mode={after_q1.get('conversation', {}).get('mode')!r}",
)

# Q2: a plain knowledge question the AI COULD answer - and used to answer into
# the void after a handoff.
status, posted_q2 = _post("/v1/support/messages", {"text": "常规交期和加急分别是几个工作日？"}, token)
check("Q2（知识问题）发送成功", status == 200, f"HTTP {status}")
check(
    "Q2 不再排队给 AI（避免生成后丢弃）",
    posted_q2.get("status") == "waiting_for_human",
    f"status={posted_q2.get('status')!r}",
)
check(
    "Q2 的响应带回会话归属",
    "owner" in posted_q2.get("conversation", {}),
    str(posted_q2.get("conversation")),
)
time.sleep(6)
_status, after_q2 = _get("/v1/support/timeline", token)
system_turns = [t["text"] for t in after_q2["items"] if t["role"] == "system"]
check(
    "时间线上出现了给客户的可见说明",
    any("人工" in text for text in system_turns),
    f"system turns={system_turns!r}",
)
check(
    "客户的问题本身仍然被保留",
    any(t["role"] == "customer" and "常规交期" in t["text"] for t in after_q2["items"]),
    f"roles={roles(after_q2['items'])}",
)

# ------------------------------------------------------------------ 汇总
print()
print("=" * 78)
print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
for label in FAILED:
    print("  失败：" + label)
print("=" * 78)
sys.exit(1 if FAILED else 0)
