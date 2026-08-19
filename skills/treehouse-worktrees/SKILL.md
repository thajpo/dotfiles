---
name: treehouse-worktrees
description: "Safely lease and return personal Git worktrees through the fenced Treehouse CLI."
---

# Treehouse Worktrees

Treehouse is available only in a personal fenced OMP session. Never use it in a
Pisec secretary or worker session; those sessions use their broker-owned
workspace lifecycle.

## Before acquiring

1. Run `treehouse --version` and require the pinned version reported by the
   environment.
2. Run `treehouse status --json` and inspect the pool state.
3. Work from the repository's canonical main checkout, not from a linked
   worktree. Verify that the current directory is canonical and that Git's
   common directory resolves inside the allowed current-working-directory
   boundary. If the current directory is a linked worktree or the common Git
   directory is outside that boundary, stop and direct the user to open the
   main checkout; never widen the Fence scope.
4. Choose a stable, descriptive lease holder for this session.

Do not place acquisition in command substitution, a shell pipeline, or a
subshell. Run the command directly so its JSON result is retained as evidence:

```text
treehouse get --lease --lease-holder <holder> --json
```

Retain the returned `path`, `lease_id`, and `lease_holder` exactly. Do not
reconstruct a path or lease identity from pool state.

## While leased

- Use only the returned worktree path.
- Keep the lease identity with the task evidence.
- Treat the four-worktree per-profile cap as a hard limit. Do not add a
  repository-local Treehouse configuration to bypass it.
- Commit or remove intended changes before returning the worktree. Inspect the
  leased worktree itself with `git -C <path> status --short`; it must be clean
  before release.

## Returning

After the clean-worktree check, return only with both retained identity values:

```text
treehouse return --if-lease-id <lease_id> --if-lease-holder <lease_holder> <path>
```

A conditional return is required. A stale or repeated conditional return must
fail rather than release another session's lease. Never use an unconditional or
forced return, and never return a path or identity that was not produced by the
current lease command.

## Prohibited operations

Never use bare acquisition or lease commands without `--lease` and
`--lease-holder`; `enter`; `prune`; `destroy`; `init`; `update`; or any command
that edits repository-local Treehouse configuration. Never widen the personal
Fence to reach an external Git common-object directory.
