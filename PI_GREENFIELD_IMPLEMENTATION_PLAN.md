# Pi Greenfield Implementation Plan

**Status:** Implementation-ready plan
**Repository:** `thajpo/dotfiles`
**Baseline:** `7c51c237a6635e35fa56f1a84f8625121efdbc10`
**Date:** 2026-08-08
**Activation authority:** Not granted by this plan

## 1. Goal

Build a new Pi system that can replace OpenCode as the primary coding environment.

The new system must let the user:

- Keep one persistent secretary for each project.
- Ask the secretary for project status, direction, risks, and dependencies.
- Start temporary read-only investigations without blocking the main conversation.
- Start durable personal, workstream, and integration coding sessions.
- Continue headful coding sessions after a restart.
- Review exact submitted changes.
- Fast-forward a simple reviewed change when it is safe.
- Use an integration agent when changes must be combined or repaired.
- Approve exact host or external-network commands.
- See all current work, submitted changes, reviews, decisions, and failures in one project view.
- Use normal coding tools with performance close to OpenCode.

OpenCode remains the working environment until every release gate in this plan passes.

This is a fresh start. It is not an old-Pi migration.

## 2. Fixed Product Decisions

These decisions are final for the first release.

### 2.1 Fresh start

- Do not add legacy mode.
- Do not add shadow mode.
- Do not use dual writes.
- Do not keep a compatibility facade.
- Do not import old Pi conversations, workstreams, routes, reviews, or runtime records.
- Do not infer new Pi state from old Pi files.
- Preserve old files and Git state without adopting them.
- Old Pi state can be deleted only by a separate manual cleanup after the new system is accepted.
- Keep normal database upgrade support for future versions of the new system. This is different from importing the old system.

### 2.2 Agent locations

| Role | Location | Persistence | Main access |
|---|---|---:|---|
| Secretary | Host | Durable | One registered project through scoped read tools and exact control operations |
| Investigator | Host | Temporary | One project or working copy through scoped read and web tools |
| Reviewer | Host | Temporary by default | One exact submitted revision through scoped read tools |
| Personal coding agent | Container | Durable | One assigned working copy |
| Workstream coding agent | Container | Durable | One controller-owned working copy and branch |
| Integration agent | Container | Durable | One integration working copy plus selected submitted revisions |
| Host command runner | Host | One command | One exact approved command |
| Container network runner | Container | One command | One exact approved network command or approved package operation |

The repository uses the word **controller**. This plan usually calls it the **host coordinator**: trusted host code that stores project state, starts agents, assigns working copies, and performs approved Git operations.

### 2.3 Read-only agents

Secretary, investigator, and reviewer processes do not get:

- General shell access.
- File write or edit tools.
- Arbitrary Git commands.
- Access to unrelated projects.
- Access to `~/.ssh`, browser files, credentials, unrelated home directories, or old Pi state.
- A writable project Git directory.
- A tool that can start another writer.

Their read tools must check the registered project and allowed working-copy paths on every call. Prompt instructions alone do not count as access control.

### 2.4 Coding agents

Every personal, workstream, and integration agent gets:

- One container.
- One assigned working copy.
- One current task.
- One active writer assignment.
- Web-search tools.
- File read, write, edit, and shell tools inside the assigned execution area.
- No host credentials.
- No Docker socket.
- No SSH agent.
- No host Git credentials.
- No browser session.
- No unrelated project files.
- No direct permission to move the target branch.
- No direct permission to push.

### 2.5 Git writes

A model may change source files, package files, and its assigned working copy. It does not receive unrestricted write access to the project Git directory.

The host coordinator owns:

- Commit creation for submitted revisions.
- The immutable submitted-change refs.
- Updates to the assigned workstream branch.
- Final target-branch updates.
- Review checkout creation.
- Integration setup.
- Rollback refs.

A worker can ask Pi to create a checkpoint or submit a revision. The host coordinator creates the commit from the exact observed working-copy state and updates only the assigned branch or immutable change ref.

This gives agents normal branch-based work without giving arbitrary shell commands permission to move source branches or the target branch.

### 2.6 Simple merge and integration agent

A simple change can use the fast path when all conditions are true:

- One submitted revision is being integrated.
- The working copy was clean at submission.
- Required tests passed.
- One independent reviewer accepted that exact revision.
- Any required package security reviews are current.
- The target branch has not moved since integration analysis.
- The submitted revision is directly ahead of the target.
- The user approves the exact target update.

The host coordinator then fast-forwards the target branch. No integration agent is created.

Use an integration agent when any condition is true:

- Two or more changes must be combined.
- The target branch moved.
- The result cannot fast-forward.
- Source files conflict.
- Package files conflict.
- Changes affect the same design area.
- Combined tests are required.
- The user requests an integration agent.

The integration agent:

- Gets a new integration working copy and branch.
- Receives the exact selected submitted revisions.
- Resolves conflicts and edits the combined result.
- Updates package files when needed.
- Runs tests in its container.
- Submits an integration revision.
- Cannot move source branches.
- Cannot move the target branch.
- Cannot push.
- Cannot approve its own result.

### 2.7 Restart rules

These headful conversations survive restart:

- Secretary.
- Personal coding agent.
- Workstream coding agent.
- Integration agent.
- A reviewer only when explicitly opened as a headful conversation.

A restart preserves:

- Conversation history.
- Project identity.
- Working-copy identity.
- Branch.
- Current task.
- Submitted revisions.
- Progress.
- Attention.
- Pending user decisions.

The old process and container do not have to survive. Pi starts a new process and, for a coding agent, a new container bound to the same conversation and working copy.

Investigators do not survive restart.

After a secretary restart:

- Completed investigator results remain available.
- Running investigators stop or are terminated.
- Incomplete investigators become `interrupted`.
- Pi does not resume them automatically.
- The secretary can start a new investigation.

### 2.8 Messaging

Use three separate paths.

1. `contact_supervisor`
   - Temporary child-to-direct-parent questions.
   - Blocking decisions, interviews, and useful progress.
   - Not durable project messaging.

2. Project messages
   - Worker progress.
   - Needs-user requests.
   - Review requests.
   - Failures.
   - Secretary replies.
   - Stored in the controller database.
   - Bound to project, workstream, conversation, request ID, and worker generation.
   - Retried writes must not create duplicate logical messages.
   - A UI notification is not an acknowledgement.

3. `harness_feedback`
   - Cross-project Pi improvement reports.
   - Separate from project coordination.
   - Does not grant permission.

Remove the generic model-visible `intercom` fallback from the first release. Do not add `pi-intercom` as a control or state authority. A later wake-up adapter may carry only an opaque durable event ID if polling latency becomes a measured problem.

### 2.9 Web and shell network access

All agents may use controlled web tools:

- `web_search`
- `fetch_content`
- `get_search_content`
- `source_check`

Web tools are not general shell internet access.

Normal coding containers have:

- Local loopback networking.
- No general external shell network.
- No published host ports.

External network commands require an exact request and user approval, except for the package operation described below. The approved command runs in a one-command network-enabled container when host access is not required.

