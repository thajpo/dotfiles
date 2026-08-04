# Secretary and worker workflow

This document describes the checked-in Pi topology. It is a description of the
launcher and extension contracts, not a replacement for host policy or user
approval.

## The three conversations

### 1. The normal parent

A normal `pi` session is the primary decision-maker. The user gives it a goal;
the parent chooses the work mode, keeps the current task packet, decides scope,
launches bounded children, runs acceptance checks, and inspects the final diff.
The normal parent has Pi's standard file/search/shell tools plus the configured
`pi-subagents` orchestrator and the global workflow extensions. The parent is
the only normal session that owns the global task packet; child sessions do not
register the `task_packet` tool.

### 2. The project secretary

`pi-start secretary` or `pisec` opens one persistent secretary per registered
project. The secretary is a project switchboard, not a coding worker. It can
inspect the project, inspect bounded Git state, maintain bounded project notes,
observe workstream attention, and create or focus explicitly authorized full
workstreams.

The secretary can see the project's registered workstreams through
`secretary_list_workstreams` and their attention events through
`secretary_list_attention`. This is not a promise that it sees every arbitrary
process or every unregistered branch; unregistered Git state is inspected with
`secretary_git`.

### 3. A full worker

A worker is the single writer for one assigned task worktree. The secretary
creates it through the semantic workstream controller; the normal parent may
also launch an explicitly assigned worker through `pi-subagents`. The worker
edits, runs tests, and commits in its assigned execution plane. It may use
`subagent` for bounded, headless, asynchronous investigation, but the nested
fanout boundary only exposes read-only agents and strips their shell/write/edit,
worktree, persistence, and further-spawn capabilities.

## Exact secretary launcher boundary

`bin/pi-secretary` starts Pi with `--no-extensions`, `--no-skills`,
`--no-context-files`, and `--no-prompt-templates`, then explicitly loads only:

- `extensions/secretary/index.ts`;
- `extensions/root-session/index.ts`;
- `extensions/auto-continue/index.ts`;
- `extensions/secretary-subagents/index.ts`;
- `extensions/fast-mode/index.ts`.

Its fixed tool allowlist is:

```text
read, grep, find, ls, web_search, fetch_content, get_search_content, source_check,
host_command, subagent,
secretary_git, secretary_git_write, secretary_git_cleanup,
secretary_record_idea, secretary_create_workstream,
secretary_open_workstream, secretary_relaunch_workstream,
secretary_list_workstreams,
secretary_list_attention, secretary_acknowledge_attention,
secretary_create_reviewer, secretary_land_reviewed,
secretary_create_integration, secretary_cleanup_workstream
```

It intentionally does not expose `bash`, `write`, or `edit`. Child secretary
investigators receive an even smaller hardened list:

```text
read, grep, find, ls, web_search, fetch_content, get_search_content,
source_check, secretary_git, contact_supervisor, intercom, host_command
```

They receive no write/edit/shell/subagent tool, no ordinary extensions, and no
Git worktree; web access is loaded only through the explicit read-only search
extension. The secretary's normal read tools are a tool boundary, not a
container security boundary: the secretary launcher does not load the
container-sandbox extension. The host controller and explicit extension list
are therefore part of the authority boundary.

## Secretary presentation backends and Herdr surface

The secretary/workstream system has two selectable presentation backends:

- **tmux (default):** `pisec` and `pi-start secretary` keep the existing
  `pisec` grid and launch full workstreams with `pidev`/tmux.
- **Herdr (opt-in):** `pi-secretary --herdr` uses the dedicated Herdr session
  `pi-secretary`. It creates one project Space per registered project and
  launches new full workstreams as guarded Herdr panes in that Space.

The active surface selects the backend; it is not a global replacement. A
runtime record pins the backend, so opening a tmux workstream from Herdr (or
vice versa) refuses rather than migrating or killing the existing worker.
Switch surfaces only after stopping the current surface, and do not run both
surfaces for one project. Existing tmux workers are never automatically
migrated.

The Herdr command starts the same guarded
`bin/pi-secretary --internal-launch --project-id <id>` process used by `pisec`.
It never starts a bare or unguarded `pi`. A Herdr workstream uses the checked-in
`bin/pi-herdr-workstream` wrapper, which verifies the exact project,
workstream, assigned branch/worktree, session ID, brief, and backend before
calling the normal guarded `bin/pi` route. It does not call `pidev` or nest
tmux inside Herdr. Runtime records retain the Herdr session, Space, tab,
secretary pane, and worker pane IDs. Launch and cleanup re-query those exact
IDs and fail closed on a missing, mismatched, unguarded, or ambiguous process.
Attention events still use the existing workstream channel and controller.

