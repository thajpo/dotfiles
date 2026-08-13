# Pi Harness Container-Safety Plan

## Core Decision
Installed system tests must safely coexist with the live Pi surface.

The documented requirement that “all managed containers are absent at journey completion” means all containers owned by that disposable journey are absent. It cannot mean zero Pi-managed containers across the shared Docker daemon, because healthy live `pi-personal` agents intentionally retain writer containers.

Cleanup must always be based on durable fixture ownership and exact identity. Daemon-wide label sweeps, global kills, and snapshot-difference deletion are prohibited.

## Issues Found

### 1. False Global Invariant
Several journeys query every container with `pi.control.managed=true` and expect zero:

- `tests/system/p6_installed_journey.py:220`
- `tests/system/p7_installed_journey.py:271`
- `tests/system/fixtures/installed-u-resume.py:148`
- `tests/system/fixtures/installed-p10.py:278,289`
- `tests/system/fixtures/installed-repair-journey.py:314`
- `tests/system/fixtures/installed-surface-journey.py:99,230`

These checks cannot distinguish disposable fixture containers from legitimate live Pi containers.

This caused P6 to fail after its own behavior had succeeded. It had already reached `P6_FINAL`, its writer had exited successfully, and its package/network cleanup receipts proved absence. The final daemon-global assertion then found the three live personal containers.

### 2. Dangerous P10 Container Kill
`tests/system/fixtures/installed-p10.py:278-282` currently selects and kills every running Pi-managed container:

```python
managed = docker ps ... label=pi.control.managed=true
for container_id in managed:
    docker kill container_id
```

If P11 had reached P10, it would have deliberately killed all three live personal containers. This is more severe than the false assertions and must be fixed before another aggregate run.

P10 must identify its own writer run from its fixture database, attest that exact container, and kill only that container ID.

### 3. Unsafe Proposed P6 Sweep
The uncommitted P6 `atexit` sweep removes every managed container created after a baseline snapshot.

That is unsafe because a live Pi agent could start during P6. Its container would appear in the set difference and be force-removed despite belonging to another controller state root.

Additional problems:

- It uses `docker rm -f`, bypassing normal graceful cleanup.
- It does not verify complete identity labels.
- It ignores removal failures.
- It can raise from `atexit`.
- It destroys forensic evidence.
- It only protects P6, leaving inconsistent behavior elsewhere.

This change should be removed, not refined.

### 4. Invalid P11 Pre-flight
The uncommitted P11 pre-flight refuses to run whenever any managed container exists.

That would make the release test unusable while the live product is running. It also uses `STOP/77`, which means “required capability unavailable,” not “prior lifecycle failure.”

This change should be removed.

### 5. Live Personal Runs Were Damaged
The three containers removed earlier were definitively the live personal writers:

| Project | Run | Container |
|---|---|---|
| dotfiles | `run_ffba6e517eb37fe8c07baeeeb3b360af` | `1dfde1916473...` |
| mlre-transition | `run_8fb2a4f1d29cd4b0e32ff7a529a4d379` | `dfe0de2dc6bd...` |
| finances | `run_2c48b9046df2899f1e8dba1b5905ef96` | `b7f1fca1339d...` |

Current condition:

- Host supervisors remain alive.
- Pi child processes remain alive.
- Tmux panes remain alive.
- Controller rows still report `running`.
- Writer claims remain held.
- Required writer containers are absent.

The sessions may remain visually responsive, but their writer tools no longer have a valid execution container.

### 6. Reconciliation Visibility Gap
`run.reconcile` currently evaluates only owner and child process identities in `scripts/pi_control/pi_reconcile.py:32-60`.

Consequently, it reports `continue` when:

- The owner process is alive.
- The child process is alive.
- The required writer container is missing.

For a `writer-container` run, process health alone is insufficient. Reconciliation should observe the exact durable container identity as well.

This does not mean automatically killing or restarting a live process. It means accurately reporting `needs_attention` instead of incorrectly reporting `running`.

### 7. Inconsistent Failure Teardown
Some journeys terminate fixture processes in failure paths; others rely on normal completion. Global cleanup assertions were partially compensating for that inconsistency.

The correct model is:

