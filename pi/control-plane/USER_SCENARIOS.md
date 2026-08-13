# Pi User Scenario Catalog

How the operator (you) actually drives Pi day to day, with the real command
surface (`pi`, `pi-start`, `pi-restart`, `pidev`, tmux grids, `pi-control`,
`pi-activate`, `pi-authorize`) and the role topology from the execution
contract. Each scenario states the user action, the expected system behavior,
and the decisive observable. This catalog exists to decide what must be
proven end to end, not to dictate implementation.

## 1. Startup and day-opening

### U-01 Start the grid
User runs `pi-start all`. Expect: personal + secretary launch into a desktop
tmux grid; two managed windows appear; the controller registers runs; nothing
else on the host is touched. Decisive observable: tmux sessions exist, two Pi
processes bound to controller runs, exit code 0, no leftover processes after
teardown.

### U-02 Restart the grid
User runs `pi-restart`. Expect: clean rebuild of personal + secretary without
duplicating sessions; the old managed sessions are replaced, unrelated tmux
sessions untouched. Decisive observable: exactly one personal and one
secretary session after restart; unrelated session still alive.

### U-03 Start personal only
User runs `pi-start` (no argument). Expect: personal conversation only, no
secretary. Decisive observable: one managed window, secretary absent.

### U-04 Start secretary only
User runs `pi-start secretary`. Expect: secretary only, no writer container.

### U-05 Start a project workspace
User runs `pi-start project [DIR]`. Expect: Neovim + Pi workspace for that
directory; project is registered; work index reflects the repo.

### U-06 Open a repo with `pidev` / `pi-start project`
User runs `pidev` or `pi-start project [DIR]` inside a repository. Expect:
existing project/session resumed if present, fresh registration if not; the
session continues its durable history. Decisive observable: resumed session
entries are contiguous with the prior session; project identity reused.

### U-07 Day-open summary
User starts the grid and asks "what's pending?" Expect: secretary reports
attention items, in-flight changes, pending reviews, and failed work from
yesterday. Decisive observable: attention rows surface, work index lists
unresolved items.

### U-08 Start when the grid is already up
User runs `pi-start all` twice. Expect: safe rebuild, no duplicate grid, no
error. Decisive observable: still exactly one personal + one secretary.

### U-09 Start with tmux unavailable
User starts the grid on a host with no tmux. Expect: explicit STOP/77-style
failure or desktop fallback, never a silent partial grid.

## 2. Everyday coding loop

### U-10 Fix a bug
User asks "fix the failing test". Expect: secretary scopes the request,
dispatches to the personal writer; the writer reads the repo, edits in its
container, runs tests, and submits an immutable change. Decisive observable:
submitted change with exact tip/tree OIDs; the working copy shows the edit
only inside the container.

### U-11 Iterate before submitting
Writer makes several internal attempts before the user is shown one change.
Expect: only the final submitted revision is visible to the user; intermediate
container state never leaks into the project.

### U-12 Revise after review feedback
A reviewer requests changes; the writer submits revision 2 of the same change.
Expect: revision 2 supersedes revision 1; review of revision 1 becomes stale;
a new review binds revision 2. Decisive observable: `current_revision`
advanced, stale receipt rejected.

### U-13 Ask a question, no write
User asks "what does this function do?" Expect: secretary or investigator
answers from a scoped read; no writer container starts; no approval prompt.
Decisive observable: zero writer runs, answer contains the exact bytes read.

### U-14 Investigator research
User asks for a bounded investigation. Expect: temporary investigator runs
read-only against an immutable snapshot, produces a durable result, and its
conversation archives. Decisive observable: investigation row in terminal
state with result; snapshot unchanged; no edit tools available.

### U-15 Multi-file refactor
User asks to rename a symbol across many files. Expect: writer edits the full
set, runs tests, submits one change with the complete changed-path set.
Decisive observable: change revision lists all touched paths; diffstat
matches.

### U-16 Parallel workstreams
User says "fix X, and in parallel research Y". Expect: a workstream is created
for Y with its own worktree and container while X proceeds in the personal
conversation. Decisive observable: two writer runs, distinct worktrees, both
submit independently.