### 2.10 Package changes

A coding agent may edit project package files without approval, including:

- `package.json`
- lock files
- `pyproject.toml`
- `uv.lock`
- `Cargo.toml`
- `Cargo.lock`
- `go.mod`
- `go.sum`

A recognized project package operation may use external network access without a separate user prompt when all rules are true:

- It runs in a dedicated container command runner.
- It uses the assigned working copy.
- It uses the reviewed project container image.
- It receives no host credentials.
- It cannot install host-wide software.
- It cannot access another working copy.
- It records the command, package-file changes, and resulting dependency state.
- It writes installed packages only to that working copy's private environment.

Examples include:

- `npm install <package>`
- `uv add <package>`
- `cargo add <package>`
- `go get <module>`

Host-wide package installation always requires a host-command request.

Each working copy gets an independent writable installed-package environment. Download caches may be shared. `node_modules`, `.venv`, Cargo build state, or similar writable environments must not be shared across branches.

The environment identity includes:

- Working-copy ID.
- Package manifest digest.
- Lock-file digest.
- Platform.
- Container image configuration ID.

A package-file change creates or selects a new environment identity.

### 2.11 Package security review

Every new direct dependency must be detected from the package manifest and lock-file change.

The worker records why the dependency is needed.

The secretary decides whether a dependency is non-standard or risky. This is a judgment, not a fixed download-count rule. The secretary may request a security review for any package.

A risky dependency starts a fresh read-only security investigator with web tools. The review checks available evidence such as:

- Exact package name and version.
- Registry identity, integrity, and provenance.
- Registry and source-repository ownership.
- Maintainers and recent ownership changes.
- Known advisories and malicious-package reports.
- Typosquatting or namespace confusion.
- Install scripts.
- Native binaries.
- Downloaded executables.
- Release history and suspicious recent changes.
- Dependency-tree size.
- Relevant risky transitive dependencies.
- Source availability.
- Maintenance state.
- License.
- Safer alternatives.

The report binds to:

- Exact package name and version.
- Exact lock-file digest.
- Exact candidate revision.

A version, lock file, or candidate revision change makes the report stale.

A dependency marked as requiring review cannot become review-ready until the package security report is recorded. The report does not approve the code or authorize integration.

### 2.12 First-release scope

The first release includes:

- Tmux presentation.
- Host secretary.
- Host read-only investigators.
- Host read-only reviewers.
- Container personal agents.
- Container workstream agents.
- Container integration agents.
- Web tools for all agents.
- Project package installation in controlled container runners.
- Package security review.
- Durable worker-to-secretary messages.
- Exact host-command and container-network approval.
- Pi feedback.
- Simple reviewed fast-forward integration.
- Integration-agent workflow.
- Headful restart recovery.
- Project work index.
- Exact installed-process, sandbox, permission, fault, and rollback tests.

Defer:

- Herdr.
- Remote Git publication.
- Old Pi state import.
- Investigator restart recovery.
- Automatic cleanup.
- Strict per-project internet filtering beyond this plan.
- Advanced package-environment sharing.
- Automatic deployment.

## 3. Current Baseline

The baseline includes useful control-plane code, but it is not a working installed product.

### 3.1 Reuse

Keep and refactor these concepts and modules:

- `scripts/pi_control/store.py`
  - Secure SQLite setup.
  - Transactions.
  - Resource-version checks.
- `scripts/pi_control/operations.py`
  - Idempotent operations.
  - Durable intent before external effects.
- `scripts/pi_control/events.py`
  - Transactional event records.
- `scripts/pi_control/errors.py`
  - Structured failures.
- `scripts/pi_control/models.py`
  - Canonical JSON and bounded identifiers.
- `scripts/pi_control/changes.py`
  - Immutable change revisions.
  - Temporary-index capture concepts.
- `scripts/pi_control/reviews.py`
  - Exact-revision review records.
- `scripts/pi_control/integration.py`
  - Target analysis and exact target update concepts.
- `scripts/pi_control/run_manifest.py`
  - Canonical immutable launch records.
  - Secure write and validation.
- `scripts/pi_control/artifacts.py`
  - Immutable artifact records.
- `pi/packages/pi-sandbox-control/`
  - First-party coding sandbox source.
- `pi/packages/pi-subagents-control/`
  - First-party child execution source.
- Build-manifest and package-provenance checks in `install.sh`.
- Useful tmux presentation code after it is reduced to controller-owned conversations.
- The kernel benchmark fixture, route smoke, tool audit, and result formats from the separate benchmark worktree.

### 3.2 Replace or remove

Replace these active designs:

- Root registry as lifecycle authority.
- Legacy secretary state as lifecycle authority.
- Project activation modes.
- Old-state migration import.
- Compatibility facade behavior.
- Hard-coded personal-project panes.
- Natural-language Git authorization.
- Generic model-visible intercom.
- Placeholder full-system tests.
- Broad `tmux kill-server`.
- The current image identity contract.
- Launchers that create routes without creating an exact run record.
- Planned/fake controller client operations that claim support without executing it.

### 3.3 Current direct blockers

The first implementation must address these blockers:

1. `pi-sandbox-control` requires `PI_RUNTIME_MANIFEST`, but ordinary launchers do not create or export it.
2. Docker image producers use local tags, while the runtime contract mixes registry manifest digests and Docker configuration IDs.
3. Natural-language Git authorization can accept descriptive prose as authority.
4. The system action driver runs help, syntax, or no-op commands for many actions.
5. The staged runner proves files and `--help`, not an actual coding tool call.
6. `pi-restart` can kill unrelated tmux sessions.
7. Current schema and client paths contain old migration and project-mode assumptions.
8. There is no proven installed secretary-to-investigator-to-worker-to-review-to-integration journey.

## 4. Target State and File Layout

Use new paths that do not overlap old Pi state.

Recommended final paths:

```text
~/.local/share/pi-system/          installed program and exact packages
~/.local/state/pi-system/          database, sessions, runs, messages, evidence
~/.local/share/pi-system-work/     controller-owned working copies
~/.cache/pi-system/                shared download caches
```

During development, tests use a completely disposable `HOME`, state root, data root, cache root, runtime root, and project set.

The new system must not read these old paths as lifecycle input:

```text
~/.pi/agent/
~/.local/state/pi-control/
~/.local/state/pi-secretary/
~/.local/share/pi/worktrees/
```

The installer may hash old paths before cutover to prove preservation. It must not import their content.

### 4.1 One installed build

The user sees one Pi build.

Installation uses:

1. A sibling staging directory.
2. Complete file and package verification.
3. A launch lock that prevents new Pi starts during replacement.
4. Rename of the old final directory to a rollback directory.
5. Rename of the verified staging directory to the final path.
6. Parent-directory sync.
7. Installed smoke test.
8. Removal of the rollback directory only after acceptance.

This is installer safety. It is not a user-facing version selector.

Before cutover, tests invoke the exact staged executable directly. They do not replace the normal `pi` command.

If first activation fails:

- Disable the new Pi launchers.
- Preserve the new database, sessions, worktrees, refs, and evidence.
- Continue to use OpenCode.
- Do not start old Pi.

## 5. New Authority Model

| Concern | One authority |
|---|---|
| Project, conversation, workstream, run, message, review, and operation state | New Pi SQLite database |
| Source files and submitted content | Git objects and working-copy files |
| Conversation messages | Exact new Pi session JSONL |
| Live writer ownership | Kernel lock plus database writer generation and verified process/container state |
| Container readiness | Exact launch record plus independent container observation |
| Target update | Exact user approval plus expected old and new Git object IDs |
| Presentation | No project or source authority |
| Old Pi state | No authority; preserved external files only |

A missing process, old timestamp, dead tmux pane, or marker file does not prove that writer access is safe to reassign.

## 6. New Database Shape

Start a new schema epoch in the new state root. Do not upgrade or import an old Pi database.

Keep the normal schema-migration framework for later upgrades of the new product.

### 6.1 Keep or rebuild

The fresh schema includes:

- `control_meta`
- `schema_migrations`
- `installed_builds`
- `projects`
- `working_copies`
- `conversations`
- `workstreams`
- `runs`
- `changes`
- `change_revisions`
- `change_revision_inputs`
- `reviews`
- `integration_attempts`
- `authorizations`
- `operations`
- `control_events`
- `event_consumers`
- `attention`
- `presentation_assignments`
- `artifact_manifests`
- `child_terminal_records`

### 6.2 Remove

The fresh schema does not include:

- `migration_runs`
- `migration_manifests`
- `migration_resource_mappings`
- `project_activations`
- Legacy, shadow, or controller project modes
- Old external identity mapping tables

Remove matching operations, API fields, commands, tests, and documentation.

Do not remove the normal `schema_migrations` table or the ability to upgrade the new schema later.

### 6.3 Add durable project messages

Add `project_messages`.

Required fields:

```text
message_id
project_id
workstream_id, optional
conversation_id
run_id
writer_generation, optional
kind
request_id, optional
idempotency_key
payload_json
state
created_at
delivered_at, optional
acknowledged_at, optional
resolved_at, optional
reply_to_message_id, optional
```

Supported kinds:

```text
progress
needs-user
decision-reply
review-requested
failure
interrupted
submitted-change
package-review-required
package-review-complete
```

Required behavior:

- A repeated `idempotency_key` with the same content returns the existing message.
- A repeated key with different content fails.
- A stale writer generation cannot create a message for a newer run.
- Cross-project references fail.
- Delivery and acknowledgement are separate.
- Restart does not lose an unacknowledged message.
- The project index is derived from state and messages, not from pane text.

### 6.4 Add command requests

Add `command_requests`.

Required fields:

```text
command_request_id
project_id
workstream_id
conversation_id
run_id
writer_generation
execution_place: container-network | host
command
working_directory
required_resource
purpose
expected_effect
change_scope_json
expected_output
sensitive_output
expected_duration_ms
request_digest
state
authorization_id, optional
result_json, optional
created_at
expires_at
completed_at, optional
```

States:

```text
requested
approved
rejected
running
succeeded
failed
expired
cancelled
```

The approval binds the exact request digest. A changed command, directory, resource, scope, or execution place requires a new request.

### 6.5 Add dependency review records

Add:

- `dependency_changes`
- `package_security_reviews`

A dependency change binds:

- Project.
- Change and revision.
- Ecosystem.
- Direct package name.
- Exact version.
- Manifest path and digest.
- Lock path and digest.
- Worker reason.
- Secretary disposition: `standard`, `review-required`, or `rejected`.

A package security review binds:

- Dependency-change ID.
- Exact candidate revision.
- Investigator run.
- Evidence.
- Risk level.
- Recommendation.
- Completion state.

A required review must be current before code review can enter an accepted state.

### 6.6 Update authorization kinds

Keep exact one-use approvals for:

```text
create-workstream
start-integration
integrate-change
host-command
container-network-command
archive-conversation
retire-workstream
close-change
```

Package installation does not create a user authorization when it matches the safe project-package operation contract. It still creates an operation and evidence record.

Remove:

```text
migration-resolve
migration-cutover
activation-change
publish
cleanup
```

Remote publication and cleanup are not first-release actions.

## 7. Launch and Runtime Design

### 7.1 Exact launch record

A **run manifest** is the exact launch record for one process. It states what project, conversation, working copy, role, build, and execution area the process is allowed to use.

Add one host operation named `run.prepare`.

It must:

1. Read the exact project, conversation, working copy, and role.
2. Check the installed build.
3. Check expected resource versions and Git object IDs.
4. Acquire the writer lock when the role writes.
5. Increase the writer generation for a new writer.
6. Create a run row.
7. Create a one-use capability.
8. Create the exact run manifest.
9. Write it under the new state root.
10. Return the exact environment and command arguments.
11. Leave the launcher process alive so its process identity remains stable through `exec`.

A production manifest must not use:

- An all-zero image ID.
- `piVersion: unknown`.
- A guessed platform.
- A guessed working copy.
- The newest build by path.
- A caller-selected trust mode.

### 7.2 Host roles

Secretary, investigator, and reviewer manifests use:

```text
execution target: host-read-only
```

They do not load the coding sandbox.

They load only scoped read, web, communication, review, and control tools for their role.

### 7.3 Coding roles

Personal, workstream, and integration manifests use:

```text
execution target: container
```

They load the first-party sandbox and exact writer tool set.

The launcher exports the canonical manifest path as `PI_RUNTIME_MANIFEST`.

### 7.4 Docker image identity

Keep different identifiers separate:

```text
image_reference       local tag or registry reference used for create
image_config_id       exact Docker image configuration ID
registry_digest       optional registry manifest digest
platform              exact OS and architecture
base_image_config_id  exact base image ID when a derived image is used
runtime_spec_hash     digest of the complete runtime specification
```

Do not require unrelated identifiers to be equal.

For a local tag:

1. Inspect the tag immediately before container creation.
2. Require its configuration ID to equal the recorded ID.
3. Create the container.
4. Inspect the container independently.
5. Require the actual image configuration ID, platform, mounts, user, and labels to match the run manifest.

### 7.5 Git inside coding containers

The model must not receive a writable project Git directory.

Use this first-release approach:

- Mount the assigned working-tree files read/write.
- Expose project Git objects and refs read-only for inspection.
- Keep the worktree index and temporary merge state in controller-owned writable storage when required.
- Set `GIT_OPTIONAL_LOCKS=0` for read commands.
- Do not allow shell Git commands to update project refs.
- Create commits and update the assigned branch through host coordinator operations.
- Import submitted objects and create immutable change refs on the host.
- Make all host ref updates name exact expected old and new object IDs.
- Disable repository hooks for controller Git operations.

For integration:

1. The host coordinator creates the integration working copy.
2. The host coordinator prepares the selected exact revisions and merge state.
3. The integration agent resolves files and runs tests.
4. The host coordinator creates the integration commit from the observed result.
5. The integration agent submits it for review.
6. Final target movement remains a separate approved host operation.

