# Local Skills Notes

## Workflow Rule
- Any time we edit dotfiles/config repos, we should commit and push in the same session.
- Preferred flow: `Use $git-cleanup draft-then-ship`.
- If push should be skipped for a specific case, explicitly state that in the request.

## Pisec Exception
- Pisec worker tasks use one bounded local acceptance followed by secretary-owned
  refresh, reconciliation, verification, fast-forward integration, and cleanup;
  they do not use a PR or a second merge approval.
- A worker ready-review packet is evidence, not acceptance. Keep target/final
  commit OIDs as refreshable integration state and preserve immutable task,
  candidate, path, check, and conflict-policy scope.

## Active Workflow Skills (current)
- User-facing:
  - `repo-init`: one-time repo bootstrap (planning + templates + baseline CI).
  - `lean-flow`: chat -> issue -> optional temporary plan -> PR; no planning tracker.
  - `pr-status`: open-PR board (`function + turn owner + priority + current.md linkage`).
  - `pr-iterate`: execution workflow (`ready/issued -> issue/PR loop -> merge-ready`).
- Internal helpers (auto-used by `pr-iterate`/`repo-init`):
  - `spec-gate`
  - `issue-handoff`
  - `worktree-manager`
  - `pr-scope-guard`
  - `ci-baseline`

## Approval Policy
- Explicit chat approval is enough unless repository policy says otherwise.
- Feature work maps to one independently reviewable GitHub issue.
- Small obvious fixes may use a direct focused PR when no product decision is hidden.
- Issue defines approved `file touch scope`; out-of-scope touches require explicit user approval.
- After PR creation, PR thread/review feedback is the iteration source of truth.
- `$pr-iterate` must fail closed if any feedback channel fetch fails (comments, mentions, reviews, inline comments, threads).

## Merge Logging Policy
- Delete temporary planning files before merge after their useful decisions and
  evidence are captured in the PR description.

## Command Surface
Use only these in normal flow:
1. `Use $repo-init` (once per repo)
2. `Use $lean-flow ...` (turn selected work into an issue or implementation plan)
3. `Use $pr-status` (open PR board with turn ownership + priorities)
4. `Use $pr-iterate "<spec-id-or-pr#>"` (issue + PR + feedback loop)
   - `$pr-iterate` performs commit + push updates by default; you review and decide merge.

## Why
- Keeps Linux and Mac in sync.
- Reduces context loss when switching machines.
- Leaves a clear rollback point via commit SHA.
