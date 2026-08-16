# Pi Harness Current / Legacy Reachability Inventory

Purpose: mechanical classification of every Pi-related repository surface as
current (release-reachable) or removed legacy. Classification is based on the
import graph from the release entrypoints plus installed-entrypoint checks,
not on age or naming.

Method: a static import-graph traversal seeded by the release entrypoints
(`RELEASE_FILES` in `scripts/pi_control/pi_install.py`, the launcher Python
entry scripts, and `scripts/pi_control/__init__.py`) determines which
`scripts/pi_control` modules are reachable. Anything unreachable from the
release graph is legacy and is removed in Slice 9.

## 1. Current controller family (`scripts/pi_control/`)

Release-reachable from the canary launchers and the staged build (51 modules):

- `pi_cli.py`, `pi_client.py`, `pi_protocol.py`, `pi_store.py`, `pi_schema.py`
- `pi_install.py`, `installed_builds.py`, `staged_build.py`, `activation_cli.py`,
  `activation_approval.py`, `authorization_cli.py`
- `host_supervisor.py`, `controller_channel.py`, `launch.py`, `run_manifest.py`
- `docker_runtime.py`, `scoped_read.py`, `pi_reconcile.py`, `reconcile.py`
- `projects.py`, `conversations.py`, `messages.py`, `command_requests.py`,
  `pi_workstreams.py`, `pi_review.py`, `reviews.py`, `changes.py`,
  `integration.py`, `dependencies.py`, `package_diff.py`,
  `package_environment.py`, `investigators.py`, `subagents.py`,
  `presentation.py`, `role_profiles.py`, `projects.py`
- Shared support: `models.py`, `errors.py`, `events.py`, `operations.py`,
  `locks.py`, `git_adapter.py`, `project_policy.py`, `process_adapter.py`,
  `writer_lock.py`

The exact RELEASE_FILES list in `scripts/pi_control/pi_install.py` is the
mechanical authority for what ships in a generation.

## 2. Removed legacy controller family

Removed in Slice 9 (43 modules, unreachable from the release graph and unused
by the current family):

- `cli.py`, `client.py`, `store.py`, `schema.py`, `workstreams.py`
- `migration.py`, `migration_importer.py`, `migration_planner.py`,
  `migration_reconcile.py`, `migration_adapters/`, `migrations/`,
  `legacy_inventory.py`
- `presentation_adapter.py`, `runtime_adapter.py`, `session_adapter.py`,
  `publication.py`, `leases.py`, `activation.py`, `error_messages.py`
- `artifacts.py`, `child_runs.py`, `cleanup.py`, `snapshot.py`

Their tests were removed with them (44 `tests/control_plane/` suites plus the
legacy `.mjs` suites). The behavior they covered is retained in the
current-family unit tests and the installed-tier journeys (HA-001..HA-018).

## 3. Launchers

Current (release canary or daily surface):

- Release canary: `bin/pi-control`, `bin/pi-authorize`, `bin/pi-install`,
  `bin/pi-system-run`, `bin/pi-system-secretary`, `bin/pi-system-investigator`,
  `bin/pi-system-reviewer`, `bin/pi-system-container-run`,
  `bin/pi-system-workstream-run`, `bin/pi-workstream`, `bin/pi-integration`,
  `bin/pi-activate`
- Daily surface (packaged): `bin/pisec`, `bin/pi-personal`, `bin/pi-start`,
  `bin/pi-restart`, `bin/pidev`, `bin/pi-help-custom`, `scripts/pi-surface.py`
- Retained operational: `bin/pi-harness-feedback` +
  `scripts/pi-harness-feedback.py`

Removed in Slice 9: `bin/pi`, `bin/pi-tmux-session`, `bin/pi-host`,
`bin/pi-root-session`, `bin/pi-personal-herdr`; installed-only legacy
launchers (`pi-secretary*`, `pi-herdr-workstream`, `pi-review-agent`,
`pi-daily`, `pi-pr`, `pi-research`, `pi-webdev`, `pi-bench-*`,
`pi-extensions-update`, `pi-worktree-nvim`) are retired from `~/.local/bin`.

## 4. Removed legacy scripts and installer

- `scripts/pi-workspace.py`, `scripts/pi-runtime.py`, `scripts/pi-root-session.py`,
  `scripts/pi-sandbox-gc.py`, `scripts/pi-personal-herdr.py`
- `scripts/pi-patch-core`, `scripts/pi-patch-subagents`
- `install.sh` (legacy installer; `bin/pi-install` is canonical)
- `scripts/pi-surface-chat.sh` (dead one-shot workaround)

## 5. Extensions

Packaged in the resource catalog (current):

- `controller-channel`, `scoped-project-read`, `project-messages`,
  `project-commands`, `dependency-review`
- `secretary-work`, `change-flow`, `observability`
- First-party packages: `pi/packages/pi-sandbox-control`,
  `pi/packages/pi-subagents-control`

Removed in Slice 9: `secretary/`, `secretary-investigator-git/`, `subagent/`,
`workstream-brief/`, `workstream-channel/`, `root-session/`, `continuity/`,
`workflow-state/`, `auto-continue/`, `review-receipt/`, `clean-headings.ts`,
`control-plane/` (the model-visible CLI-exec bridge superseded by the
channel extensions), `pi/agents/`, `pi/prompts/`, `pi/themes/`,
`pi/core/pinned-conversation-viewport.mjs`.

Retained: `pi/settings.json` (consumed by the package provenance tests),
`pi/npm/` (the first-party npm tree and locks).

## 6. Tests

Current: `tests/control_plane/` current-family suites, `tests/system/`
(contract/source/staged/installed runners and catalogs), `tests/test_pi_core.py`,
`tests/test_pi_install.py`, `tests/pi_test_build.py`, `tests/test_pi_harness_feedback.py`,
the first-party package `.mjs` tests, `tests/pi-manifest-bridge.test.mjs`,
`tests/secretary-work-extension.test.mjs`, `tests/change-flow-extension.test.mjs`,
`tests/observability-extension.test.mjs`, `tests/test_pisec_grid.sh`,
`tests/test_personal_grid.sh`.

Removed in Slice 9: the legacy `test_pi_*` suites, the legacy
`tests/control_plane/` suites listed in section 2, legacy `.mjs` suites, the
legacy shell runners (`pi-docker-integration.sh`, `pi-docker-runtime-cache.sh`,
`pi-installer-transaction.sh`, `run-candidate-tests.sh`), and the historical
planning records (`ANCHORED_SUMMARY.md`, `PLAN_P10_P12.md`; preserved in Git
history).

## 7. Installed state

Current: `~/.local/share/pi-system/` (active generation until re-activation),
`~/.local/state/pi-system/` (controller state until the deliberate fresh
reset), `~/.local/share/pi-system-work/` (controller-created working copies),
`~/.pi/agent/auth.json` (provider credential source; preserved).

Removable (Slice 9/10, after re-activation): `~/.local/share/pi/` (legacy
core, control, worktrees, rollback generations), `~/.pi/agent/` npm tree and
old sessions (except auth.json), `~/.local/bin/*.rollback.*` and installed
legacy launchers, `~/.config/pi`.

## 8. Historical records

`PI_HARNESS_DEEP_DIVE.md` and `PI_HARNESS_RECOVERY_PLAN.md` are the program
records and are intentionally kept.
