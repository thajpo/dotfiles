---
name: worktree-manager
description: "Manage one-issue-per-worktree lifecycle for branch isolation, PR traceability, and predictable cleanup."
---

# Worktree Manager

> Pisec routing: this skill is for generic GitHub workflows only; do not use it for a Pisec-managed workstream. Use the Pisec Secretary and brokered acceptance path instead.

## Pisec Exception
Pisec worker worktrees are broker-owned bounded execution surfaces, not PR
worktrees. Do not create a PR, request a second merge approval, delete the
branch, or run generic cleanup after a worker reports ready review. The user
accepts the bounded candidate once; the project secretary owns refresh,
reconciliation, verification, fast-forward integration, retirement, and
cleanup through Pisec.

## Usage
- Internal helper skill.
- Normally invoked by `$pr-iterate`, not directly by user.

## Goal
Enforce isolated execution: one approved issue maps to one branch, one worktree, one PR.

## Contract
- `1 issue -> 1 branch -> 1 worktree -> 1 PR`
- no shared worktree across multiple active issues
- no mixed-scope commits in a single worktree
- no implementation before mapping verification succeeds

## Naming
- worktree directory should include issue/spec id
- branch should include issue/spec id with `codex/` prefix
- naming may vary by repo as long as mapping is unambiguous

## Lifecycle
1. Create isolated worktree from default branch.
2. Verify mapping (`issue`, `branch`, `worktree`) and verify current checkout is the mapped branch.
3. Implement and open PR from that branch only.
4. After merge, remove worktree and delete local branch.

Use command patterns in `references/commands.md`.

## Guardrails
- Never reuse an active worktree for a different issue.
- If urgent hotfix is needed, create a separate worktree.
- Cleanup happens only after merge/abandonment is confirmed.
- If current branch/worktree is not mapped to the issue, stop as `blocked` and do not edit files.

## References
- `references/commands.md`