If the current sandbox cannot support this split cleanly, stop the slice and redesign the Git mount boundary. Do not fall back to a writable common Git directory.

## 8. Ordered Implementation Slices

Each slice must be reviewed and accepted before the next slice depends on it.

The implementation agent receives only one slice at a time, with this document and the exact current repository state.

---

## Slice 0 — Reset the contracts and remove old product assumptions

### Goal

Make the repository describe one greenfield product before more implementation is added.

### Main files

Modify:

- `pi/control-plane/PRODUCT_CONTRACT.md`
- `pi/control-plane/STATE_CONTRACT.md`
- `pi/control-plane/EXECUTION_CONTRACT.md`
- `pi/control-plane/CHANGE_INTEGRATION_CONTRACT.md`
- `pi/control-plane/OBSERVABILITY_CONTINUITY_CONTRACT.md`
- `pi/control-plane/SYSTEM_INTEGRATION_TEST_PLAN.md`
- `pi/control-plane/ACCEPTANCE_PLAN.md`
- `pi/SECRETARY_WORKFLOW.md`
- `pi/README.md`
- `README.md`

Replace:

- `pi/control-plane/COMPLETION_IMPLEMENTATION_PLAN.md`
  - Replace its content with this greenfield plan or make this file the controlling plan.
- `pi/control-plane/MIGRATION_ACTIVATION_PLAN.md`
  - Replace with `GREENFIELD_CUTOVER_AND_ROLLBACK.md`.
- `pi/control-plane/PHASE_11D_CANARY_RUNBOOK.md`
  - Replace with `PRE_ACTIVATION_ACCEPTANCE_RUNBOOK.md`.

Update validation:

- `tests/system/validate_plan_docs.py`
- `tests/system/action-manifest.v1.json`
- `tests/system/launcher-surface.v1.json`
- `tests/system/configured-packages.v1.json`
- `tests/system/loaded-extensions.v1.json`

### Work

- Remove legacy, shadow, dual-write, migration-import, compatibility, and project-activation requirements.
- Fix the first-release role table.
- Fix restart behavior.
- Fix the package and package-security rules.
- Fix the messaging rules.
- Remove Herdr and remote publication from first-release acceptance.
- State that old Pi files are preserved but never adopted.
- Mark all unsupported actions accurately.
- Add a repository validator that rejects old project-mode and migration-import terms in active product contracts, except in explicit removal notes.

### Acceptance

Run:

```bash
python3 -m tests.system.validate_plan_docs
bash tests/system/run-contract.sh
git diff --check
```

Add tests that fail if active contracts contain:

```text
legacy -> shadow -> controller
project activation mode
old conversation import
compatibility facade
dual writer
```

### Stop conditions

Stop and investigate if:

- Any supported launcher still needs old Pi state to identify a project or conversation.
- The new schema cannot be defined without importing old IDs.
- A document still names migration readiness as a release gate.

---

## Slice 1 — Build a real installed-process test driver

### Goal

Create a trustworthy test path before adding more product code.

### Main files

Create or replace:

- `tests/system/fixtures/scripted-provider.ts`
- `tests/system/fixtures/installed-pi.py`
- `tests/system/loaded_resource_probe.ts`
- `tests/system/action_driver.py`
- `tests/system/process_fixture.py`
- `tests/system/driver.py`
- `tests/system/evidence.py`
- `tests/system/run-process-fixture.sh`
- `tests/system/run-staged-installed.sh`
- `tests/system/action-manifest.v1.json`
- `tests/system/evidence.schema.json`

### Work

The driver must launch the exact staged Pi executable and:

- Use a deterministic test provider.
- Drive an actual prompt and tool call through Pi.
- Record exact package and extension paths.
- Record file hashes.
- Record actual registered tools.
- Invoke one allowed tool.
- Attempt one forbidden tool and prove it is absent or rejected.
- Record session JSONL.
- Record process arguments and environment after redaction.
- Record working-copy and host filesystem state before and after.
- Never call a remote model provider.
- Write evidence outside the repository.

Remove these as full-system success substitutes:

- `pi help`
- `--help`
- syntax checks
- direct module imports
- package counts
- `noLiveAction: true`

They may remain as narrow static tests, but not as T2 or installed-process proof.

### Acceptance

A minimal installed journey must prove:

1. Exact staged build loaded.
2. Exact first-party package loaded.
3. Exact extension loaded.
4. Actual tool schema registered.
5. Actual read tool invoked.
6. Forbidden write tool absent in a read-only role.
7. Evidence binds the exact build and fixture.

Run twice from two clean disposable environments.

### Stop conditions

Stop if:

- Pi cannot use a deterministic local provider through its supported process interface.
- The driver must patch production code to select test behavior.
- Loaded paths cannot be observed from a real Pi process.
- A test passes without an actual tool invocation.

---

## Slice 2 — Create the fresh schema and controller API

### Goal

Make the new database the only lifecycle store for the new system.

### Main files

Modify:

- `scripts/pi_control/schema.py`
- `scripts/pi_control/store.py`
- `scripts/pi_control/client.py`
- `scripts/pi_control/cli.py`
- `scripts/pi_control/models.py`
- `scripts/pi_control/operations.py`
- `scripts/pi_control/events.py`
- `scripts/pi_control/reconcile.py`
- `bin/pi-control`

Create:

- `scripts/pi_control/projects.py`
- `scripts/pi_control/conversations.py`
- `scripts/pi_control/messages.py`
- `scripts/pi_control/command_requests.py`
- `scripts/pi_control/dependencies.py`
- A new schema migration package for future greenfield upgrades.

Retire from the fresh schema and supported API:

- `scripts/pi_control/activation.py`
- Old migration-import modules and commands.
- `project_activations`.
- Migration tables.
- Migration authorizations.
- Planned fake operation responses.

Do not delete the normal future schema-upgrade framework.

### Work

Implement real operations for:

```text
project.register
project.status
project.work-index
conversation.create
conversation.focus
conversation.archive
message.post
message.list
message.acknowledge
message.reply
command.request
command.authorize
command.reject
dependency.detect
dependency.disposition
package-review.record
```

Project registration:

- Requires an explicit repository path.
- Resolves the Git common directory and object format.
- Records the primary checkout.
- Records the approved project boundary.
- Creates a new project ID.
- Does not read old Pi files.
- Inventories unmanaged Git worktrees as observations only.

Project work index returns:

```text
Working now
Investigations
Changes ready for review
Changes ready to merge
Needs attention
Integrated recently
Unmanaged Git work
```

Each managed row includes:

- Human title.
- Agent type.
- Branch.
- Current state.
- Last useful update.
- Test state.
- Review state.
- Whether user action is required.
- One exact focus or resume target.

### Acceptance

Use a disposable Git repository and fresh state root.

Prove:

