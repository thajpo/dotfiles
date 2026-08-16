The two interrupted investigations were rerun. No files or runtime state were changed in plan mode.

**Crash Findings**
The tmux crash exposed serious recovery defects:

| Severity | Defect | Evidence |
|---|---|---|
| Critical | Writer recovery checks `authority == "writer"`, but real coding runs use `"writer-container"` | `scripts/pi_control/pi_reconcile.py:66,76`; `role_profiles.py:23-25` |
| Critical | Lost-run recovery never stops orphaned Docker containers | Six dead runs still have managed containers running |
| Critical | Recovery therefore never releases `active_writer_run_id` for real writer runs | Relaunches can permanently fail at writer acquisition |
| High | Recovery verifies only the supervisor PID, not the child Pi process | `pi_reconcile.py:61-64` |
| High | tmux `SIGHUP` can bypass host-supervisor cleanup | No signal handler in `host_supervisor.py` |
| High | Surface repair relaunches before recovering the previous run | This collides with the stale writer claim |
| High | The grid relies on pane titles that the Pi TUI overwrites | Live pane title is `π - mlre-transition`, not the expected controller title |
| High | Grid reconciliation only adds panes; it does not reconcile changed membership, order, or layout | Existing windows can contain the wrong conversation |
| High | `pidev` silently registers projects, creates worktrees, starts coding runs, and creates tmux sessions | This caused the current accidental workstreams |

Current observable state:

- `pisec` is gone.
- `pi-personal` contains one inconsistent `dotfiles` window that is actually running the `mlre-transition` personal conversation.
- Eight runs claim `running`.
- Seven of those runs have dead supervisor and child PIDs.
- Six orphaned managed containers remain alive.
- One `mlre-transition` personal run remains genuinely alive.
- Five accidental `"personal"` workstreams exist because I incorrectly used `pidev` as a registration command.
- Those five worktrees were clean and at their starting commits when inspected.
- `surface/preferences.json` does not exist, so both grids currently fall back to every registered project.
- Two failed system-test tmux servers also leaked because fixture cleanup is not in a `finally` block.

## Target Model
The implementation should restore this exact hierarchy:

```text
pisec tmux session
├── project secretary pane: sleepydreamyv3
├── project secretary pane: csvagent
└── project secretary pane: vla-lens

pi-project-<stable-git-common-dir-hash> tmux session
├── workstream window A
│   ├── Neovim on worktree A
│   └── controller-bound workstream agent A
└── workstream window B
    ├── Neovim on worktree B
    └── controller-bound workstream agent B

pi-personal tmux session
├── personal pane: dotfiles, using primary checkout
├── personal pane: mlre-transition, using primary checkout
└── personal pane: finances, using primary checkout
```

In mobile mode, each headful workstream window contains only the agent pane. No personal conversation is duplicated into a project workstream session.

The controller owns project, conversation, working-copy, run, authorization, and workstream identity. Tmux only presents and focuses those resources.

## Fixed Decisions
These defaults remove ambiguity for the implementing agent:

- Restore historical desktop workstream windows as **Neovim + agent**.
- Keep `pi-personal` bound to primary checkouts.
- Give each headful workstream its own controller-owned worktree and durable conversation.
- Make `pidev` navigation/presentation-only; it must never silently create a workstream.
- Derive project tmux session names from the canonical Git common-directory hash, not random controller project IDs or checkout paths.
- Use existing `project_messages`, `operations`, `authorizations`, and `presentation_assignments`; avoid a schema epoch bump unless implementation proves one unavoidable.
- Do not import old chats.
- Do not automatically terminate a live proven conversation merely because grid membership changed.
- Automatically recover a lost run only when supervisor, child process, writer lock, and container absence can all be proved. Otherwise surface attention and refuse relaunch.

## Declarative Configuration
Add a tracked machine configuration and expose it live as `~/.config/pi/surfaces.json`.

Recommended tracked path:

```text
machines/linux-x86_64.pi-surfaces.json
```

Recommended contents:

```json
{
  "version": 1,
  "projects": [
    {
      "alias": "dotfiles",
      "repository": "/home/j/dotfiles"
    },
    {
      "alias": "mlre-transition",
      "repository": "/home/j/Projects/mlre-transition"
    },
    {
      "alias": "finances",
      "repository": "/home/j/Projects/investing"
    },
    {
      "alias": "sleepydreamyv3",
      "repository": "/home/j/Projects/SleepyDreamyV3"
    },
    {
      "alias": "csvagent",
      "repository": "/home/j/Projects/csv-agent"
    },
    {
      "alias": "vla-lens",
      "repository": "/home/j/Projects/vla-lens"
    }
  ],
  "surfaces": {
    "pi-personal": [
      "dotfiles",
      "mlre-transition",
      "finances"
    ],
    "pisec": [
      "sleepydreamyv3",
      "csvagent",
      "vla-lens"
    ]
  }
}
```

`finances` must use `/home/j/Projects/investing`, not `/home/j/Projects/investing/investment-os`; Git reports the former as the canonical top-level.

The bootstrap must:

- Validate the whole configuration before writing anything.
- Canonicalize each repository with Git.
- Fail on missing required repositories.
- Detect duplicate aliases and duplicate Git common directories.
- Register missing projects idempotently.
- Rename existing controller display names safely when aliases changed.
- Preserve unrelated registered projects but leave them inactive.
- Atomically replace each ordered surface preference.
- Distinguish missing preference from explicitly empty preference.
- Support `--dry-run`.
- Produce a useful desired/current diff.
- Be safe to rerun after every state reset.

## Execution Plan

### 1. Preserve and Classify Current State
Before edits, record an exact state packet:

- Current Git status and diff.
- Active build ID and schema digest.
- Every project, workstream, conversation, run, writer claim, and presentation assignment.
- Every managed Docker container and its labels.
- Every tmux server/session/socket.
- Exact Git status, branch, HEAD, and starting OID of the five accidental worktrees.
- The currently alive personal run and its container identity.

Do not kill the genuinely alive run during implementation. Do not manually edit SQLite.

Acceptance: the state packet must identify every resource that later cleanup mutates.

### 2. Fix Lost-Run Recovery First
Files:

- `scripts/pi_control/pi_reconcile.py`
- `scripts/pi_control/writer_lock.py`
- `scripts/pi_control/docker_runtime.py`
- `scripts/pi_control/launch.py`
- `scripts/pi_control/host_supervisor.py`
- `scripts/pi_control/pi_protocol.py`
- `scripts/pi_control/pi_client.py`
- `scripts/pi_control/pi_cli.py`

Required behavior:

1. `reconcile_run` observes both supervisor and child PID with start-identity fencing.
2. A missing/reused supervisor or child moves a running run to `needs_attention`.
3. `WriterLock` gains a read-only non-mutating availability probe.
4. Recovery requires supervisor gone, child gone, writer lock available, and exact managed-container identity.
5. Recovery stops/removes the exact container and proves absence.
6. Only after container absence is proved may one transaction mark the run `lost` and clear `active_writer_run_id`.
7. Claim clearing uses a compare-and-swap against the exact run ID.
8. `writer_epoch` remains monotonic.
9. Any uncertain process, lock, or container observation leaves the run in `needs_attention`.
10. `SIGHUP` and `SIGTERM` in the host supervisor enter controlled cleanup rather than bypassing `finally`.
11. Repeated recovery is idempotent.
12. Recovery cannot stop a container with mismatched IDs, names, labels, project, run, build, working-copy, or writer generation.

Critical bug fix:

```python
row["authority"] == "writer-container"
```

must replace the impossible `"writer"` checks in `recover_lost_run`.

Acceptance:

- Dead writer run becomes `lost`.
- Its container is absent.
- Its writer claim is cleared.
- A new run on the same working copy succeeds.
- A live writer lock refuses recovery.
- A live child with a dead parent refuses recovery.
- Mismatched container labels refuse cleanup.
- No partial DB transition survives failed cleanup.

