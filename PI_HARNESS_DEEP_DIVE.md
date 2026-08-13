# Pi Harness — Deep Dive Findings (2026-08-11)

Purpose: complete inventory of the current state, what is legacy, what is
broken, and a removal plan. No migration of old chats/state — fresh start.

## 1. The product

The Pi harness = a controller (`pi-control`) that owns durable conversations
(secretary, personal, workstream, investigator, reviewer, integration) and
runs. Daily surface: interactive Pi TUI in tmux panes (`pisec` for
secretaries, `pi-personal` for personal/workstream writers), bound to the
controller via an authenticated channel. The controller gates every tool call,
sandboxes runs (private HOME/agent dir, digest-pinned manifests, writer
containers), and owns the run ledger.

## 2. Immediate breakage (why pi-restart fails now)

1. A repo-wide rename `greenfield_*` → `pi_*` is **in flight, uncommitted**.
   It renamed 9 modules in `scripts/pi_control/`, the resource catalog, and
   touched imports/tests. The control-plane suite currently has import errors
   in several test modules (stale imports not yet updated in all files).
2. The **activated** install at `~/.local/share/pi-system/bin/pi-control`
   still references `greenfield_cli` (built pre-rename). The surface helper
   (`pi-surface.py env`) resolves the controller to the activated root when
   the surface stage is stale, so every surface command hits the stale binary:
   `pi-surface: /home/j/.local/share/pi-system/bin/pi-control failed`.
3. The surface-stage marker is stale (repo moved), so the next
   `ensure-surface-stage` will rebuild — but the rebuild output (and the
   rename) must land first.
4. `tmux` does not auto-attach after `pi-restart`; only `pisec`/dotfiles
   windows appeared in the last run; no other sessions opened.

## 3. What is legacy (to remove)

### 3.1 Repo — legacy launchers still tracked in `bin/`
- `bin/pi` (legacy root launcher; EXCLUDED from greenfield manifest)
- `bin/pi-tmux-session`, `bin/pi-host` (legacy session/host wrappers)
- `bin/pi-secretary`, `bin/pisec`, `bin/pi-secretary-herdr`,
  `bin/pi-secretary-stats` (legacy secretary surface)
- `bin/pi-herdr-workstream`, `bin/pi-review-agent` (legacy worker launchers)
- `bin/pi-root-session`, `bin/pi-personal-herdr` (legacy helpers)
- NOTE: `bin/pisec`, `bin/pi-start`, `bin/pi-restart`, `bin/pi-personal`,
  `bin/pidev` were REWRITTEN greenfield-backed, but `bin/pi`,
  `bin/pi-tmux-session`, `bin/pi-host`, `bin/pi-secretary*`,
  `bin/pi-herdr-workstream`, `bin/pi-review-agent`, `bin/pi-root-session`,
  `bin/pi-personal-herdr` are still the legacy implementations.

### 3.2 Repo — legacy scripts in `scripts/`
- `pi-workspace.py`, `pi-runtime.py`, `pi-root-session.py`,
  `pi-sandbox-gc.py`, `pi-personal-herdr.py`, `pi-harness-feedback.py`
  (legacy control helpers; greenfield RELEASE_FILES only includes
  `pi-system-run.py`, `pi-system-container-run.py`,
  `pi-system-workstream-run.py`, and `pi_control/`)
- `pi-patch-subagents` (legacy patch script)

### 3.3 Repo — legacy extensions in `pi/extensions/`
Deleted earlier: `secretary-subagents`, `observability`.
Still present but legacy-only (not in greenfield role resource sets):
- `secretary/` (has legacy `secretary_*` tools), `secretary-investigator-git/`
- `subagent/`, `workstream-brief/`, `workstream-channel/` (legacy secretary
  workflow)
- `root-session/`, `continuity/`, `workflow-state/`, `auto-continue/`
  (legacy session glue)
- `clean-headings.ts` (legacy)
- Still greenfield-relevant: `controller-channel`, `scoped-project-read`,
  `project-messages`, `project-commands`, `dependency-review`,
  `review-receipt`, `control-plane`, `fast-mode`, `host-command`,
  `harness-feedback`, `pi-sandbox.json`

### 3.4 Repo — legacy tests (broken or testing deleted surface)
- `tests/test_pi_secretary_*` (7 files) — deleted launcher/control surface
- `tests/test_pi_harness_static.py`, `test_pi_launchers.py`,
  `test_pi_mobile.py`, `test_pi_personal.py`, `test_pi_personal_herdr.py`,
  `test_pi_restart.py`, `test_pi_root_sessions.py`, `test_pi_help.py`,
  `test_pi_acceptance.py`, `test_pi_acceptance_regressions.py`,
  `test_pi_compaction.py`, `test_pi_sandbox_gc.py`,
  `test_pi_runtime_contract.py`, `test_pi_image_attachment.py`,
  `test_pi_pinned_viewport.py`, `test_pi_harness_feedback.py`