- Registration creates only new state.
- Old-Pi sentinel files remain byte-identical.
- Duplicate repository registration returns the same project or an exact conflict.
- A copied clone is not silently treated as the same project.
- Unmanaged worktrees are visible but not adopted.
- Project messages survive controller process restart.
- Duplicate message submission is idempotent.
- Cross-project message forgery fails.
- Stale writer generation fails.
- Project index output is stable and bounded.

### Stop conditions

Stop if:

- A supported API operation still returns only `planned: true`.
- Project status depends on old secretary records.
- A project path alone silently changes project identity.
- The work index needs tmux pane state as lifecycle truth.

---

## Slice 3 — Wire exact launch records and real Docker runtime

### Goal

Make one real coding agent read, edit, run shell commands, and test through the new controller path.

### Main files

Modify:

- `scripts/pi_control/run_manifest.py`
- `scripts/pi_control/runtime_adapter.py`
- `scripts/pi-runtime.py`
- `scripts/pi-workspace.py`
- `pi/packages/pi-sandbox-control/src/manifest-adapter.ts`
- `pi/packages/pi-sandbox-control/src/index.ts`
- `pi/extensions/pi-sandbox.json`
- `bin/pi`
- `bin/pi-review-agent`
- `install.sh`

Create:

- `scripts/pi_control/launch.py`
- `scripts/pi_control/docker_runtime.py`
- `scripts/pi_control/writer_lock.py`
- `bin/pi-workstream`
- `bin/pi-integration`

Update:

- `tests/pi-sandbox-control-manifest.test.mjs`
- `tests/pi-docker-control-plane-e2e.sh`
- `tests/pi-docker-integration.sh`
- `tests/pi-docker-isolated-ownership.sh`
- `tests/pi-docker-runtime-cache.sh`
- `tests/system/run-docker.sh`

### Work

- Implement `run.prepare`, `run.attest`, `run.start`, `run.stop`, and `run.reconcile`.
- Pass `PI_RUNTIME_MANIFEST` to the exact Pi process.
- Remove production defaults for unknown build, image, platform, or Pi version.
- Separate image reference, image configuration ID, optional registry digest, and runtime-spec hash.
- Create and attest a real Docker container.
- Bind one writer generation to one working copy.
- Hold the writer lock for the process lifetime.
- Prevent a second writer when process or container state is unknown.
- Remove ordinary unsandboxed host fallback.
- Implement the Git write boundary described in Section 7.5.
- Make the container default to no external network.
- Keep loopback.
- Verify no host secrets or sockets enter the container.

### Acceptance

A real installed coding loop must:

1. Register a disposable project.
2. Create a coding conversation.
3. Prepare an exact run.
4. Start a real container.
5. Load the exact first-party sandbox.
6. Read a file.
7. Edit a file.
8. Run a local test.
9. Show the expected working-copy delta.
10. Leave unrelated host paths unchanged.
11. Reject a second writer.
12. Stop and restart with a new run ID.
13. Reuse the same conversation and working copy.
14. Prove actual image, platform, user, mounts, tools, and build identity.

Run the full Docker script set from `tests/system/run-docker.sh`.

### Stop conditions

Stop if:

- The container gets a writable project Git common directory.
- Image config ID and registry digest are still treated as the same value.
- A route file can override the run manifest.
- A stale process can write after a new writer starts.
- Tests use fabricated Docker observations.

---

## Slice 4 — Build host secretary, investigator, reviewer, and tmux surface

### Goal

Prove the read-only host side of the product.

### Main files

Modify:

- `pi/extensions/secretary/index.ts`
- `pi/extensions/secretary-subagents/index.ts`
- `pi/extensions/secretary-investigator-git/index.ts`
- `pi/extensions/review-receipt/index.ts`
- `pi/extensions/observability/index.ts`
- `bin/pi-secretary`
- `bin/pi-review-agent`
- `bin/pi-start`
- `bin/pi-restart`
- `bin/pi-help-custom`

Create:

- `pi/extensions/scoped-project-read/index.ts`
- `pi/extensions/scoped-project-read/core.mjs`
- `scripts/pi_control/presentation.py`
- `scripts/pi_control/investigators.py`

Remove from first-release launch surfaces:

- Herdr flags and launchers.
- Built-in unrestricted host `read`, `grep`, `find`, and `ls` if they can escape the project.
- Generic `intercom`.
- Legacy secretary control paths.
- Root registry lookup.

### Work

Secretary:

- One durable conversation per registered project.
- No general shell, write, or edit.
- Uses project status and project work index.
- Starts temporary investigators.
- Starts or focuses headful workstreams after exact approval.
- Receives durable project messages.
- Requests exact reviews.
- Presents integration choices.

Scoped host read tools:

- Accept only controller project and working-copy IDs.
- Resolve paths through the controller.
- Reject `..`, symlink escapes, replaced roots, and other projects.
- Bound bytes, lines, matches, and file counts.
- Disable external Git helpers, text conversion, hooks, and configuration injection.
- Provide read-only Git status, log, show, diff, branch, and worktree inventory.

Reviewer:

- Binds one exact change revision.
- Receives a detached read-only view.
- Has no mutation tool.
- Submits one exact review receipt.

Tmux:

- Creates or focuses only controller-owned conversations.
- Uses controller labels.
- Does not create identity.
- `pi-restart` stops only exact managed Pi sessions.
- Never calls broad `tmux kill-server`.
- Preserves unrelated sessions.

Investigators:

- Run asynchronously.
- Have no shell, write, edit, worktree, persistence, or further-spawn tool.
- Completed results persist.
- Running investigators become interrupted after secretary restart.
- Are not resumed.

### Acceptance

Prove with a real installed secretary:

- It reads an allowed project file.
- It cannot read another project.
- It cannot read `~/.ssh`.
- It rejects symlink escape.
- It has no shell, write, or edit tool.
- It starts at least two asynchronous investigators.
- One completes.
- One fails.
- One requests a decision.
- Results and events appear in the project index.
- Restart marks the active investigator interrupted.
- Completed result remains.
- A reviewer sees only the exact submitted revision.
- Unrelated tmux sessions survive `pi-restart`.

### Stop conditions

Stop if:

- Read-only safety depends only on the system prompt.
- A reviewer can see a moved branch instead of its exact revision.
- Tmux pane state creates or rebinds project identity.
- Restart can kill an unrelated process or session.

---

## Slice 5 — Add durable worker-to-secretary messages and exact command approval

### Goal

Make asynchronous work usable during long coding sessions and restarts.

### Main files

Modify:

- `pi/extensions/workstream-channel/index.ts`
- `pi/extensions/host-command/index.ts`
- `pi/extensions/host-command/core.mjs`
- `pi/packages/pi-subagents-control/src/intercom/native-supervisor-channel.ts`
- `pi/packages/pi-subagents-control/src/intercom/intercom-bridge.ts`
- `pi/packages/pi-subagents-control/src/intercom/result-intercom.ts`
- `pi/extensions/secretary/index.ts`
- Worker and integration agent definitions under `pi/agents/`

Create:

- `pi/extensions/project-messages/index.ts`
- `pi/extensions/project-commands/index.ts`
- `scripts/pi_control/network_runner.py`
- Controller message and command tests.

