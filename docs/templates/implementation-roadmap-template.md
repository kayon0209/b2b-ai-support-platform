# Multi-stage implementation roadmap

## Current status

Stage: [stage name] — [not started / in progress / complete / blocked]

### Stage 1: [name]

- [ ] Pending milestone
- [~] Partially complete milestone; say what remains
- [!] Blocked milestone; name the external dependency
- [x] Complete milestone; cite validation evidence

### Stage 2: [name]

- [ ] Pending milestone

## Progress rules

- Keep milestones small enough to update when code or validation changes.
- `[x]` means implementation and its required verification are complete.
- `[~]` means a stated portion is complete; the remaining portion stays explicit.
- `[!]` means blocked by an external dependency; explain what would unblock it.
- `[ ]` means not started.
- Update the status after each milestone and report changes, evidence, and blockers in the active Codex chat.
- Progress percentages are checklist-weighted: complete = 1, partial = 0.5, pending/blocked = 0. They are not time or effort estimates.
- Only advance to the next stage when the current stage's acceptance criteria are met.

Run a one-time summary:

```bash
python3 scripts/roadmap_progress.py docs/implementation/production-agent-roadmap.md
```

Watch live checklist edits (stop with Ctrl-C):

```bash
python3 scripts/roadmap_progress.py docs/implementation/production-agent-roadmap.md --phase 1 --watch
```

By default the watcher shows a stale marker after five minutes without a
roadmap edit. That marker means only that no checklist update was recorded; it
cannot infer hidden work or whether Codex is still reasoning. Update the
roadmap at each milestone so the display reflects actual progress. On macOS,
add `--notify-stale` to send one desktop notification for each stale period.