Herdr server restore is deliberately native-agent-safe:

```toml
[session]
resume_agents_on_restore = false
```

Herdr's native Pi restore command is `pi --session ...`, which would bypass the
secretary/worker wrappers and their fixed boundaries. After a Herdr server
restart, `pi-secretary --herdr` restores idle shells and relaunches only
through the guarded wrappers. Herdr workstream recovery is automatic only for
records already pinned to Herdr; a tmux-pinned record is left untouched. The
dedicated config is separate from the normal Herdr default session, and the
command does not install the optional Herdr Pi integration automatically.

Use `pisec` or `pi-start secretary` for the tmux surface. Before stopping a
surface, finish or explicitly stop its secretary/worker processes through their
normal controls; do not kill a live Pi worker and assume its state is
recoverable. A surface stop never migrates a live worker, and a worker pinned to
the old backend remains unavailable from the new surface until its explicit
relaunch procedure is completed. To switch from Herdr to tmux:

```bash
herdr session stop pi-secretary
pisec
```

To switch from tmux to Herdr, stop the tmux secretary surface first, then run
`pi-secretary --herdr`. The per-project secretary lock is the final ownership
check; a failed switch leaves the existing surface and worker untouched.

To explicitly relaunch one existing tmux worker into Herdr (never as part of
ordinary startup):

1. In the tmux worker pane, let the worker finish or exit normally; do not kill
   a live Pi process. Save/close any separate editor pane in that exact tmux
   workstream window.
2. Verify the exact tmux workstream window from the runtime record and close
   that now-idle window. The relaunch controller refuses an existing window,
   live process, uncertain socket, or process still using the assigned
   worktree; it never guesses or kills through uncertainty.
3. Stop the tmux secretary surface and release its project lock:
   `tmux kill-session -t pisec` (or exit the exact `pisec` client without
   disturbing unrelated tmux sessions), then run `pi-secretary --herdr`.
4. In the Herdr secretary for that project, explicitly say
   `relaunch <exact-workstream-id> into Herdr`. The exact-target authorization
   calls `secretary_relaunch_workstream`; it preserves the existing workstream
   and Pi session identity, creates a Herdr worker pane, and starts only the
   guarded `pi-herdr-workstream` wrapper.

If any verification fails, leave the tmux worker and runtime record unchanged
and resolve the stale/live state first.

### Herdr UX vocabulary and navigation

- **Space/workspace** = the project/context container.
- **Tab** = a layout within a Space.
- **Pane** = one terminal.
- **Agent** = the process running in a pane.

The current one-secretary-per-project layout makes a current-Space **Agents**
filter redundant: each Space initially has exactly one secretary. The useful
model is for a project Space to contain its secretary plus one or more Herdr
workstream agents; the Agents view then selects an exact process, not merely a
project.

Herdr's default prefix is **Ctrl-b**. Use `Ctrl-b w` for the Spaces/workspace
picker, `Ctrl-b h/j/k/l` to move between panes, `Ctrl-b c` and
`Ctrl-b n/p` for tabs, and `Ctrl-b b` to toggle the sidebar. `Ctrl-b q` detaches
from Herdr; `Ctrl-b ?` opens key help. Press the prefix twice (`Ctrl-b
Ctrl-b`) to send a literal prefix to the focused Pi pane. These are Herdr
bindings, not Pi Backspace bindings.

### Backspace diagnostic

A single Backspace deleting two characters is the known outer-Kitty
press/release compatibility bug, not a secretary binding. Kitty 0.31/0.32 can
send a normal `CSI 127 u` press and a release that arrives as another raw
`DEL`; the old outer terminal cannot distinguish the release, while newer
Kitty releases fixed the protocol issue. The observed host is Kitty 0.32.2;
upgrade the outer Kitty to **0.48.1 or newer** (current release preferred), then
restart Kitty and the Herdr server. Check the path with:

```bash
kitty --version
herdr --version
HERDR_DEBUG_OSC_EVIDENCE=1 herdr --session pi-secretary api snapshot
```

The repository does not remap Pi's Backspace and does not edit Kitty
configuration automatically. If the problem remains after the upgrade, save
the diagnostic output and report the outer terminal plus the exact key path;
changing Pi's binding would hide the transport problem rather than fix it.

## Secretary tools

### Project inspection, feedback, and notes

- `read`, `grep`, `find`, `ls`: inspect project evidence.
- Child workers and investigators report blocked capability requests, risks, and
  suggestions through the parent supervisor intake. The parent decides whether
  to answer, reject, or promote the request; no feedback message grants
  authority by itself.
- `secretary_record_idea`: write a bounded brief into the secretary state store
  outside the repository. It requires explicit record/note authorization and is
  the durable path for an accepted agent suggestion.