### Work

- Keep `contact_supervisor` for direct temporary child-to-parent contact.
- Remove model-visible generic `intercom`.
- Replace `notify_secretary` legacy calls with controller-backed `message.post`.
- Let a restarted worker list unresolved replies for its conversation and generation.
- Require explicit acknowledgement.
- Format command requests clearly, but store the full structured fields now.
- Separate:
  - `container-network` request.
  - `host` request.
- Display exact request details in secretary UI.
- Create one-use approval bound to the request digest.
- Run a container-network command in a one-command helper container.
- Run a host command with a minimal host environment.
- Expire approval after use, request change, or restart.
- Return bounded output to the requesting worker.
- Prompt workers to use `harness_feedback` when a normal coding task exposes a missing Pi capability.

### Acceptance

Prove:

- Worker posts `progress`.
- Worker posts `needs-user`.
- Secretary reads and replies.
- Worker restarts and receives the unresolved reply.
- Worker acknowledges it.
- Duplicate post does not create a duplicate message.
- A stale worker generation cannot post.
- A different project cannot reply.
- UI delivery without database acknowledgement leaves the message pending.
- Host command approve, reject, fail, timeout, and stale cases work.
- Container-network command approve and reject cases work.
- A changed command invalidates approval.
- A correctly host-only operation does not create automatic harness feedback.
- A normal blocked coding operation can create bounded harness feedback.

### Stop conditions

Stop if:

- Project messages are stored only in temp files.
- Secretary reply requires the original worker process to remain alive.
- Approval can be reused.
- The container-network path silently executes on the host.
- Generic intercom remains a second project message system.

---

## Slice 6 — Complete personal agents, workstreams, package environments, and package security

### Goal

Make normal feature work practical.

### Main files

Modify:

- `scripts/pi_control/workstreams.py`
- `scripts/pi_control/client.py`
- `scripts/pi_control/cli.py`
- `bin/pi-personal`
- `bin/pi-workstream`
- `pi/extensions/workstream-brief/index.ts`
- `pi/extensions/workflow-state/index.ts`
- `pi/extensions/task-packet/index.ts`
- `pi/agents/worker.md`
- `pi/agents/planner.md`
- `pi/agents/reviewer.md`
- `pi/settings.json`

Create:

- `scripts/pi_control/package_environment.py`
- `scripts/pi_control/package_diff.py`
- `pi/extensions/dependency-review/index.ts`
- Package security investigator definition.
- Workstream and package system tests.

### Work

Personal agents:

- Are created from registered project state.
- Use controller conversations, not hard-coded machine project lists.
- Default to a controller-owned working copy for first-release safety.
- May use the primary checkout only through an explicit selection that records its current dirty state.
- Submit changes through the same change queue as workstreams.

Workstreams:

- Creation is one durable operation.
- Create working copy, branch, conversation, task, writer assignment, and tmux presentation.
- Report ready only after all required resources are verified.
- Keep one writer.
- Allow nested read-only investigators through `contact_supervisor`.
- Persist useful progress and attention.

Package environments:

- Detect package ecosystem.
- Derive exact environment identity.
- Use one writable environment per working copy and identity.
- Share only download caches.
- Run recognized package operations through the network command runner.
- Record command and package-file delta.
- Reject host-global package commands.

Package security:

- Detect every new direct dependency.
- Require worker reason.
- Let secretary record standard, review-required, or rejected.
- Start a package security investigator for review-required dependencies.
- Bind result to exact version, lock digest, and candidate revision.
- Block review-ready state when a required report is missing or stale.

### Acceptance

Prove:

- Two workstreams have separate branches, files, containers, and package environments.
- Package X on branch A does not appear in branch B's installed environment.
- A shared download cache does not create shared writable installed state.
- A package install script cannot access host credentials or another project.
- Package manifests and lock files are captured in the submitted revision.
- New direct dependencies are detected.
- A risky dependency blocks review-ready state.
- A completed exact package report opens the gate.
- Changing the version or lock file makes the report stale.
- Personal and workstream changes enter the same review queue.
- One worker cannot write another working copy.

### Stop conditions

Stop if:

- Writable package environments are shared across branches.
- Package operations run with host credentials.
- Package risk judgment is made by the worker that selected the package.
- A required package report is not bound to an exact revision.
- Workstream creation can report success after only partial setup.

---

## Slice 7 — Complete change, review, fast-forward, and integration-agent workflows

### Goal

Prove the full local delivery path.

### Main files

Modify:

- `scripts/pi_control/changes.py`
- `scripts/pi_control/reviews.py`
- `scripts/pi_control/integration.py`
- `scripts/pi_control/authorizations.py`
- `scripts/pi_control/workstreams.py`
- `scripts/pi_control/client.py`
- `pi/extensions/secretary/index.ts`
- `pi/extensions/review-receipt/index.ts`
- `bin/pi-review-agent`
- `bin/pi-integration`

Remove:

- `pi/extensions/secretary/authorization.ts`
- Any natural-language parser that directly grants Git authority.
- First-release publication tools and commands.

### Work

Submission:

- Capture exact working-copy status.
- Create exact commit through the host coordinator.
- Create immutable submitted ref.
- Record changed paths, tests, package state, and provenance.
- Update only the assigned branch through exact expected-old-object update.
- Keep revision immutable.

Review:

- Bind exact revision.
- Bind exact package security state.
- Record verdict and evidence.
- New revision makes prior review stale.
- Worker cannot review its own revision as the required independent review.

Fast-forward path:

- Analyze exact target object.
- Require current accepted review.
- Require current package reports.
- Display target, source, tests, review, changed paths, and effects.
- Create one-use approval.
- Recheck target under lock.
- Fast-forward with exact expected old object.
- Do not push.

Integration-agent path:

- Select exact input revisions.
- Create integration working copy and conversation.
- Prepare combined state.
- Let integration agent resolve and test.
- Submit a new integration revision linked to every input.
- Require independent review.
- Require exact user approval.
- Update target through expected-old-object check.
- Do not push.

### Acceptance

Run these scenarios:

1. Submit revision 1.
2. Review revision 1.
3. Submit revision 2.
4. Prove revision 1 review is stale.
5. Review revision 2.
6. Move the target.
7. Prove simple fast-forward refuses.
8. Start integration agent.
9. Resolve target movement or conflicts.
10. Submit and review integration result.
11. Approve exact target update.
12. Prove target moved to the approved object only.
13. Prove no remote changed.
14. Retry completed operations and receive the recorded result.
15. Inject a crash after ref update and before database completion.
16. Reconcile without repeating an unsafe effect.
17. Prove repository hooks did not run.

### Stop conditions

Stop if:

- A review verdict itself authorizes integration.
- Generic `yes` authorizes a target update.
- A worker can move the target.
- Integration reads a mutable branch instead of the selected exact revisions.
- A retry can publish or integrate a newer source object than the approved one.

---

## Slice 8 — Restart, recovery, permission, and fault acceptance

### Goal

