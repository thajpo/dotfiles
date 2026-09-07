# Durable project review coordination

This is an opt-in layer around `bin/spawn` and Herdr's existing agent API. It
automates handoffs and bounded review/revision exchanges, not task invention,
merging, pushing, deployment, approvals, or worktree cleanup. It requires a
running Herdr host and running Codex sessions; it does not wake a sleeping
computer or launch a replacement conversation.

## Current process versus the new process

| Stage | Existing process | With coordination enabled |
| --- | --- | --- |
| Assignment | Supervisor writes a task packet and launches an isolated worker. | Same launcher; persist the packet, base, worktree, and exact parent/child identities. |
| Progress | Herdr reports activity; supervisor inspects panes. | Activity is still just activity. Explicit handoffs carry task state. |
| Completion | Worker commits and reports; someone notices the report. | Worker records project checks and submits a durable review request tied to its SHA. |
| Wake-up | Supervisor or human sends a prompt. | A deterministic timer dispatches pending requests to their registered recipients. |
| Review | Supervisor reads diff/tests and reports a judgment. | Supervisor acknowledges the request, independently reviews and reruns checks, then records a commit-specific verdict. |
| Revision | Supervisor sends feedback and later checks again. | Feedback enters the worker inbox; acknowledgement and a fresh tested commit start the next review round. |
| Interruption | Coordination context lives largely in conversations. | Queue, identities, test receipts, review decisions, and delivery attempts survive process restarts. |
| Integration | Supervisor follows the user's integration policy. | Unchanged. `ready` does not merge anything or authorize cleanup. |

The launcher and stateless `herdr-workers-state` projection remain useful on
their own. Unregistered supervisors retain the existing manual workflow.

## Level one: separate activity, task state, and delivery state

There are three independent facts:

- Herdr activity: working, idle/done, blocked, or unknown. `done` is not a
  completed task, passing review, or permission to merge.
- Task state: submitted, reviewing, changes requested, ready, needs User, or
  superseded. A historical ready verdict becomes effectively stale if its
  candidate HEAD moves or worktree becomes dirty.
- Delivery state: pending, sending, delivered, acknowledged, uncertain, or
  cancelled. A successful prompt call is not an acknowledgement or a review.

The normal cycle is:

```text
assigned worker
  -> commit + required checks
  -> explicit handoff -> durable pending delivery
  -> supervisor idle + exact identities verified
  -> prompt -> acknowledgement -> independent review/checks
       -> ready at SHA --------------------------------> integration decision
       -> scoped revision -> worker acknowledgement ---> new tested commit
       -> needs User ----------------------------------> explicit decision
```

`check` records the actual argv, exit status, log path, caller identity, and
HEAD before/after execution. Required check commands are registered by the
supervisor, not substituted by worker claims. Supplemental tests are allowed;
the latest recorded failure under any label blocks approval until rerun.
Tests run in the worker checkout. An untracked file, changed HEAD, or changed
tracked file makes a successful exit invalid evidence. Ignored build artifacts
are allowed. Test commands must be reviewed for side effects before enrollment.

A ready verdict requires all baseline checks from both worker and supervisor
on the same SHA, plus a supervisor attestation of diff review, acceptance
coverage, and risks. That is a minimum process gate, not proof that the tests
are adequate or the code is correct. The supervisor must examine the actual
assignment, regression coverage, failure cases, scope, and unresolved findings.
Existing worker model/session inspection remains a separate source of evidence.

## Level two: identity, reliability, and authority

The routing record pins the Herdr API socket/session, project-owner Space,
supervisor pane/native session, worker pane/native session, Git common
directory, isolated worktree, and base commit. No lookup uses a display name,
cohort label, focused pane, newest transcript, or newest Codex chat.

The dispatcher only sends when the project is enabled and the recipient is
idle/done. Review handoffs also wait for the worker to settle. It serializes
supervisor review requests: a delivered or claimed review holds the next one
until a verdict is recorded. Other projects can proceed independently.

State lives in `~/.herdr/coordination/state.json`, outside the repositories.
An exclusive file lock serializes mutations; replacement and directory fsync
make commits atomic and durable. A separate dispatcher lock prevents two timer
instances from sending the same request. Logs live alongside the state and
files are private to the user. This small local-file design targets personal
project volumes, not a multi-user or distributed job service.

Delivery intent is persisted before the external prompt. A timeout or crash
after that point is ambiguous: the request becomes `uncertain`, not an
automatic retry. The supervisor can acknowledge an already received request
or explicitly retry after inspecting the target. Duplicate handoffs deduplicate
by worker identity, SHA, and request kind. Acknowledgements are idempotent.
Overdue acknowledgements (five minutes) and reviews (thirty minutes) appear in
`status`; time alone does not cause another model turn.

Important limits of the current transport:

- Herdr's prompt API has no atomic native-session precondition or idempotency
  key. Identity is checked immediately before submission, but a narrow
  check/send race remains. The adapter cannot provide exactly-once delivery.
- Idle does not establish that the human's input composer is empty. Pause the
  project while using its supervisor interactively, or use a supervisor pane
  dedicated to background work. Enabling explicitly accepts this limitation.
  Pausing prevents future dispatch attempts; it cannot recall an in-flight prompt.
- Native-session changes never silently inherit routing. Resume the same
  recorded session in the same pane, or use explicit `rebind`. Rebinding stays
  in the same project Space/socket, invalidates unfinished review claims, and
  pauses delivery until re-enabled. Worker replacement requires a fresh
  registration; old requests are not rerouted to a different worker.