- `secretary_list_workstreams`: list validated persistent full-agent records.
- `secretary_list_attention`: list unacknowledged worker attention events.
- `secretary_acknowledge_attention`: acknowledge one exact event after explicit
  user instruction.

### Read-only Git

`secretary_git` calls the host-owned controller with a registered project ID.
It supports bounded `status`, `log`, `diff`, `show`, `branch`, `rev-parse`,
`remote`, `tag`, and `worktree` reads. It rejects repository overrides,
configuration injection, external diff/textconv, output redirection, and
mutating forms such as branch deletion or worktree creation.

### Existing Git write exception

`secretary_git_write` is a separate parent-only tool. It supports only:

- `commit` of an explicit relative path list;
- `push` to the existing `origin` and the current branch;
- `commit-and-push`.

Commit/push requires explicit current-turn language and does not inherit a
plain `yes`. This is a controlled host bridge, but it is important to be
precise: although the secretary has no file-edit tool, this exception can
commit existing source changes and push them. It is not equivalent to a strict
no-source-mutation policy.

### Structured Git cleanup

`secretary_git_cleanup` has two operations:

1. `plan`: validate an exact proposed plan and return the canonical plan,
   actions, counts, and a plan hash without applying it;
2. `apply`: repeat the exact plan and hash after explicit current-turn apply
   authorization.

The plan accepts only:

- renames from `benchmark/*` or `side-agent/*` to `feature/*` with an expected
  commit OID;
- deletions of exact `benchmark/*` or `side-agent/*` branches with expected
  OIDs;
- exact worktree paths under the configured managed worktree root, with their
  expected branch and OID;
- exact regular Pi artifact files in the known subagent/workflow namespaces,
  with an expected SHA-256 digest.

The controller revalidates repository/common-directory identity, policy,
protected branches, OIDs, destination absence, worktree registration, clean
status, active-session/process use, artifact ownership, and artifact hashes.
It holds the common-directory Git lock while applying, removes only clean
worktrees without `--force`, and updates refs with compare-and-swap semantics.
It never accepts arbitrary Git arguments, source paths, remote operations,
pushes, force deletion, or wildcard deletion. The secretary must first inspect
state and construct the exact plan; this capability does not perform the
benchmark cleanup automatically.

### Workstream lifecycle

- `secretary_create_workstream`: after explicit user authorization, create a
  bounded brief, allocate a persistent workstream branch/worktree, and launch
  the full worker.
- `secretary_open_workstream`: focus an existing exact workstream.
- `secretary_relaunch_workstream`: an explicit, exact-target-only transition for
  a stopped tmux worker into Herdr; it refuses live/uncertain tmux state and
  never performs an automatic migration.
- `secretary_create_reviewer`: create a detached read-only reviewer checkout
  for an exact pending candidate OID.
- `secretary_land_reviewed`: after the secretary and user jointly decide that
  the candidate is acceptable, fast-forward only an exact current ACCEPT
  receipt. This is a controlled integration exception and can materialize
  reviewed source into the target worktree; a reviewer receipt never authorizes
  an automatic merge.
- `secretary_create_integration`: create a separate integration worker when
  the original target moved or direct landing is not safe.
- `secretary_cleanup_workstream`: remove exact owned landed workstream/review
  resources only after clean, unmoved, non-live, landed checks.

A generic secretary `subagent` request cannot create a worker or worktree. It
only delegates read-only investigation. Implementation starts through the
semantic workstream path or an explicitly authorized normal-parent worker
handoff.

## Worker tools and execution

The checked-in `worker` definition has:

```text
read, write, edit, bash, grep, find, ls, web_search, fetch_content,
get_search_content, source_check, contact_supervisor, intercom, host_command,
subagent
```

`host_command` is request-only: a worker or investigator may propose an exact
host shell command with a reason and description, but the host parent displays
it to the user and executes it only after that request is explicitly approved.

It loads the container-sandbox extension and the child-only workflow-state and
auto-continue extensions. `write`, `edit`, and `bash` are routed through the
assigned task execution plane. The worker is the one writer for that worktree;
the parent verifies the actual delta after it returns. A worker's nested
investigators are never writers and never receive a Git worktree.

The normal read-only roles (`scout`, `context-builder`, `delegate`, `oracle`,
`planner`, `researcher`, and `reviewer`) have:

```text
read, bash, grep, find, ls, web_search, fetch_content, get_search_content,
source_check, contact_supervisor, intercom, host_command
```

Their `acceptanceRole: read-only` is an acceptance/coordination contract, not a
filesystem authority boundary. `host_command` does not grant direct host
execution; it only creates a user-approved request. In normal parent runs their sandboxed `bash`
can still mutate a task workspace, so the parent must inspect the delta. The
secretary wrapper removes that risk for secretary investigators by replacing
their tools with the hardened read-only list above.