### 3. Integrate Recovery with Surface Repair
Before respawning a dead secretary, personal, or workstream pane:

1. Query active runs for its conversation.
2. Continue if an exact live owner still exists.
3. Reconcile dead or uncertain owners.
4. Automatically recover only when every recovery proof is exact.
5. Refuse relaunch and show attention if recovery remains uncertain.
6. Launch a new run only after the prior writer claim is gone.

`pi-restart` must stop managed grid processes through controller-aware cleanup before killing or rebuilding grid sessions. It must never kill the tmux server or project workstream sessions.

Acceptance: killing a fixture tmux session leaves no running managed container, stale claim, or permanently blocked conversation.

### 4. Add Exact Workstream Retirement
A real cleanup operation is missing and is needed both for the accidental resources and normal product use.

Add `workstream.retire` with exact authorization and these guards:

- Workstream belongs to the authenticated project.
- No live or uncertain run exists.
- Container absence is proved.
- Writer lock is free.
- Working-copy status is observed freshly.
- Unsubmitted or dirty work prevents deletion.
- Worktree path and Git common directory match durable intent.
- Branch deletion requires exact expected ref and OID.
- Presentation assignment becomes `absent`.
- Workstream and conversation become retired/archived.
- Git worktree removal happens before branch deletion.
- Partial cleanup is recoverable and recorded as attention.

No broad `rm -rf`, wildcard branch deletion, or raw SQLite deletion.

### 5. Separate Headful and Headless Workstreams
Change the low-level workstream saga to accept explicit presentation intent:

```text
presentation = headful | headless
```

For headful workstreams:

- Create one `presentation_assignments` row with `desired_state=present`.

For headless workers:

- Do not create a presentation assignment.
- Launch through the detached child path.
- Expose them in observability as headless workers, not headful workspaces.

Update `start_worker_assignment()` to request `headless`.

This fixes the current bug where every headless worker implicitly requests tmux presentation.

### 6. Implement Declarative Bootstrap
Add a command such as:

```text
pi-grid-bootstrap [--config PATH] [--dry-run]
```

Alternatively expose the same operation as:

```text
pi-surface bootstrap
```

Add a safe `project rename` controller operation because re-registering an existing Git common directory currently ignores a new alias.

Project rename must:

- Preserve `project_id`.
- Preserve Git identity and working copies.
- Reject duplicate aliases.
- Update the default secretary display name if it still matches the old generated form.
- Emit a controller event.
- Never rewrite user-chosen conversation names.

Bootstrap acceptance:

- Fresh state produces exactly six registered projects.
- Second execution is a no-op.
- `pi-personal` preference has exactly the three requested aliases in order.
- `pisec` preference has exactly its three aliases in order.
- Resetting state and rerunning bootstrap reproduces the same desired surface.
- A missing repository fails before preference mutation.
- Existing unrelated registered projects remain registered but inactive.

### 7. Fix Preference and Surface CLI Semantics
Files:

- `scripts/pi-surface.py`
- `bin/pisec`
- `bin/pi-personal`

Required fixes:

- Return `configured: true|false` with preference reads.
- Missing preference means configured fallback behavior.
- Present empty list means an intentionally empty grid.
- Add `activate --clear`.
- Drop or report stale project IDs instead of bricking every command.
- Deduplicate hand-edited preferences.
- Correct `swap` to reorder two active projects or rename it to `reorder`.
- Correct the misleading "one active and one inactive" error.
- Resolve projects by exact ID or unique alias.
- Reject duplicate aliases.
- Make `list` show registered, configured-active, and stale entries distinctly.
- Make `launch-info` accept the documented project ID as well as alias.
- Stop claiming fallback order is registration order when the controller returns display-name order.

### 8. Replace Bash Grid Mutation with One Reconciler
Move tmux mutation into a single tested Python presentation primitive. Keep shell wrappers thin.

Files:

- `scripts/pi_control/presentation.py`
- New `scripts/pi_control/presentation_locator.py`
- `scripts/pi_control/pi_protocol.py`
- `scripts/pi_control/pi_cli.py`
- `scripts/pi-surface.py`
- `bin/pisec`
- `bin/pi-personal`
- `bin/pidev`