Show that the system remains correct during failures.

### Main files

Modify:

- `scripts/pi_control/reconcile.py`
- `scripts/pi_control/process_adapter.py`
- `scripts/pi_control/docker_runtime.py`
- `scripts/pi_control/writer_lock.py`
- `scripts/pi_control/presentation.py`
- `bin/pi-restart`
- `bin/pi-start`
- `tests/system/fault_driver.py`
- `tests/system/scenarios/recovery_security.py`
- `tests/system/run-presentation.sh`
- `tests/system/run-docker.sh`

### Work

Add deterministic failures:

- Before durable intent.
- After durable intent.
- After working-copy creation.
- After container creation.
- After process start.
- After Git commit creation.
- After ref update.
- Before database completion.
- After database completion.
- Before message notification.

Add observations for:

- Correct process.
- Dead process.
- Reused PID.
- Unobservable process.
- Correct container.
- Stopped container.
- Stale container.
- Foreign container.
- Wrong image.
- Wrong mount.
- Wrong user.
- Wrong working-copy path.
- Wrong file ownership.
- Group-writable state.
- Symlinked state.
- Replaced inode.
- Retained child lock descriptor.
- Unknown tmux state.

Recovery rules:

- Resume only when one exact continuation is proven.
- Preserve ambiguous refs and working copies.
- Mark uncertainty as `needs attention`.
- Never grant a new writer while an old writer may still have access.
- Never infer completion from a file or container name alone.
- Never delete automatically in the first release.

### Acceptance

Prove:

- Secretary restart restores the same conversation.
- Worker restart restores the same conversation and working copy with a new run.
- Integration restart restores the same integration task.
- Investigators become interrupted, not resumed.
- Pending user decisions remain.
- Unrelated tmux sessions remain.
- A reused PID is not accepted as the old process.
- A stale container cannot satisfy attestation.
- Wrong owner, mode, symlink, or inode fails closed.
- Crash recovery reaches succeeded, safely retryable, or needs-attention state.
- No scenario silently chooses a destructive repair.

### Stop conditions

Stop if:

- Recovery uses timestamps as authority.
- PID absence grants a writer.
- Unknown container state is treated as stopped.
- Restart can produce two live writers.
- A fault test cannot prove the host namespace remained unchanged.

---

## Slice 9 — Exact installation, rollback, and OpenCode comparison

### Goal

Prove the exact built system can replace OpenCode without risking existing work.

### Main files

Modify:

- `install.sh`
- `tests/run-candidate-tests.sh`
- `tests/run-control-plane-candidate-tests.sh`
- `tests/system/run-staging-gate.sh`
- `tests/system/run-staged-installed.sh`
- `tests/system/evidence.schema.json`
- `pi/control-plane/PRE_ACTIVATION_ACCEPTANCE_RUNBOOK.md`
- Root and Pi README files.

Port, do not merge wholesale:

- Benchmark fixtures.
- Route smoke.
- Tool ownership audit.
- Result schemas.
- Active-build identity checks.

Do not merge:

- `feature/extended-agent-interaction-surfaces`
- Its rejected hashline edit changes.
- Its Bash-compression changes.
- Unrelated old harness code.

### Work

Installation:

- Build in a clean staging directory.
- Verify exact file set and hashes.
- Verify exact first-party package bytes.
- Verify Pi version.
- Run the complete installed journey twice in clean disposable environments.
- Run the full Docker and permission matrix.
- Record evidence outside the repository.
- Install atomically only after all pre-activation gates pass.
- Start with a new production database.
- Do not read old Pi state.
- Run a short installed smoke test.
- Register real projects only after smoke passes.

Rollback:

- Stop exact new Pi processes.
- Preserve new database, sessions, worktrees, refs, changes, and evidence.
- Disable new Pi launchers.
- Restore the pre-cutover command state.
- Verify OpenCode still works.
- Verify old Pi files and Git state are unchanged.
- Do not start old Pi.

OpenCode comparison:

Use the same:

- Repository fixture.
- Task.
- Starting bytes.
- Model.
- Reasoning level.
- Tool intent.
- Validation.
- Repetitions.

Compare the single-agent coding loop, not secretary overhead.

Initial release thresholds:

- No lower task success rate than OpenCode on the accepted task set.
- Zero unexpected modified paths.
- No unrecovered tool or sandbox failure.
- Median end-to-end latency no more than 20% worse.
- Median model tokens no more than 20% worse unless task success improves.
- Median tool calls no more than 20% worse unless task success improves.
- User intervention rate no worse.
- Ten representative real-work tasks complete without an OpenCode rescue.

Thresholds may be changed only before the comparison starts. Record the reason and new values.

### Acceptance

The release candidate must complete the final journey in Section 9 and meet every gate in Section 10.

### Stop conditions

Stop if:

- The staged and installed byte sets differ.
- A real process loads a repository source path instead of the installed path.
- Rollback loses or deletes new work.
- Old Pi state changes.
- OpenCode becomes unavailable before final acceptance.
- Performance or correctness misses the declared threshold.

## 9. Complete Installed Acceptance Journey

Run this in a disposable project with the exact staged installed generation.

1. Build and attest the exact generation.
2. Create a fresh database.
3. Register one disposable Git project.
4. Start the tmux secretary.
5. Ask for the project work index.
6. Launch multiple asynchronous investigators.
7. Record their actual tools.
8. Prove forbidden tools are absent.
9. Receive completion, failure, and needs-user events.
10. Create one personal coding conversation.
11. Create two workstream conversations.
12. Confirm exact project, working copy, branch, conversation, run, build, image, mounts, user, and writer generation.
13. Read, edit, run shell, and run local tests in one worker.
14. Prove only its assigned working copy changed.
15. Launch a nested read-only investigator.
16. Prove nested write, edit, shell, worktree, persistence, and spawn authority are absent.
17. Post worker progress to the secretary.
18. Post a needs-user request.
19. Reply from the secretary.
20. Restart the worker.
21. Receive and acknowledge the unresolved reply.
22. Request one container-network command.
23. Approve it and prove it ran only in the container.
24. Request one host command.
25. Reject it and prove it did not run.
26. Approve a different exact host command.
27. Restart and prove stale approval cannot be reused.
28. Add a standard project package.
29. Prove its private environment is isolated from the other workstream.
30. Add a dependency that the secretary marks review-required.
31. Run a package security investigation.
32. Bind the report to the exact version, lock digest, and revision.
33. Change the version and prove the report becomes stale.
34. Complete a new package review.
35. Submit an immutable feature revision.
36. Prove the worker cannot integrate it.
37. Create an exact read-only reviewer.
38. Submit an accepted exact review.
39. Fast-forward the unchanged target after exact approval.
40. Prove no remote changed.
41. Complete a second independent feature.
42. Move the target or create a conflict.
43. Prove simple integration refuses.
44. Create an integration agent from exact submitted revisions.
45. Resolve, edit, install merged packages if needed, and run combined tests.
46. Submit the integration revision.
47. Review the exact integration revision.
48. Approve the exact target update.
49. Prove the target moved only to the approved object.
50. Restart the complete Pi tmux surface.
51. Prove secretary, personal, workstream, integration, change, review, message, and attention identities remain correct.
52. Prove active investigators became interrupted.
53. Prove unrelated tmux sessions survived.
54. Inject representative crashes and reconcile.
55. Run the project work index and account for every managed item.
56. Roll back the installed generation.
57. Prove all new work remains preserved.
58. Prove old Pi files and original Git state outside the test project remain unchanged.
59. Prove OpenCode remains usable.

