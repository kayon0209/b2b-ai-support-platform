#!/usr/bin/env python3
"""Display checklist progress for a staged implementation roadmap.

Usage:
  python3 scripts/roadmap_progress.py docs/implementation/production-agent-roadmap.md
  python3 scripts/roadmap_progress.py \
    docs/implementation/production-agent-roadmap.md --phase 1 --watch

The percentage is checklist-weighted, not an estimate of elapsed time or
engineering effort. Completed items count as 1, partial items as 0.5, and
pending/blocked items as 0. The watcher rereads the Markdown file each refresh.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

STAGE_HEADING = re.compile(r"^##\s+(阶段[^#]+?)\s*$")
CHECKBOX = re.compile(r"^\s*-\s+\[([ xX~!])\]\s+(.+?)\s*$")
STATUS_WEIGHT = {"x": 1.0, "~": 0.5, " ": 0.0, "!": 0.0}
BAR_WIDTH = 24


@dataclass(frozen=True)
class StageProgress:
    title: str
    complete: int
    partial: int
    pending: int
    blocked: int

    @property
    def total(self) -> int:
        return self.complete + self.partial + self.pending + self.blocked

    @property
    def weighted_complete(self) -> float:
        return self.complete + self.partial * STATUS_WEIGHT["~"]

    @property
    def percent(self) -> float:
        return self.weighted_complete * 100 / self.total if self.total else 0.0


def parse_roadmap(path: Path) -> list[StageProgress]:
    stages: list[dict[str, int | str]] = []
    current: dict[str, int | str] | None = None

    def finish_stage() -> None:
        nonlocal current
        if current is not None:
            stages.append(current)
            current = None

    for line in path.read_text(encoding="utf-8").splitlines():
        heading = STAGE_HEADING.match(line)
        if heading:
            finish_stage()
            current = {
                "title": heading.group(1),
                "complete": 0,
                "partial": 0,
                "pending": 0,
                "blocked": 0,
            }
            continue
        if line.startswith("## "):
            finish_stage()
            continue
        if current is None:
            continue

        checkbox = CHECKBOX.match(line)
        if checkbox is None:
            continue
        state = checkbox.group(1).lower()
        key = {"x": "complete", "~": "partial", "!": "blocked"}.get(state, "pending")
        current[key] = int(current[key]) + 1

    finish_stage()
    return [
        StageProgress(
            title=str(stage["title"]),
            complete=int(stage["complete"]),
            partial=int(stage["partial"]),
            pending=int(stage["pending"]),
            blocked=int(stage["blocked"]),
        )
        for stage in stages
    ]


def _bar(percent: float) -> str:
    filled = round(BAR_WIDTH * percent / 100)
    return f"[{'#' * filled}{'.' * (BAR_WIDTH - filled)}] {percent:5.1f}%"


def _line(stage: StageProgress) -> str:
    return (
        f"{stage.title}: {_bar(stage.percent)}  "
        f"done {stage.complete}, partial {stage.partial}, "
        f"pending {stage.pending}, blocked {stage.blocked}"
    )


def _age_label(seconds: float) -> str:
    whole_seconds = max(0, int(seconds))
    minutes, remainder = divmod(whole_seconds, 60)
    return f"{minutes}m {remainder:02d}s" if minutes else f"{remainder}s"


def _notify_stale() -> None:
    """Send one fixed macOS notification when explicitly requested."""
    if sys.platform != "darwin":
        return
    subprocess.run(
        [
            "/usr/bin/osascript",
            "-e",
            'display notification "No roadmap update recorded; check the current blocker." '
            'with title "Codex progress monitor"',
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def render(path: Path, *, phase: int | None = None) -> str:
    stages = parse_roadmap(path)
    if not stages:
        raise ValueError("no '## 阶段…' sections with checklist items were found")
    if phase is not None and not 1 <= phase <= len(stages):
        raise ValueError(f"phase must be between 1 and {len(stages)}")

    selected = stages if phase is None else [stages[phase - 1]]
    lines = [f"Roadmap: {path}"]
    lines.extend(_line(stage) for stage in selected)
    overall = StageProgress(
        title="Overall",
        complete=sum(stage.complete for stage in stages),
        partial=sum(stage.partial for stage in stages),
        pending=sum(stage.pending for stage in stages),
        blocked=sum(stage.blocked for stage in stages),
    )
    lines.append(_line(overall))
    lines.append("Checklist-weighted only; [~] counts as half. No time/ETA estimate.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roadmap", type=Path)
    parser.add_argument("--phase", type=int, help="show one 1-based stage in addition to overall")
    parser.add_argument(
        "--watch", action="store_true", help="refresh while the Markdown file changes"
    )
    parser.add_argument(
        "--interval", type=float, default=2.0, help="watch refresh interval in seconds"
    )
    parser.add_argument(
        "--stale-after",
        type=float,
        default=300.0,
        help="show a stale marker after this long without a roadmap file edit",
    )
    parser.add_argument(
        "--notify-stale",
        action="store_true",
        help="send one macOS notification when a roadmap becomes stale",
    )
    args = parser.parse_args()

    if args.interval <= 0:
        parser.error("--interval must be greater than zero")
    if args.stale_after <= 0:
        parser.error("--stale-after must be greater than zero")
    try:
        if not args.watch:
            print(render(args.roadmap, phase=args.phase))
            return 0
        notified_mtime: float | None = None
        while True:
            if sys.stdout.isatty():
                print("\033[2J\033[H", end="")
            now = time.time()
            last_edit = args.roadmap.stat().st_mtime
            age = max(0.0, now - last_edit)
            print(datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"))
            if age >= args.stale_after:
                print(
                    f"NO ROADMAP UPDATE for {_age_label(age)}. "
                    "Checklist unchanged; unrecorded work is not detectable."
                )
                if args.notify_stale and notified_mtime != last_edit:
                    _notify_stale()
                    notified_mtime = last_edit
            else:
                print(f"Last roadmap update: {_age_label(age)} ago")
                notified_mtime = None
            print(render(args.roadmap, phase=args.phase), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
