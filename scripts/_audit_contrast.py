"""Audit probe: WCAG 2.2 contrast for the customer surface's palette.

Pairs are (foreground, background) lifted from `apps/admin-web/src/styles-support.css`.
The background is the surface the text actually sits on. For the chat thread that
is `.support-shell`'s `#f7f8f8` - not white, which is what an eyeball check
tends to assume.
"""


def _lin(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def ratio(fg: str, bg: str) -> float:
    l1, l2 = luminance(fg), luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


SHELL = "#f7f8f8"  # .support-shell background

# (元素, 行号, 前景, 背景, 门槛)
PAIRS = [
    ("空状态「还没有对话…」 .support-empty", 60, "#9ca3af", SHELL, 4.5),
    ("副标题」有问题请直接说…」.support-sub", 46, "#6b7280", SHELL, 4.5),
    ("「客服正在输入…」.support-typing", 91, "#6b7280", SHELL, 4.5),
    ("正文/气泡 .support-shell", 18, "#1c1c1e", SHELL, 4.5),
    ("品牌名 h1（默认强调色）", 34, "#1a393d", SHELL, 4.5),
    ("客户气泡白字（默认强调色底）", 78, "#ffffff", "#1a393d", 4.5),
    ("平台通知 .support-system", 104, "#55605f", "#eceeee", 4.5),
    ("卡片小字 .tool-card-kind", 135, "#6b7280", "#ffffff", 4.5),
    ("卡片数据来源标记 .tool-card-demo", 157, "#8a6d1a", "#ffffff", 4.5),
    ("卡片事实行 .tool-card-facts", 237, "#55605f", "#ffffff", 4.5),
    ("错误提示 .support-problem", 251, "#8a1c1c", "#fdecec", 4.5),
    ("核实提示 .support-verify-lead", 271, "#55605f", "#ffffff", 4.5),
    ("「已核实身份」 .support-verified", 320, "#1f5c46", "#e6f0ec", 4.5),
    ("输入框内文字 .support-composer input", 338, "#1c1c1e", "#ffffff", 4.5),
]

print(f"{'元素':<40} {'行':>4} {'前景':<9} {'背景':<9} {'对比度':>7}  判定")
print("-" * 96)
failed = []
for label, line, fg, bg, need in PAIRS:
    r = ratio(fg, bg)
    ok = r >= need
    if not ok:
        failed.append((label, line, r))
    print(f"{label:<40} {line:>4} {fg:<9} {bg:<9} {r:>7.2f}  {'通过' if ok else '**不通过**'}")

print()
print(f"不通过项：{len(failed)} / {len(PAIRS)}")
for label, line, r in failed:
    print(f"  - {label}  (styles-support.css:{line})  对比度 {r:.2f} : 1，需 ≥ 4.5:1")