## 10. Release Gates

All gates must pass. A missing gate is not a pass.

| Gate | Required proof |
|---|---|
| Contract | Greenfield contracts agree; no active old-mode or import path |
| Component | Schema, transactions, messages, approvals, changes, reviews, integration, package gate |
| Installed process | Exact installed Pi loads exact packages and invokes actual tools |
| Host read-only | Secretary, investigator, and reviewer path restrictions |
| Coding sandbox | Real container, exact image, mounts, user, tools, and one-writer rule |
| Network | Web tools work; shell egress blocked; approved runner works |
| Packages | Per-working-copy environment and package security gate |
| Async work | Multiple investigators, durable messages, attention, and decisions |
| Restart | Headful recovery and investigator interruption |
| Review | Exact revision, stale review, independent reviewer |
| Integration | Simple fast-forward and integration-agent path |
| Faults | Crash, PID, container, permission, symlink, and replay matrix |
| Presentation | Tmux focus and restart without unrelated-session loss |
| Installation | Exact staged and installed bytes; two clean repetitions |
| Rollback | New work preserved; new Pi disabled; old state unchanged |
| OpenCode comparison | Correctness and declared performance thresholds |
| Human use | User can understand and operate the project work index and approval cards |

## 11. Files to Retire After Replacement Is Proven

Do not delete these at the start. Delete them only after the replacement path has real installed-process acceptance.

Candidates:

- `scripts/pi-secretary-control.py`
- `scripts/pi-root-session.py`
- `bin/pi-root-session`
- Old root-session extension and registry code.
- Old secretary state readers and writers.
- Old migration import and reconciliation modules.
- `scripts/pi_control/activation.py` if no remaining build-provenance use exists.
- Project activation CLI and schema code.
- Generic intercom bridge and model-visible `intercom` tool.
- Herdr launchers and first-release action entries.
- Hard-coded personal project layout code.
- Remote publication launch surfaces.
- Obsolete migration and canary documents.
- Tests that validate removed legacy behavior.

Keep old user files on disk. Removing code from the repository does not authorize deleting user state.

## 12. Code That Must Remain Untouched Unless a Slice Names It

- The local benchmark branch `feature/extended-agent-interaction-surfaces`.
- The `pi-kernel-uplift-v1` tag.
- Existing user repositories outside disposable tests.
- Existing old Pi state directories.
- Existing unmanaged Git worktrees and refs.
- OpenCode configuration and installation.
- Remote Git branches.
- Production services and credentials.

## 13. Evidence Rules

Every real scenario records:

- Source commit and tree.
- Installed build ID.
- Pi version.
- Package and extension paths and hashes.
- Actual registered tools.
- Role and authority.
- Run manifest.
- Process identity.
- Container ID.
- Image reference and configuration ID.
- Platform, user, and group.
- Mounts and modes.
- Working-copy and Git identity.
- Before and after Git state.
- Before and after bounded host filesystem state.
- Writer lock and generation.
- Controller resource versions.
- Session JSONL identity.
- Project messages and attention.
- User-visible result.
- Host and remote mutations.
- Rollback result.

Evidence files are written outside the project repository.

Static scans, help output, syntax checks, and direct imports are useful narrow tests. They are not installed-system proof.

## 14. Implementation Review Rules

For each slice:

1. Use a dedicated branch or working copy.
2. Record the exact baseline commit.
3. Change only the named slice surfaces unless a dependency is documented.
4. Run focused tests first.
5. Run the cumulative accepted gate.
6. Inspect the actual diff.
7. Use a separate reviewer.
8. Fix findings or record a clear rejection.
9. Do not call the slice complete when a required runner returns STOP or skip.
10. Do not activate real Pi during implementation.

A slice returns to investigation when:

- The current Pi extension API cannot enforce the required boundary.
- The proposed design needs a second lifecycle store.
- A writer needs a writable project Git common directory.
- A test cannot observe the real installed behavior.
- Recovery has more than one plausible continuation.
- A security boundary depends only on prompt text.
- A required operation cannot be made idempotent.
- The implementation would require old-state adoption.
- The implementation would make OpenCode unavailable before release acceptance.

## 15. Definition of Ready to Replace OpenCode

Pi is ready only when:

- The exact installed build completes the full acceptance journey.
- No supported action is represented only by a planned or fake response.
- No full-system gate uses `--help`, syntax checks, or `noLiveAction` as its proof.
- All role tool sets are observed in real processes.
- Host read-only tools cannot escape their project.
- Coding containers cannot access host secrets or unrelated projects.
- Normal shell commands cannot use external network.
- Approved network and host commands are exact, one-use, and auditable.
- Package environments are isolated by working copy.
- Required package security reports are exact and current.
- Headful sessions recover after restart.
- Investigators stop cleanly and become interrupted.
- The project work index accounts for ongoing work and decisions.
- Review is exact and independent.
- Simple fast-forward integration is safe.
- Integration-agent work is safe.
- No implicit push, deployment, or cleanup occurs.
- Fault and permission tests pass.
- Installed rollback preserves all new work.
- Old Pi files and Git state remain unchanged and unadopted.
- OpenCode remains available through the entire development and acceptance period.
- Coding correctness matches OpenCode.
- Performance meets the declared comparison thresholds.
- The user can complete representative real work without falling back to OpenCode.

## 16. Immediate Work Order

Start with these packets in order:

### Packet A — Contract reset

Complete Slice 0 only.

Deliver:

- Greenfield product contracts.
- Greenfield schema design.
- Retired migration and project-mode requirements.
- Updated first-release action manifest.
- Passing contract validator.

Do not change launch behavior.

### Packet B — Real process driver

Complete Slice 1 only.

Deliver:

- Deterministic installed Pi driver.
- Loaded-byte proof.
- Real allowed-tool call.
- Real forbidden-tool proof.
- Evidence output.

Do not claim a coding loop yet.

### Packet C — Fresh database and project index

Complete Slice 2 only.

Deliver:

- Fresh schema.
- Real project registration.
- Durable messages.
- Work index.
- No old-state read path.

### Packet D — One real coding loop

Complete Slice 3 only.

Deliver:

- Exact run preparation.
- Real Docker attestation.
- First-party sandbox launch.
- Read, edit, shell, test.
- One-writer proof.
- Restart with same conversation and working copy.

After Packet D passes, continue with the remaining slices in order.

---

This plan contains enough product and technical direction to begin implementation. Further clarification is needed only when a slice hits one of its stated stop conditions. Those are implementation discoveries, not missing product decisions.