1. Track fixture-owned subprocesses.
2. Signal them gracefully during teardown.
3. Wait for product cleanup.
4. Inspect only fixture-owned container identities.
5. Use exact identity-checked cleanup only if normal cleanup failed.
6. Retain diagnostics before cleanup.
7. Never touch another state root’s containers or tmux server.

## Target Invariants

The implementation should enforce these invariants:

1. A journey can run while unrelated live Pi containers exist.
2. A journey never kills, stops, removes, or fails because of an unrelated container.
3. Every writer container is associated with its fixture run ID and exact expected labels.
4. Every one-shot network/package container is associated with its request ID and kind.
5. Journey success requires all fixture-owned containers to be absent.
6. Journey failure performs bounded, exact, identity-checked teardown.
7. Identity mismatch causes a fail-closed diagnostic, not deletion.
8. P10 kills only the writer container created for its own scenario.
9. Aggregate tests do not require an otherwise idle Docker daemon.
10. Writer reconciliation reports a missing or mismatched durable container.
11. Live recovery never uses direct database edits or daemon-wide Docker commands.
12. Tmux cleanup always remains fixture-scoped.

## Implementation Plan

### 1. Remove Unsafe Changes
Resolve the four uncommitted files as follows:

- Keep the valid tool-order correction in `scripted-provider.ts`.
- Keep the valid tool-order correction in `scripted-repair-secretary-provider.ts`.
- Remove the daemon-wide pre-flight from `run-p11-release.sh`.
- Remove the snapshot-difference `atexit` sweep from `p6_installed_journey.py`.

Do not rerun Docker journeys until the P10 global kill has been corrected.

### 2. Add Fixture-Owned Container Inventory
Create a shared test helper, likely `tests/system/container_hygiene.py`.

It should derive container identities from a fixture’s disposable control database.

Writer identities come from:

- `runs.run_id`
- `runs.container_id`
- `runs.manifest_path`
- `authority == "writer-container"`
- Deterministic name `pi-tool-<run-id>`
- Complete expected labels from the run manifest

Network one-shot identities come from:

- `command_requests.command_request_id`
- Operations executed in `container-network`
- Deterministic name `pi-network-<request-id>`
- Expected labels:
  `pi.control.managed=true`,
  `pi.control.request-id=<id>`,
  `pi.control.kind=one-shot-network`

Package identities come from:

- `package_requests.package_request_id`
- Deterministic name `pi-package-<request-id>`
- Expected labels:
  `pi.control.managed=true`,
  `pi.control.request-id=<id>`,
  `pi.control.kind=one-shot-package`

The helper should expose operations equivalent to:

```python
fixture_container_refs(state_root)
inspect_fixture_containers(state_root)
assert_fixture_containers_absent(state_root)
cleanup_fixture_containers(state_root)
```

Cleanup requirements:

- Inspect by durable ID and deterministic name.
- Validate name and expected `pi.control.*` labels.
- Leave mismatched containers untouched.
- Prefer graceful stop followed by remove.
- Record all commands and observations.
- Raise a clear test failure if absence cannot be proved.
- Never query a global label and treat the result as fixture ownership.

### 3. Add Helper Unit Tests
Add tests for the shared helper before migrating journeys.

Required cases:

- Fixture writer container is found by run identity.
- Fixture package container is found by request identity.
- Fixture network container is found by request identity.
- Unrelated managed container is ignored.
- Container created after fixture start but owned by another state root is ignored.
- Matching ID with mismatched labels is refused.
- Matching name with mismatched durable ID is refused.
- Already-absent container is idempotently accepted.
- Failed stop/remove is reported rather than ignored.
- Diagnostics are captured before removal.

Docker subprocess calls should be mocked for these unit tests.

### 4. Fix P10 First
Update `tests/system/fixtures/installed-p10.py`.

Replace the global kill with:

1. Start the fixture writer.
2. Poll the fixture control database for that conversation’s new run.
3. Require `observed_state == "running"` and a durable `container_id`.
4. Inspect the exact container ID.
5. Verify run ID, project ID, working-copy ID, writer epoch, build ID, and name.
6. Kill only that exact container.
7. Terminate the fixture supervisor as required by the documented scenario.
8. Verify the fixture’s writer container is absent by ID and name.
9. Verify the second writer remains refused until lifecycle recovery completes.
10. Ignore all unrelated managed containers.

