---
name: git-cleanup
description: "Draft a concise commit message, then safely commit with a cross-machine handoff packet."
---

# Git Cleanup

> Pisec routing: this skill is for generic GitHub workflows only; do not use it for a Pisec-managed workstream. Use the Pisec Secretary and brokered acceptance path instead.

## Mode
- `draft-then-ship` (default and only mode for now).

## Goal
Turn local edits into a safe, reviewable commit and handoff you can resume on another machine.

## Default Preference (Dotfiles)
- When edits are in your dotfiles/config repos, commit in the same session when the user requests the change.
- Push only when the user explicitly requests a push.

## Pisec Exception
- A bounded Pisec worker is not a PR workstream. Its `ready_review` packet is
  evidence for the secretary and does not imply acceptance.
- The user accepts a bounded candidate once; after that, the Pisec secretary
  owns target refresh, reconciliation, verification, fast-forward integration,
  completion, retirement, and cleanup. Do not request a second merge approval.
- Do not use this cleanup skill to push, open a PR, delete a Pisec worker
  branch, or bypass the brokered acceptance/integration records.

## Inputs
- Optional commit intent (short sentence).
- Optional test command (run before commit when provided).
- Optional target remote (default: tracked remote, else `origin`).

## Preconditions
- Must be inside a git worktree.
- Must have staged or unstaged changes (no empty commit).
- Must have a named branch (not detached HEAD), or escalate.

## Worktree + Branch Policy
1. Worktree awareness
- Detect current path and active worktree from `git worktree list --porcelain`.
- Operate only in the current worktree.
- Never prune, move, or remove worktrees.

2. Branch safety
- If on `main`/`master`: ask one explicit question before committing.
- Recommended option: create and switch to a feature branch, then continue.
- If detached HEAD: hard-pause and ask to create a branch or abort.

3. Upstream state
- If branch has no upstream: push with `git push -u <remote> <branch>`.
- If branch is behind or diverged from upstream: hard-pause and ask rebase/merge choice.

## Procedure (`draft-then-ship`)
1. Inspect state
- Run `git status --short --branch`.
- Run `git diff` and `git diff --staged`.
- Run `git log --oneline -n 8` for message style.

2. Draft commit message
- Produce a 1-2 sentence message focused on why.
- Do not ask for commit-message approval; use the drafted message after the staged diff has been reviewed or the user has explicitly requested the commit.

3. Stage safely
- Stage relevant tracked/untracked files.
- Do not stage likely secret files (`.env`, `*.pem`, `credentials*.json`, key files).
- If suspicious files are detected, hard-pause and ask explicit include/exclude decision.

4. Validate
- If input test command exists, run it and report result.
- If tests fail, hard-pause (do not commit).

5. Commit and optionally push
- Create the commit with the drafted message.
- Push only when explicitly requested, using `-u` when the upstream is missing.
- Never force push.

6. Verify and handoff
- Run final `git status --short --branch`.
- Return handoff packet.

## Hard Rules
- Never use destructive git commands (`reset --hard`, forced checkout restore).
- Never amend unless user explicitly requests amend.
- Never force push.
- Never commit when working tree has no relevant changes.
- Never include likely secrets by default.

## Required Return (Handoff Packet)
- `repo_path`
- `worktree_path`
- `branch`
- `remote`
- `commit_sha`
- `commit_message`
- `push_status`
- `working_tree_clean` (true/false)
- `resume_on_mac` commands:
  - `git fetch --all --prune`
  - `git switch <branch>`
  - `git pull --ff-only`

## Escalation Rules
- Ask exactly one explicit decision question when blocked.
- Include: blocker, attempted command, key output, options/tradeoffs, recommended default.
- Hard-pause until user decision.

## Suggested Commands
- `Use $git-cleanup draft-then-ship`
- `Use $git-cleanup draft-then-ship message: "..."`
- `Use $git-cleanup draft-then-ship test: "pytest -q"`