- `tests/test_greenfield_core.py`, `tests/test_greenfield_install.py`,
  `tests/greenfield_test_build.py`, `tests/greenfield-manifest-bridge.test.mjs`,
  `tests/system/test_greenfield_docs.py` — need rename + content pass

### 3.5 Repo — legacy docs
- `PI_GREENFIELD_IMPLEMENTATION_PLAN.md` → rename to `PI_IMPLEMENTATION_PLAN.md`
- `pi/control-plane/GREENFIELD_CUTOVER_AND_ROLLBACK.md` → rename
- All docs still say "greenfield" (~201 mentions across docs, tests,
  packages). "greenfield" should become "Pi harness" / "Pi" / "controller".

### 3.6 Installed on disk (removable)
- `~/.local/share/pi/` (~5 GB): legacy core, control, worktrees (1.4 GB),
  dozens of `.rollback.*` generations (178 MB each)
- `~/.pi/agent/` (~4.9 GB): legacy agent dir — npm tree, sessions (627 MB of
  old chats), extensions, settings, auth
- `~/.local/bin/`: 1310 `.rollback.*` files; legacy launchers
  (`pi`, `pi-tmux-session`, `pi-host`, `pi-secretary*`, `pisec`,
  `pi-herdr-workstream`, `pi-review-agent`, `pi-root-session`,
  `pi-personal-herdr`, `pi-secretary-stats`)
- `~/.local/share/pi-system-work/` (~13 MB): controller-created worktrees
- `~/.local/state/pi-system/` (25 MB): controller state — 2 projects,
  7 conversations, 28 runs, 1 registered build (all disposable per user)
- tmux.conf legacy references: `prefix g` pisec switch, resurrect-processes
  matching `pi-tmux-session`/`pi-host`/`pi`, post-restore hook calling
  `pisec launch`

## 4. What is current (keep)
- `bin/pi-system-*` launchers, `bin/pi-control`, `bin/pi-install`,
  `bin/pi-activate`, `bin/pi-authorize`, `bin/pi-workstream`,
  `bin/pi-integration`
- Rewritten surface: `bin/pisec`, `bin/pi-personal`, `bin/pi-start`,
  `bin/pi-restart`, `bin/pidev` (greenfield-backed, interactive TUI)
- `scripts/pi-surface.py` (stage/register/launch resolver), the 3
  `pi-system-*.py` launchers, `scripts/pi_control/` (the controller package)
- Greenfield-relevant extensions (see 3.3)
- `tests/control_plane/` (the greenfield test suite — currently has import
  errors from the in-flight rename)
- `tests/system/` journeys + release pipeline
- The activated `~/.local/share/pi-system/` (356 MB) — but it must be
  re-built/re-activated after the rename lands (its bin/pi-control is stale)
- `machines/linux-x86_64.env` — machine config (dotfiles dirs, trusted roots)

## 5. Broken now (to fix after the audit)
- In-flight rename incomplete → finish it, fix test imports, re-verify
- Activated install stale (greenfield_cli ref) → re-stage + re-activate
- surface-stage marker stale → rebuild once
- `pi-restart` doesn't auto-attach; only pisec/dotfiles window appeared
- tmux.conf resurrect/legacy refs must be updated after launcher removal
- No revision re-scope for conversations (product gap; low priority given
  fresh-start decision)

## 6. Removal plan (draft, for audit)

Phase 0 — finish the rename:
- Complete `greenfield_*` → `pi_*` renames, fix remaining test imports,
  run control-plane suite green.

Phase 1 — delete legacy from the repo:
- Remove legacy bin/ launchers, scripts/, extensions/, tests/, docs renames.
- Update tmux.conf (resurrect patterns, pisec keybind), README, machines if
  needed.
- Keep only: greenfield launchers, surface wrappers, controller package,
  greenfield extensions, control-plane + system tests.

Phase 2 — wipe installed legacy + old state:
- Delete `~/.local/share/pi`, `~/.pi/agent`, `~/.local/bin/*.rollback.*`,
  legacy launchers in `~/.local/bin`, `~/.local/share/pi-system-work`,
  `~/.local/state/pi-system` (old chats/conversations — user does not want
  migration).
- Keep `~/.local/share/pi-system` (active build) until re-activated.

Phase 3 — fresh install + verify:
- `install.sh` (or the greenfield flow) fresh: stage → verify → activate →
  init-state (empty controller), register projects (dotfiles, mlre, etc.).
- `pi-restart` from inside tmux: caller survives, grid spawns interactive
  TUI panes for all registered projects, auto-attach works.
- Run control-plane suite + release pipeline on the cleaned tree.
- Confirm no `greenfield` mentions remain (git grep = 0).

## 7. Verification gates
- `git grep -i greenfield` → 0
- `python3 -m unittest discover tests/control_plane -t .` → green (except
  known pre-existing test_runtime_spec)
- `pi-restart` from a tmux pane: caller survives, grid up, TUIs interactive
- release pipeline (P11) passes on the renamed tree