Add a regression where another fixture-owned writer remains active while P10 runs. The foreign writer must remain alive and unchanged after P10 kills its own container.

### 5. Replace Global Assertions
Migrate all affected journeys to `assert_fixture_containers_absent(state)`:

- `tests/system/p6_installed_journey.py`
- `tests/system/p7_installed_journey.py`
- `tests/system/fixtures/installed-u-resume.py`
- `tests/system/fixtures/installed-p10.py`
- `tests/system/fixtures/installed-repair-journey.py`
- `tests/system/fixtures/installed-surface-journey.py`

Use `tests/system/writer_docker_journey.py:138-141` as the existing good pattern: it scopes absence checks to the exact run ID and deterministic name.

The global query may remain only as optional diagnostic output. It must not:

- Determine PASS/FAIL.
- Select cleanup targets.
- Select kill targets.
- Trigger STOP/77.

### 6. Standardize Failure Teardown
Use explicit `try/finally`, not `atexit`, around each journey that launches processes or containers.

The teardown order should be:

1. Capture process, run, request, and container diagnostics.
2. Signal fixture-owned supervisors with SIGINT or SIGTERM.
3. Wait a bounded period for normal product cleanup.
4. Kill only the exact fixture-owned supervisor if it does not exit.
5. Re-read fixture state.
6. Run exact identity-checked cleanup for remaining fixture containers.
7. Assert fixture-owned absence.
8. Stop only fixture-scoped tmux sessions or servers.
9. Preserve evidence and relevant logs.

Process registries should retain every fixture-created `Popen`, not only the last supervisor.

A cleanup failure must not silently replace the original assertion. Report both the original failure and teardown failure.

SIGKILL cannot be made recoverable by in-process `finally`; the harness should document that limitation rather than introducing broad next-run deletion.

### 7. Harden Writer Reconciliation
Add read-only writer-container observation to the reconciliation path.

For `authority == "writer-container"`, `reconcile_run` should verify:

- A durable container ID exists for a run recorded as running.
- The exact container exists.
- Its deterministic name matches.
- Its complete controller labels match the run manifest.
- It is in the expected running state.

Outcomes:

| Process | Container | Decision |
|---|---|---|
| Exact owner/child alive | Exact container alive | `continue` |
| Owner/child gone | Container present/absent | existing lost-run recovery path |
| Owner/child alive | Container absent | `needs_attention` |
| Owner/child alive | Container identity mismatch | `needs_attention` |
| Process/container observation uncertain | Any | `needs_attention` |

Important boundary: reconciliation remains observation-only. It must not automatically kill the live owner or remove a mismatched container.

Add tests to `tests/control_plane/test_run_recovery.py`:

- Live owner + exact writer container → continue.
- Live owner + missing writer container → needs attention.
- Live owner + mismatched container → needs attention without removal.
- Docker observation unavailable → fail closed.
- Recovery still refuses while exact owner or child is alive.
- Existing dead-process recovery remains idempotent.

### 8. Recover the Three Live Personal Runs
This is a separate, explicit live operation after the code safety fixes are ready.

No direct `docker rm`, global label sweep, or database update should be used.

Recovery sequence for each run, one at a time:

1. Back up `~/.local/state/pi-system/control.db` and relevant run manifests.
2. Record exact tmux pane, owner PID, child PID, start identities, working-copy claim, Git status, and conversation ID.
3. Revalidate that the owner PID and start identity still match the controller record.
4. Send SIGTERM to the exact supervisor PID so `host_supervisor` follows its exception/finally cleanup path.
5. Wait for the supervisor and child to exit.
6. Verify the old run terminalized and its writer claim was released.
7. If it instead enters `needs_attention`, call `run.reconcile`.
8. Call `run.recover` only after owner and child absence and writer-lock availability are proved.
9. Run `pi-personal --ensure` to relaunch the same durable conversation.
10. Verify a new run ID, exact new container, correct working-copy claim, same session file, and unchanged repository contents.
11. Stop immediately on PID identity mismatch, held lock, uncertain container identity, or unproved cleanup.

Recovery order:

1. dotfiles
2. mlre-transition
3. finances

After each project, verify the other two were untouched before continuing.

