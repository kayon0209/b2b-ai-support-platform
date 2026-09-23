"""Audit probe: does today's customer-visibility change meet yesterday's
human-reply module?

Two pieces of uncommitted work sit on opposite sides of one fact:

* `agent_reply.send_agent_reply` (pre-existing, untracked) *sets* the lease to
  `human` when a person answers - "the lease is the only mechanism that
  prevents" the AI speaking over them.
* `lease_service.current_owner` (new today) *reads* that lease so the customer
  window can say who has the conversation, instead of going silent.

Either side working alone is not enough: a lease that transfers without the
customer being told, or a customer-facing state bar that never sees a transfer.
This walks the seam - open a customer session, let a person reply through the
operator endpoint, then read the *customer's* timeline back.

Run from the project root (API must be up):
    .venv/Scripts/python.exe scripts/_audit_probe_agent_reply_integration.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
import uuid

BASE = "http://127.0.0.1:8000"
# The seeded `admin-demo` owner. A real deployment resolves this from OIDC; the
# probe borrows the bootstrap token for the same reason every other probe here
# does.
TOKEN = "pt_admin-demo_8c89893c-09ce-4252-b839-971ac15e9a07"  # noqa: S105 - the seeded demo token, not a secret
TIMEOUT = 30

FAILED = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    if not ok:
        FAILED += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"\n        {detail}" if detail else ""))


def call(
    method: str, path: str, body: dict | None = None, token: str | None = None
) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if method != "GET":
        headers["Idempotency-Key"] = str(uuid.uuid4())
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)  # noqa: S310 - BASE is a hardcoded http://127.0.0.1 URL
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 - BASE is a hardcoded http://127.0.0.1 URL
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


RUN = uuid.uuid4().hex[:8]

print("=" * 78)
print(f"集成点：人工回复（既有模块） → 客户可见状态（今天新增）  RUN={RUN}")
print("=" * 78)

# 1. A customer opens a session and asks something.
status, session = call(
    "POST",
    "/v1/support/sessions",
    {"tenant_slug": "admin-demo", "visitor_id": f"reply-seam-{RUN}"},
)
assert status == 200, (status, session)
customer_token = session["token"]
conversation_ref = session["conversation_ref"]
print(f"\n[1] 客户会话 {conversation_ref[:8]} 已开启")

status, posted = call("POST", "/v1/support/messages", {"text": "转人工"}, customer_token)
assert status == 200, (status, posted)
print(f"[2] 客户发言完成，response.status = {posted.get('status')!r}")

_status, before = call("GET", "/v1/support/timeline", token=customer_token)
owner_before = before.get("conversation", {}).get("owner")
print(f"[3] 客户视角 owner（人工回复前）= {owner_before!r}")

# 2. A person answers through the operator endpoint. This is what transfers the
#    lease to `human`.
status, reply = call(
    "POST",
    f"/v1/conversations/{conversation_ref}/replies",
    # `origin` is an allowlist (`models.KNOWN_ORIGINS`: "", "free", "canned",
    # "ai_suggestion"), and a value outside it is refused rather than stored -
    # an unrecognised origin would quietly dilute the adoption rate it exists
    # to measure. A person typing by hand is ORIGIN_FREE.
    {"text": "您好，我是人工同事，已经接手这条对话，正在为您核实。", "origin": "free"},
    token=TOKEN,
)
print(
    f"[4] 人工回复 POST → HTTP {status}"
    + (f"  {reply.get('error', {}).get('code', '')}" if status != 200 else "")
)
check("人工回复被接受", status == 200, json.dumps(reply, ensure_ascii=False)[:200])

# 3. Read the customer's own timeline back.
_status, after = call("GET", "/v1/support/timeline", token=customer_token)
owner_after = after.get("conversation", {}).get("owner")
mode_after = after.get("conversation", {}).get("mode")
texts = [t.get("text", "") for t in after.get("items", [])]
print(f"[5] 客户视角 owner（人工回复后）= {owner_after!r} mode={mode_after!r}")

check(
    "人工回复把租约交给了 human（既有模块生效）",
    owner_after == "human",
    f"owner={owner_after!r}",
)
check(
    "人工的回复出现在客户时间线上（客户能看到）",
    any("人工同事" in t for t in texts),
    f"items={len(texts)}",
)

# 4. And the customer surface must now say who has it, rather than waiting.
print("\n[6] 客户面此时应显示「已有同事接手这条对话，他们会看到您发的消息。」")
print("    —— 断言由浏览器探针承担；这里只确认驱动它的 owner 值正确。")
check(
    "前端据此渲染状态条所需的 owner 值已就位", owner_after in ("human", "queue"), str(owner_after)
)

print()
print("=" * 78)
print("失败项：", FAILED)
print("=" * 78)
sys.exit(1 if FAILED else 0)