### U-17 Workstream dependent on a change
A workstream must build on an already-submitted change's revision. Expect:
child source pins the exact revision; the worktree starts from that revision,
not the branch head. Decisive observable: workstream HEAD equals the pinned
revision OID.

### U-18 Run the test suite
Writer runs tests in its container. Expect: tests execute only inside the
container; results return; failure starts a fix loop. Decisive observable:
test command recorded in the run, no host test process.

## 3. Dependency and package work

### U-19 Upgrade a dependency
User asks to upgrade npm/Python packages. Expect: dependency inventory diffs,
the exact new version is proposed, package materialization runs in an
isolated cache-backed container, and a one-use approval gates it. Decisive
observable: `package_requests` succeeded, environment tree digest recorded,
no remote contact.

### U-20 Approve a package request at the TTY
A package request prompts `pi-authorize`. User types APPROVE. Expect: exact
request shown (project, conversation, run, digest, effect scope), one-use
execution, replay refused. Decisive observable: authorization consumed,
identical replay errors.

### U-21 Reject a package request
User types REJECT. Expect: request recorded rejected, no container runs.

### U-22 Locked environment reproducibility
Two identical locked inputs materialize identical private environments.
Decisive observable: environment tree digests equal.

### U-23 Unsupported/tampered package input
A package manager Pi does not support, or a tampered cache, is refused
fail-closed. Decisive observable: request fails, nothing partially installed.

## 4. Review and integration

### U-24 Review before merge
After a writer submits, the secretary requests a review of the exact
revision. Expect: reviewer inspects the immutable snapshot; verdict
recorded. Decisive observable: review receipt bound to change/revision, with
tip/tree OIDs.

### U-25 Multiple reviewers must all pass
Two reviewers review different parts; integration proceeds only if every
submitted review is `accept`. Decisive observable: a single
`changes_requested` blocks authorization.

### U-26 User inspects work index
User views pending work. Expect: active changes, pending reviews, attention,
and failed work listed. Decisive observable: work index rows reflect DB state.

### U-27 Approve a local target update
User is prompted to authorize integration of an accepted change. Expect:
exact target, revision, and analysis shown; one-use authorization.
Decisive observable: authorization scope digest binds analysis + target.

### U-28 Fast-forward integrate
Accepted change integrates cleanly. Expect: target ref advances exactly to the
candidate tip, rollback ref recorded, change marked merged. Decisive
observable: `refs/heads/<target>` == candidate tip; rollback ref points at the
old target.

### U-29 Diverged branch (conflict)
The branch moved while the change was in flight. Expect: controller creates an
integration assignment, an integration writer merges the exact revisions in a
worktree, tests, submits a new revision, which receives independent review and
a separate target-update authorization. Decisive observable: integration
result change exists, source change stays open until its result is accepted.

### U-30 Nothing to integrate
The change is already contained. Expect: integrate reports already-contained;
target ref unchanged.

### U-31 Expired or replayed authorization refused
A stale or replayed integration authorization is refused. Decisive
observable: error, no ref mutation.

## 5. Interruption, crash, and recovery

### U-32 Ctrl-C mid-task
User interrupts a long writer/investigator run. Expect: run terminalized,
conversation archived for temporary roles, nothing half-written. Decisive
observable: run in a terminal state, investigation terminal record present.

### U-33 Cancel one tool request
A long-running tool call is cancelled with the exact request-id. Expect:
worker stops, response is an error, no partial side effect.

### U-34 Laptop sleep/reboot mid-session
The machine restarts while a personal session is open. Expect: on restart,
the same session resumes with contiguous history; project, messages, and
attention survive. Decisive observable: session file grows by appending;
contiguity check passes.

### U-35 Controller killed mid-integration
The controller dies during a target update. Expect: on retry, either the
target is unchanged or the integration resumes deterministically; never a
half-updated ref without a rollback ref. Decisive observable: recovery
assertion as in P10 evidence.

### U-36 Writer container killed
A writer's container dies mid-run. Expect: the writer run fails cleanly; a
second writer for the same working copy is refused until container absence is
proved; no managed containers remain. Decisive observable: second-writer
request fails, `docker ps` clean.

### U-37 Docker daemon restart mid-writer
The daemon disappears while a writer is running. Expect: run fails cleanly,
no orphan container claim.