### 9. Add Coexistence Acceptance
Add a deterministic system regression that proves a test journey can coexist with an unrelated live writer:

1. Launch a foreign writer through the installed product path in a separate disposable state root.
2. Record its exact run/container identity.
3. Run P6 or a focused fixture journey in another state root.
4. Require the journey to pass.
5. Require the foreign writer’s container and process to remain exact and alive.
6. Gracefully terminate the foreign supervisor.
7. Prove its normal cleanup.

This test is stronger than relying on the developer’s live surface, while the final aggregate should also be run with the repaired live personal agents active.

A separate Docker daemon/context could later add defense in depth, but it is not a substitute for correct ownership scoping. The test harness must remain safe on a shared daemon.

### 10. Run Targeted Verification
Run in this order:

```bash
python3 -m unittest tests.control_plane.test_run_recovery
python3 -m unittest tests.system.test_container_hygiene
bash tests/system/run-docker.sh
bash tests/system/run-p6-installed.sh
bash tests/system/run-p7-installed.sh
bash tests/system/run-u-resume.sh
bash tests/system/run-p10-installed.sh
bash tests/system/run-repair-installed.sh
bash tests/system/run-repair-surface.sh
```

Before and after every Docker journey:

- Record the three live personal run/container IDs.
- Verify their processes remain alive.
- Verify their container identities remain unchanged.
- Verify only the journey’s fixture-owned containers disappear.

### 11. Run Full Acceptance
Create a fresh staged build after all changes.

Run:

```bash
bash tests/system/run-source-gate.sh
bash tests/system/run-contract.sh
python3 -m unittest discover -s tests/control_plane -p 'test_*.py'
node --test tests/secretary-work-extension.test.mjs
bash tests/test_pisec_grid.sh
bash tests/test_personal_grid.sh
bash tests/test_project_workspace_grid.sh
PI_SYSTEM_STAGED_ROOT=/tmp/pi-p11-stage \
OPENCODE_BIN=/home/j/.opencode/bin/opencode \
bash tests/system/run-p11-release.sh
```

P11 must run while the three repaired live personal writers remain active.

Final evidence must establish:

- All P11 actions passed.
- Every fixture-owned container is absent.
- All pre-existing live containers remain present and exact.
- Live run IDs, claims, and process identities were not changed by the aggregate.
- No unrelated tmux session was touched.
- No global Docker kill/remove command was used.
- The known pre-existing runtime-spec failure remains separately identified.

## Commit Structure
Use modular commits:

1. Keep provider fixture ordering and remove unsafe cleanup/pre-flight.
2. Add fixture-owned container inventory and tests.
3. Scope P10 kill behavior.
4. Replace daemon-global assertions.
5. Standardize failure teardown.
6. Add writer-container reconciliation observation.
7. Add shared-daemon coexistence regression.
8. Apply any documentation correction clarifying “fixture-owned containers absent.”
9. Perform live recovery separately; do not mix live state mutation into a code commit.
10. Commit final verification-only fixes if necessary.

## Changed Surfaces

| Surface | Change |
|---|---|
| Public API | No new operation required |
| Schema | None |
| Persistence | No migration |
| Authority | Cleanup restricted to exact fixture ownership |
| Container lifecycle | Fixture-specific observation and teardown |
| Reconciliation | Writer health includes durable container identity |
| Error semantics | Foreign containers ignored; uncertain fixture identity fails closed |
| Dependencies | None |
| Deployment | Fresh stage only after fixes |
| Operations | Three live personal runs require exact graceful recovery |
| Rollback | Code commits are independently revertible; live recovery preserves old run records |

## Completion Criteria
This work is complete only when:

- The unsafe P6 sweep is gone.
- The invalid P11 pre-flight is gone.
- P10 cannot kill foreign containers.
- No installed journey uses daemon-global managed-container emptiness as acceptance.
- Every journey proves absence only for its own identities.
- Failure teardown is exact and deterministic.
- Reconciliation detects a missing writer container.
- The three personal conversations are healthy on new exact runs.
- The full P11 aggregate passes while those live personal writers remain active.
- Final Docker and tmux inventories prove no unrelated state was touched.

No live recovery or additional Docker mutation should occur until the safety changes are implemented and the exact recovery step is explicitly approved.