- This is a cooperative protocol, not an isolation/security boundary between
  agents running as the same Unix user. They share filesystem permissions.
  Scope and approval instructions still apply. Stronger isolation and atomic
  turn control would require integration with the runtime itself.

The default budget is two automatic revision requests per registered worker.
After that, another `revise` verdict becomes `needs_user` without prompting the
worker. There is no token/dollar cap or automatic worker spawning in this
layer. A task needing a User decision cannot submit new candidates until its
supervisor records an actual User decision with an explicit extra-round budget.

Codex `Stop` hooks are deliberately not used as completion detectors. They
signal turn endings, and asynchronous hook output does not start an idle
conversation. Explicit handoffs plus the persistent dispatcher provide the
return path without editing Herdr-managed Codex hooks. See the official
[Codex hooks documentation](https://learn.chatgpt.com/docs/hooks).

## Enrollment and operation

Use the canonical script path below, or a normal dotfiles `bin` PATH entry.
For each project, create a reviewed JSON map of baseline check names to argv
arrays. Commands execute relative to each assigned worker checkout. The
dotfiles example is `herdr/coordination-checks.json`; other repositories need
their own relevant checks, not these dotfiles checks.

From the project's primary Codex conversation in its owner Space:

```bash
~/dotfiles/bin/herdr-coordinate init --checks-file herdr/coordination-checks.json
~/dotfiles/bin/herdr-coordinate status
~/dotfiles/bin/herdr-coordinate mode --enable
```

`init` starts paused unless passed `--enable`. It preserves existing policy on
repeat invocation rather than rewriting an active contract. The launcher
automatically registers future workers only for enrolled, exact supervisor
sessions. Existing workers are not swept into automation. To enroll one,
explicitly use `register` with its verified pane/session, owner session,
worktree, base, task, and original assignment (see `register --help`).

Commit/integrate the implementation and its baseline check scripts before
spawning workers from those refs. A worker launched from an older base that
lacks a required check script correctly fails that baseline; do not waive the
check or silently substitute commands to get it through the queue.

Install the optional recurring dispatcher once:

```bash
systemctl --user link "$HOME/dotfiles/systemd/user/herdr-coordinate.service"
systemctl --user link "$HOME/dotfiles/systemd/user/herdr-coordinate.timer"
systemctl --user daemon-reload
systemctl --user enable --now herdr-coordinate.timer
```

The timer checks pending work every 15 seconds without running a model. Each
project retains its captured socket route; systemd does not need to inherit
the currently focused project's environment. Paused/unregistered projects are
not prompted. `dispatch` is also a one-shot command for testing or another
scheduler. `status --all` is the explicit cross-project operator view.

Worker, after committing the assigned changes:

```bash
~/dotfiles/bin/herdr-coordinate check --all
~/dotfiles/bin/herdr-coordinate handoff --summary 'Worker: implemented X; added regression Y; remaining caveat Z.'
```

For a blocker, use `handoff --kind blocked --summary 'Worker: ...'`; a blocker
does not require a clean checkout or passing tests and cannot be approved as
a reviewed change.

The notification tells its recipient which request to `show` and which
delivery to `ack`. A supervisor then reviews the assignment/diff and uses:

```bash
~/dotfiles/bin/herdr-coordinate check --request REQUEST_ID --all
~/dotfiles/bin/herdr-coordinate review REQUEST_ID --verdict ready \
  --summary 'Codex: reviewed this candidate.' --diff-reviewed \
  --acceptance 'Explain how the requested behavior and regression coverage were checked.' \
  --risks 'Record unresolved risks and untested paths; do not hide missing evidence.'
```

Use `--verdict revise --summary 'Codex: scoped actionable findings...'` for
another worker round, or `--verdict needs_user` for a decision. Workers must
acknowledge revision deliveries before submitting a fresh candidate; do not
rewrite a review candidate or keep editing while it is being reviewed.

Recovery and control are explicit:

```bash
~/dotfiles/bin/herdr-coordinate mode --pause
~/dotfiles/bin/herdr-coordinate retry DELIVERY_ID --reason 'Codex: inspected the recipient; explain why another submission is needed.'
~/dotfiles/bin/herdr-coordinate rebind PROJECT_ID --previous-session OLD_SESSION_ID
~/dotfiles/bin/herdr-coordinate resolve REQUEST_ID \
  --user-decision 'Record the actual new User instruction, not an inferred permission.' \
  --additional-revisions 1
```

`resolve` records a real User decision; it is not an automatic escape from the
revision cap. Nothing here grants permission to merge or clean up a worker.

## Verification and remaining extensions

`python3 tests/test-herdr-coordinate.py` uses temporary repositories, real Git
commits, actual test subprocesses, and a fake Herdr transport. It covers the
review/revision lifecycle, exact-session scoping, evidence gates, stale
candidates, duplicates, failures, timeouts, interrupted delivery, concurrent
dispatch, pause, and recovery. The existing launcher and worker-state suites
remain part of the dotfiles baseline. These tests do not establish live UI
delivery, composer safety, or an atomic Herdr session guard.

Useful later increments are richer sidebar projection, external notifications
for human-needed/overdue items, task-specific check contracts, per-project
spawn/resource limits, and runtime-level atomic prompt/session guards. They
are intentionally separate from this first review-inbox implementation.