### U-38 Unrelated tmux preserved
Pi restarts or crashes; an unrelated tmux session (e.g., a long build) is
untouched. Decisive observable: session still listed with its original
command.

## 6. Multi-project and continuity

### U-39 Two registered projects
User works in two repos. Expect: each has its own secretary, sessions, and
work index; nothing leaks across projects. Decisive observable: per-project
conversation/run rows, distinct state.

### U-40 Switch focus
User moves between project workspaces. Expect: each resumes its own durable
conversation; attention reflects the focused project.

### U-41 Long-lived session
A secretary session runs for days. Expect: session file bounded, history
contiguous, resume works after the process restarts many times.

## 7. Messages and attention

### U-42 Secretary asks a decision
Secretary posts "decision needed". User replies and acknowledges. Expect:
message chain, ack recorded. Decisive observable: message state transitions
project-wide (shared inbox), reply threading.

### U-43 Attention cleared
A resolved item leaves the attention set; new items appear on the next day
open.

## 7.5 Grids, personal defaults, headless subagents, and observability

### U-51 Configure the secretary grid
User runs `pisec register ALIAS`, `pisec list`, `pisec activate A B`,
`pisec swap OLD NEW`. Expect: the secretary grid shows the ordered active
set; registration and ordering are durable controller preferences. Decisive
observable: exactly the active set renders, one live secretary per project,
ordering follows the configured list.

### U-52 Configure the personal grid
User runs the same grid commands against the personal surface. Expect: the
personal grid's ordered active set is independent from the secretary grid.
Decisive observable: personal shows a different set/order than `pisec`
without affecting secretary panes.

### U-53 Personal edits the primary checkout by default
User asks `pi-personal` to fix a bug with pre-existing uncommitted work in
the registered checkout. Expect: personal works in the primary checkout, the
controller records the exact baseline before mutation, task changes are
submitted as a task-delta revision, and pre-existing files stay untouched.
Decisive observable: baseline recorded, submitted revision contains only task
files, pre-existing work byte-identical afterward.

### U-54 Personal asks when attribution is ambiguous
User edits an overlapping file by hand mid-task. Expect: submission stays a
draft, the user is asked to select files; nothing is combined silently.

### U-55 Async headless investigation
Secretary starts a headless investigator and continues answering. Expect:
control returns immediately, the child runs under the controller, a
completion notification and run status arrive later. Decisive observable:
parent turn does not block on the child; child terminal record exists.

### U-56 Parallel headless fanout
User asks for two independent review angles. Expect: two read-only children
run concurrently with distinct briefs and snapshots; the parent synthesizes.
Decisive observable: two child runs, distinct sessions, both terminal.

### U-57 Headless worker isolation and one writer
A headless worker implements in its own working copy while a headful writer
owns another. Expect: each working copy has exactly one writer; a second
writer on the same copy is refused. Decisive observable: worker working copy
and container distinct; second-writer refusal.

### U-58 Child escalation and steering
A child hits an unapproved boundary and requests a decision; the user replies
through the parent; the child resumes. Expect: bounded supervisor messages,
no scope change without approval. Decisive observable: message records
parent↔child, child continues after the reply.

### U-59 Interrupt, stop, and resume a child
User soft-interrupts a child, later resumes it, and finally stops it.
Expect: interrupt pauses durably, resume continues the same session, stop is
terminal and non-resumable. Decisive observable: session contiguity across
interrupt/resume; stop record is terminal.

### U-60 Child restart continuity
The machine restarts while a headless child is running. Expect: child
terminal/attention state survives; no double-launch of a live bound run.

### U-61 Role and model routing for children
Each headless role uses its configured model/reasoning policy. Decisive
observable: child runs bind the configured provider/model, not the parent's
default by accident.

### U-62 Inspector and compaction card
User opens `/observe` and compacts a long conversation. Expect: Task/Fleet/
Messages views show bounded real state without hidden reasoning; the
compaction card lists goal, decisions, submitted work, and unresolved items.
Decisive observable: inspector rows match controller records; the card's
user-visible summary derives from the same compacted result as the
model-visible summary.