The dedicated review launcher has only read/search tools plus
`submit_review_receipt`. It cannot edit the review checkout.

## Typical user flow

1. The user opens the secretary grid and discusses a project goal with the
   project secretary.
2. The secretary reads project files and bounded Git state. If useful, it
   starts one or more asynchronous read-only investigators. Results return to
   the secretary for synthesis; investigators do not create worktrees.
3. The user authorizes a bounded implementation workstream. The secretary
   records the brief, creates the managed branch/worktree, and starts the full
   worker in its own Pi development window.
4. The worker edits and tests in its assigned sandbox/worktree/branch, then
   reports completion, attention, or a review request. The secretary observes
   the durable workstream record and attention events.
5. For review, the secretary creates an exact-OID detached reviewer. The
   reviewer inspects without editing and submits one receipt. A later commit
   makes that receipt stale.
6. The secretary and user jointly decide whether the candidate is acceptable.
   A reviewer receipt is evidence for that discussion, not an automatic merge
   decision. Only after the user explicitly authorizes landing/merging does the
   secretary call the guarded fast-forward tool, or create a separate
   integration workstream. Landing is never an automatic merge of an arbitrary
   branch.
7. After an explicitly approved landing, the user may authorize exact workstream cleanup. For legacy
   benchmark/side-agent cleanup, the secretary first constructs a
   `secretary_git_cleanup` dry-run plan, shows the exact ref/worktree/artifact
   actions, and applies only the reviewed hash.
8. Git commit or push actions require their own explicit commit/push language.
   A generic affirmation for a different secretary action does not authorize
   them.

## Sessions, state, and containers

- `pi-root-session.py` gives visible root conversations stable session files,
  repositories, worktrees, and branches.
- Secretary project records and workstream records live in the user-scoped
  `pi-secretary` state directory, not in product source.
- Normal child sessions and subagent artifacts live in user-scoped session or
  temporary directories, not in repositories.
- Clean protected ordinary repositories receive linked worktrees and managed
  branches. The configured control-plane repository may intentionally remain
  trusted-live when dirty so it can repair its own launchers.
- In trusted-live mode the assigned worktree and required Git metadata are
  host-shared; in isolated mode the task uses a private clone/checkpoint path.
- The container-sandbox extension keeps Pi/auth/session state on the host and
  routes worker tools into the task container. The host performs fixed Git and
  container-control bridges; it does not expose arbitrary host shell/Git tools
  to the model.
- `pi-host` is a separate explicit unsandboxed maintenance mode. It is the
  activation path for reviewed control-plane installation and should not be
  confused with a worker or secretary.

## Async behavior and failure visibility

`pi-subagents` is configured for asynchronous top-level runs with hidden success
notifications. The interactive parent normally gets control immediately and
receives a completion wake-up; status/fleet views remain available for explicit
inspection. Failures, pauses, stops, and attention events remain visible.

There are no automatic elapsed-time, assistant-turn, provider-token, or
tool-call limits on spawned agents. The configuration omits timeout and budget
defaults, and the secretary explicitly strips caller/configured limits before
launching its read-only investigators. `maxSubagentSpawnsPerSession: 0` and
`parallel.maxTasks: 0` mean unlimited; concurrency staging and child-depth/tool
allowlists are safety/authority boundaries, not model-completion cutoffs. A
user may still explicitly interrupt or stop a run.

This is deliberate anti-slop design: contracts, task packets, acceptance gates,
relevant context, role/tool authority, and observable results constrain meaning
without forcing a model to stop before it has finished the work.

The acceptance layer only evaluates criterion status for `checked` and stronger
levels. At `attested`, it validates the report shape but does not reject a
criterion marked `not-applicable`; if a run rejected that status, its effective
level or a later aggregate gate was stronger and must be inspected in
`status.json`. Read-only investigations should use a bounded report and
`level: none` when no acceptance gate is meaningful. A provider WebSocket/stream
error is classified by `pi-goal` as a retryable provider interruption, but the
exact provider error must be preserved in the host session to distinguish
transport failure from harness failure.

Async runs persist `status.json`, `events.jsonl`, child session JSONL, and
metadata/output artifacts when enabled. Terminal step events include bounded
error text plus acceptance status, effective level, and failed-check messages.
Secretary aggregate statistics persist
run state, timing, tokens, step summaries, and bounded failure/acceptance
diagnostics in `secretary-stats.jsonl`. The generic run's full acceptance
ledger and recent output still belong in its `status.json` or metadata; the
aggregate log deliberately omits prompts and full transcripts.