Canonical naming:

```text
Secretary session: pisec
Secretary windows: projects-1, projects-2, ...

Personal session: pi-personal
Personal windows: personal-1, personal-2, ...

Project session: pi-project-<sha256(canonical-git-common-dir)[:12]>

Workstream window:
ws-<sanitized-title>-<workstream-id-short>
```

Using numbered grid windows avoids collisions with project aliases such as `shell`, numeric names, duplicate sanitized names, and tmux indexes.

The project session hash must use canonical Git common-directory identity so all linked worktrees converge on the same project session and the name remains stable across controller-state resets.

### 9. Use Versioned Presentation Locators
Standardize `presentation_assignments.locator_json`:

```json
{
  "version": 1,
  "backend": "tmux",
  "surface": "project",
  "session": "pi-project-0123456789ab",
  "window": "ws-fix-cancel-abcdef12",
  "pane": "%42",
  "projectId": "prj_...",
  "workstreamId": "ws_...",
  "conversationId": "conv_...",
  "role": "workstream",
  "layout": "desktop",
  "argvDigest": "sha256:...",
  "ownerPid": 12345,
  "ownerStartIdentity": "linux:..."
}
```

Old unversioned locator shapes must be treated as unknown and rewritten after observation. Do not trust them as proof of a live presentation.

### 10. Fix Pane Identity Proof
Never use pane title as authority. The TUI overwrites it.

A live pane is proven only when:

- The controller run belongs to the exact conversation.
- Its owner PID and process-start identity match.
- The pane process tree contains the exact activated launcher.
- Launcher argv contains the exact conversation ID, role, build ID, and `--interactive`.
- The recorded argv digest matches the expected activated-generation argv.

Tmux pane user options may store locator hints:

```text
@pi-managed
@pi-conversation-id
@pi-role
@pi-argv-digest
```

These are hints, not authority.

Pane title remains a human label and is reasserted after launch or respawn.

A dead or idle pane may be repaired using its durable assignment. A live pane that fails proof is `drifted` and must not be killed automatically.

### 11. Implement Full Grid Reconciliation
The current launchers are additive. Replace that behavior with desired-versus-observed reconciliation.

The algorithm:

1. Load configured ordered projects.
2. Resolve exact controller conversations.
3. Observe all managed windows and panes.
4. Classify each pane as proven live, dead, idle, drifted, foreign, or missing.
5. Build replacement windows before retiring old windows.
6. Move proven live panes when order/layout changes.
7. Create only missing panes.
8. Respawn only dead or idle managed panes.
9. Never duplicate a conversation.
10. Never trust a window merely because its name exists.
11. Never kill a proven live thinking conversation during repair.
12. Put removed-but-live conversations into a temporary draining window until they stop or the user explicitly stops them.
13. Remove dead or idle stale panes.
14. Remove empty managed windows.
15. Leave foreign/user-created windows untouched.
16. Persist new locator observations and errors.
17. Release the reconcile lock before attaching a client.

Desktop layout uses two project conversations per grid window. Mobile layout uses one.

### 12. Restore Project Workstream Sessions
A headful workstream presentation creates or repairs:

```text
pi-project-<hash>:ws-<title>-<id>
```

Desktop:

```text
pane 0: nvim in exact controller worktree
pane 1: pi-system-workstream-run for exact conversation
```

Mobile:

```text
pane 0: pi-system-workstream-run for exact conversation
```

The secretary can list, create, open, focus, and relaunch only workstreams belonging to its authenticated project.

Focusing from inside tmux uses `switch-client`, `select-window`, and `select-pane`. Outside tmux it uses `attach-session`.

Presentation failure does not delete or recreate the workstream. Relaunch uses the same conversation and session file but creates a fresh run.

### 13. Close the Workstream Approval Loop
Use existing durable primitives rather than reviving the old capability-token system.

Split creation into:

```text
plan -> approve -> apply -> present
```

Planning:

- Refresh project and Git observations.
- Capture target ref, base OID, base tree, title, purpose, known overlap, and presentation intent.
- Create an idempotent `workstream.create` operation in `planned`.
- Post a `needs-user` project message containing the operation ID and request digest.
- Do not create a worktree yet.

Approval:

- The secretary's first-party extension renders the exact card using `ctx.ui.confirm`.
- Approval requires an interactive UI.
- Generic "yes," message acknowledgement, or model text is not approval.
- Create one `authorizations` row with `kind=create-workstream`.
- Bind it to the exact operation ID, project, base OID, request digest, user decision, and expiry.
- Reject replay, expiry, digest mismatch, project mismatch, and target movement.

Application:

- Atomically consume the authorization and move the operation to `applying`.
- Create the exact branch and worktree.
- Re-observe the Git effect.
- Insert working copy, conversation, workstream, and headful presentation assignment in one transaction.
- Mark the operation succeeded.
- Resolve the proposal message.
- Reconcile and focus the project presentation.

Crash after Git worktree creation but before DB commit must replay through the existing `_observe_effect()` mechanism without creating a second worktree.

Keep raw `workstream.create` outside secretary role grants. The first-party extension may request the authorized semantic operation but may not execute arbitrary subprocesses.

### 14. Fix `pidev`
`pidev` must no longer create a generic `"personal"` workstream.

Recommended semantics:

```text
pidev DIR
```

- Resolve or explicitly register the project.
- Reconcile the project's existing headful workstream session.
- Open/switch to that project session.
- If no headful workstream exists, open an editor-only `home` window for the primary checkout or report that no workstream exists.
- Never launch the same personal conversation in both `pi-personal` and a project session.
- Never create a worktree without an explicit approved workstream request.

Explicit creation belongs in the secretary flow or an approved `pi-workstream create` command.

Apply the same `send-keys "exec ..."` workaround used by the grids; current `pidev` directly passes `--conversation-id` through tmux despite documenting that tmux 3.4 can drop such commands.

### 15. Fix `pi-start`, `pi-restart`, and tmux Hooks
Required corrections:

- Pass mobile mode to both grids.
- Replace stale `personal-1..N` layout detection with reconciler-reported layout.
- Stop using session deletion as normal layout conversion.
- Preserve project workstream sessions.
- Never kill the tmux server.
- Reconcile lost runs before relaunch.
- Run personal and secretary bootstrap before grid reconciliation.
- Gate automatic startup so merely starting any tmux server does not launch every Pi agent.
- Post-resurrect repair should run only for grid sessions that existed or when an explicit auto-start preference is enabled.
- Preserve unrelated sessions and attached clients.

### 16. Fix Test Isolation
`installed-surface-journey.py` and all tmux fixture journeys must use `try/finally`:

- Kill only their fixture-scoped tmux server.
- Stop fixture processes.
- Prove no managed fixture containers remain.
- Remove fixture roots.
- Never touch the user's default tmux socket.
- Preserve logs/evidence when a test fails.

This addresses the currently leaked fixture servers.

## Test Matrix
The implementation is not accepted by row-level tests alone.

### Unit and Contract Tests
Add or extend:

```text
tests/control_plane/test_run_recovery.py
tests/control_plane/test_workstream_authorization.py
tests/control_plane/test_workstream_retirement.py
tests/control_plane/test_presentation.py
tests/control_plane/test_surface_bootstrap.py
tests/control_plane/test_subagents_async.py
tests/control_plane/test_p7_workstreams.py
tests/control_plane/test_secretary_work.py
tests/secretary-work-extension.test.mjs
```

Required cases:

- Writer-container authority recovery.
- Parent and child identity fencing.
- Read-only writer-lock probe.
- Exact container cleanup.
- Failed cleanup leaves claim intact.
- New run succeeds after recovery.
- Headless worker has no headful assignment.
- Proposal replay, expiry, staleness, rejection, and one-use authorization.
- Worktree saga crash recovery.
- Locator parsing and rewrite.
- Duplicate aliases.
- Empty and missing preference distinction.
- Bootstrap idempotency.
- Safe workstream retirement.