### U-63 Harness feedback
A worker records one bounded non-blocking harness observation; the user
reviews it with `pi-harness-feedback`. Expect: record appears in the central
feed with provenance; no project state changed.

## 8. Maintenance and upgrade

### U-44 Stage and activate a new build
User stages a new generation and runs `bin/pi-activate` at a TTY. Expect:
exact build/plan shown, ACTIVATE required, fresh state initialized, bounded
smoke passes, old generation preserved as rollback. Decisive observable:
activation marker, smoke command output, `.rollback.*` sibling.

### U-45 Roll back an activated generation
User rolls back after a bad activation. Expect: new generation preserved as
`.preserved.*`, previous generation restored with its state and work intact.

### U-46 Activate twice / reject
A second activation preserves the first as rollback; a REJECT touches
nothing.

## 9. Security and failure closedness

### U-47 No credential leak
Child processes never inherit tokens/secrets. Decisive observable: child
environment probe contains no sensitive keys.

### U-48 Host command without approval refused
A writer-requested host command with no approval is refused, never run.

### U-49 Startup attestation rejection
A tampered/foreign Pi executable is refused at startup with a clear error.

### U-50 Replay refusal everywhere
Approved requests, reviews, and authorizations reject replays with different
content or after expiry.

## Coverage note

This catalog is the user-visible contract. Each U-### maps to the role/launch
matrix and to one or more HA actions. Installed evidence coverage after the
user-scenario journey work:

**New installed journeys (2026-08-11):**
- `run-u-resume.sh` (`installed-u-resume.py`) — coding-resume: personal writer
  full cycle in container, restart, contiguous resume, work index + attention.
  Covers U-06/07/41/43.
- `run-u-conflict.sh` (`installed-u-conflict.py`) — integration-agent-conflict:
  diverged branch forces the integration writer to create an integration-result
  change that receives independent review and a separate authorization before
  the target advances. Covers U-29. **Found and fixed a product gap**:
  `create_review_assignment` could not snapshot controller-created integration
  results (`source_working_copy_id IS NULL`); now snapshots from the primary
  repository (pi_review.py).
- `run-u-multiproject.sh` (`installed-u-multiproject.py`) — two registered
  projects with distinct secretaries/sessions/work indexes and no row leakage.
  Covers U-39/40.
- `run-u-review.sh` (`installed-u-review.py`) — review-exact-revision loop:
  submit, accept, revise, stale receipt rejected, all-submitted-reviews-must-
  accept gate. Covers U-12/24/25.
- `run-u-investigate.sh` (`installed-u-investigate.py`) — investigation-
  complete: durable result + archived conversation (the non-interrupt path).
  Covers U-14.
- Real-TTY approval envelopes (HA-011 tty-approve-execute-replay-refuse and
  HA-007 command-request-without-approval) emitted by `run-p6-installed.sh`;
  message threading (HA-006), locked-package-environment (HA-014) from the
  same journey; second-writer-refused (HA-004) from `run-docker.sh`;
  subagent-isolation (HA-018) from `run-p7-installed.sh`; secretary-resume
  (HA-002) and p2-controller-contract (HA-015) from `run-p10-installed.sh`.

**Coverage result:** 24 of 25 declared scenarios now have installed PASS
evidence on a single deterministic build (`run-p11-release.sh` aggregates all
18 actions). The only declared scenario without installed evidence is
`host-startup-attestation-rejection` (HA-016): the tampered-executable race is
inherently in-process (executable changing between prepare and spawn) and is
source-tested (`test_host_supervisor.py`); it cannot be deterministically
triggered from an installed subprocess without test-only hooks.

**Still untested at installed tier (not declared scenarios):** U-18 (test
suite run in container is covered by the P5 writer isolation bash test),
U-33 (cancel one tool request — source-only), U-37 (docker daemon restart
mid-writer), U-50 (replay refusal consolidated — covered per-surface).

**Repair-program target scenarios (declared in the action catalog, pending
installed evidence):** U-04 (secretary-only start), U-51/U-52 (grid active
sets), U-53/U-54 (personal-primary default and ambiguous attribution),
U-55–U-61 (headless subagent async/fanout/worker/escalation/restart/model
routing), U-62/U-63 (inspector, compaction card, harness feedback).
