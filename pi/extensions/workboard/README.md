# Pi workboard

A thin, repository-local task dashboard over the stable `pi-side-agents` tool contract. It tracks intent, decisions, contracts, worker associations, and review ranges; it does not own worker processes, worktrees, merges, PRs, or scheduling. Normal questions and coding conversation still go directly to the parent model—`/work` is the control surface, not a chat wrapper.

## Commands

```text
/work
/work new [--id ID] <description>
/work scout <question>
/work decide <question>
/work resolve <number> <resolution>
/work contract
/work implement
/work review
/work send <worker-label-or-id> <message>
/work use <task-id>
/work close
/work help
```

## State and artifacts

- Runtime state: `.pi/workboard/state.json` (local/untracked)
- Runtime lock: `.pi/workboard/state.lock`
- Durable artifacts: `.agent/tasks/<TASK-ID>/`
  - `intent.md`
  - `decisions.md`
  - `contract.yaml`
  - `reports/*.md` on scout branches
- Configuration: `~/.pi/agent/workboard.json`

The extension stores references to side-agent IDs, branches, worktrees, and tmux windows. Live process status remains authoritative in `.pi/side-agents/registry.json` and is reconciled whenever `/work` runs.

## Safety and lifecycle

- Commands require a trusted Git repository.
- `/work new` creates missing local lifecycle files automatically, infers the integration branch, and never overwrites existing lifecycle files.
- Generated `.pi/side-agent-*` files remain local/untracked; optional project bootstrap belongs in `.pi/side-agent-bootstrap.sh`.
- Scouts may write only their expected report artifact and are told not to merge.
- `/work implement` refuses to run without a non-empty approved contract.
- Implementation contracts are embedded into the worker handoff, so untracked parent artifacts remain available inside isolated worktrees.
- `/work review` resolves the worker branch and merge base, then asks the parent to audit the actual range.
- `/work close` refuses while workers are active.
- No command automatically merges, pushes, publishes, approves, deletes a branch, or removes a worktree.

Dispatch and send operations ask the parent model to call the stable `agent-start`/`agent-send` tools exactly once. Pi currently exposes tool metadata but not a direct cross-extension tool-execution API; this avoids wrapping undocumented tmux internals or patching `pi-side-agents`.