### Isolated Tmux Tests
Keep and expand:

```text
tests/test_pisec_grid.sh
tests/test_personal_grid.sh
tests/test_project_workspace_grid.sh
```

Required scenarios:

- Correct independent active sets.
- Desktop and mobile layouts.
- Reorder without duplicate conversations.
- Shrink without killing live conversations.
- Dead-pane repair after TUI overwrites the title.
- Exact argv/process proof.
- Drift is reported, not killed.
- Project session has multiple workstream windows.
- Desktop workstream is Neovim + agent.
- Mobile workstream is agent-only.
- Unrelated sessions survive.
- Attached clients survive reconciliation.
- Session names do not collide with aliases.
- Fixture cleanup runs after assertion failure.

### Installed End-to-End Journey
The installed journey must prove:

1. Fresh state bootstrap creates the six desired projects.
2. `pi-personal` shows only dotfiles, mlre-transition, and finances.
3. `pisec` shows only sleepydreamyv3, csvagent, and vla-lens.
4. A secretary proposes a headful workstream.
5. No worktree exists before approval.
6. Exact interactive approval creates one worktree and one conversation.
7. The project session appears with Neovim and the real workstream agent.
8. A second workstream becomes a second window in the same project session.
9. A tmux crash leaves no orphan container or stale writer claim.
10. Reconciliation relaunches the same conversation.
11. `pi-personal` still works directly in the primary checkout.
12. A headless worker never appears in tmux.
13. Restart preserves unrelated tmux sessions.
14. All managed containers are absent at journey completion.

Verification commands should include:

```bash
python3 -m unittest discover -s tests/control_plane -p 'test_*.py'
node --test tests/secretary-work-extension.test.mjs
bash tests/test_pisec_grid.sh
bash tests/test_personal_grid.sh
bash tests/test_project_workspace_grid.sh
bash tests/system/run-repair-surface.sh
bash tests/system/run-p11-release.sh
```

## Live Cleanup and Migration
Only after the recovery and retirement operations pass tests:

1. Reconcile all current runs.
2. Leave the genuinely live personal run untouched until the user exits or explicitly authorizes stopping it.
3. Recover the seven dead-owner runs.
4. Prove the six orphan containers absent.
5. Recheck all five accidental worktrees are clean and at their recorded starting OIDs.
6. Retire the five accidental workstreams through the new operation.
7. Remove their exact worktrees and branches.
8. Remove the accidental project tmux sessions if they still exist.
9. Install the declarative surface configuration.
10. Bootstrap the six projects and aliases.
11. Stage and activate a fresh generation.
12. Rebuild only `pisec` and `pi-personal`.
13. Verify project workstream sessions are created only after approved workstreams.
14. Keep the existing finale backup until this live acceptance passes.

## Changed System Surfaces
| Surface | Planned change |
|---|---|
| Public CLI | Bootstrap, project rename, workstream plan/approve/status/retire, presentation reconcile/focus |
| Schema | Prefer no column/table changes; reuse current durable records |
| Persistence | Versioned locator JSON; declarative surface config; corrected preference semantics |
| State transitions | Exact lost-run recovery; authorized headful workstream saga; safe retirement |
| Authority | Secretary proposes and receives interactive approval; controller creates; tmux never creates identity |
| Transactions | Authorization consumption before Git; resource commit after observed Git effect |
| Concurrency | Run/process fencing, read-only writer-lock probe, presentation reconcile locks |
| Cancellation | Signal-aware supervisor cleanup; crash recovery before relaunch |
| Error semantics | Uncertainty becomes attention; no silent relaunch or destructive cleanup |
| Dependencies | No new external dependency expected |
| Performance | One controller/tmux observation per reconcile; no source rebuild |
| Operations | No tmux server kill; exact container/worktree cleanup; rollback retained |
| Observability | Presentation state, recovery decisions, drift, and deferred removals recorded |

This should be treated as a **MAJOR program**, not one large edit. The implementation agent should complete and verify each slice before moving to the next, with crash recovery first.
