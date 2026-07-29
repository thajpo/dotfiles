# Investigation guide

Use only read-only commands, bounded to the question and repository. Prefer
explicit limits such as `-n 20` and a recent time window. Do not fetch or
inspect unrelated repositories.

## Safe evidence collection

```sh
git status --short --branch
git log -n 20 --date=iso --format='%h %ad %an %s' --decorate
git log -n 20 --stat --oneline
git branch -vv --all
git worktree list --porcelain
git show --stat --oneline <commit-or-ref>
git reflog -n 20 --date=iso <ref>
```

Read explicitly named plans, issues, notes, and target documents. Treat commit
subjects and branch names as statements about intent only when attributed as
such; they are not proof of completion. Avoid commands that update refs or
working state, including fetch, checkout, reset, stash, clean, commit, merge,
rebase, branch/worktree creation or deletion, and push.

## Bounded fresh-context angles

When the request is broad and fanout is available, use no more than three
read-only children. Give each only the repository/question/window and ask for
facts with command or path citations:

1. **Recent history / intent** — identify recent change clusters, explicit
   goals in messages or named documents, and a clearly labeled direction
   inference. Do not call the direction a plan unless written explicitly.
2. **Attempts / worktrees** — inventory visible branches and worktrees,
   upstreams, divergence, dirty paths, and recent tip evidence. Report
   candidate evidence and blockers; do not label anything complete,
   abandoned, or resumable as a fact.
3. **Gaps / directions** — compare observed changes and tests with the explicit
   target (if any), list missing evidence or unresolved seams, and suggest
   bounded next investigations. Mark every suggestion as inference.

Children must not write files, mutate Git/runtime state, inspect arbitrary
session transcripts, or launch further agents. If fanout is unavailable,
perform the needed angles sequentially and disclose that limitation.
