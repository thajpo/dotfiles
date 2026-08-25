---
name: issue-handoff
description: "Create one GitHub issue from one ready Specd contract, preserve the full contract in the issue, and compact current.md to issued state."
---

# Issue Handoff

> Pisec routing: this skill is for generic GitHub workflows only; do not use it for a Pisec-managed workstream. Use the Pisec Secretary and brokered acceptance path instead.

## Usage
- Internal helper skill.
- Normally invoked by `$pr-iterate`, not directly by user.

## Goal
Convert a single `Specd: ready` item into an executable GitHub issue with no contract drift.

## Preconditions
- Item exists in `Specd` with status `ready`.
- Full schema is present, including `user intent` and `file touch scope`.
- Manual approval evidence exists in markdown.

## Procedure
1. Validate required fields and readiness evidence.
2. Render issue body from the approved contract (semantically identical).
3. Create exactly one issue for that one item.
4. Update planning file by compacting the item to:
- `<id/title> | status: issued | issue: <url-or-#>`

## Hard Rules
- Never batch multiple ready items into one issue.
- Never start implementation before issue exists.
- Never alter contract meaning during issue transcription.
- If fields are missing, stop and return blockers.

## Output Contract
- issue URL/number
- final issue title/body summary
- recommended issue branch name
- recommended worktree path
- compact tracker line applied to planning file
- blocker packet (if blocked)

## References
- `references/issue-body-checklist.md`
