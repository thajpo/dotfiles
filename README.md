# dotfiles

## Oh My Pi

```bash
curl -fsSL https://omp.sh/install | sh
omp
pi  # dotfiles compatibility launcher; delegates to omp
```

The retired controller-driven Pi implementation is preserved under
[`archive/custom-pi/`](archive/custom-pi/README.md) for historical reference.

## Shared CLI Skills + Dotfiles Auto-Sync

This repo keeps one canonical shared instruction file
(`~/dotfiles/agent/AGENTS.md`) and one canonical Agent Skills directory
(`~/dotfiles/skills`). The installer wires automatic instructions into OMP and
Codex. OpenCode remains available only as a shared skills adapter through
`~/.skills`; it is not the default Pisec harness:

- `git pull --rebase --autostash origin master`
- commit/push only when there are local changes

Those two bullets describe the separately owned shared-dotfiles synchronizer,
not Pisec-managed worker repositories. Pisec workers never push.

### Linux (systemd user timer)

Symlink setup:

```bash
mkdir -p ~/dotfiles/skills
ln -sfn ~/dotfiles/agent/AGENTS.md ~/.omp/agent/AGENTS.md
ln -sfn ~/dotfiles/agent/AGENTS.md ~/.codex/AGENTS.md
ln -sfn ~/dotfiles/skills ~/.skills
ln -sfn ~/.skills ~/.omp/agent/skills
ln -sfn ~/.skills ~/.codex/skills
ln -sfn ~/.skills ~/.config/opencode/skills
ln -sfn ~/dotfiles ~/.dotfiles
```

Optional drift repair (recommended):

```bash
~/dotfiles/scripts/skills-doctor.sh
```

The checked-in sync script pulls with rebase/autostash, repairs shared skill
links, commits only local changes, and pushes `origin/master`.

Install the timer with:

```bash
~/dotfiles/scripts/dotfiles-sync-install.sh
```

The installer writes `dotfiles-sync.service` and `dotfiles-sync.timer` under
`~/.config/systemd/user`, reloads the user manager, and enables the timer.
Re-running it is idempotent.

### macOS (launchd)

Use the same shared-link setup as Linux, or run the idempotent skills-only
installer:

```bash
~/dotfiles/scripts/agent-workflow-install.sh --skills-only
```

Install the two-hour launchd sync agent with:

```bash
~/dotfiles/scripts/dotfiles-sync-install.sh
```

The installer writes
`~/Library/LaunchAgents/com.user.dotfiles-sync.plist`, replaces an older
loaded copy safely, and uses the checked-in sync script. Re-running it is
idempotent. Inspect or unload it with:

```bash
launchctl print "gui/$(id -u)/com.user.dotfiles-sync"
launchctl bootout "gui/$(id -u)/com.user.dotfiles-sync"
```

The checked-in sync script uses a portable `mkdir` lock, so the same approach
works on Linux and macOS.

## Agent Engineering Workflow

OMP and Codex are the current Pisec worker harnesses when configured. OpenCode
is not a Pisec default; its shared skills link remains available for unrelated
workflow use.

Install or repair the shared memory/config symlinks:

```bash
~/dotfiles/scripts/agent-workflow-install.sh
```

Inspect the setup:

```bash
~/dotfiles/scripts/agent-workflow-doctor.sh
```
On macOS, the shared OMP/skills workflow and dotfiles synchronization are
supported. Full fenced Pisec is Linux-only. The full-install path exits before
modifying the macOS home directory; use `--skills-only` for shared workflow
links and the separately owned dotfiles sync installer for synchronization.



### Pisec workflow broker

Pisec product v1 is the durable, product-neutral workflow and security core. It owns
projects, proposals, approvals, workstream intent, decisions, runtime
bindings, research packets, and audit events. The selected `HarnessAdapter`
and `WorkspaceAdapter` own product wire protocols and artifacts. The tested
worker harnesses are OMP 17.3.4 (`omp`) and Codex 0.147.0 when configured;
Herdr 0.8.0 protocol 19 (`herdr`) is the workspace adapter. Fence 0.1.66,
Collie 0.28.0, the committed Collie unread-activity patch, and Reviewr 0.32.1
are exact v1 pins. Collie remains presentation glue and Reviewr is review-only.

The labels below are intentionally different: Pisec product v1, configuration
format version 3, and control database `pisec-core-v1` version 1.

```json
{
  "schemaVersion": 3,
  "fencePath": "/absolute/path/to/fence",
  "harness": {"id": "omp", "config": {}},
  "workspace": {"id": "herdr", "config": {}}
}
```

The concrete adapter config fills the selected harness executable/gateway/model
values and the dedicated Herdr session/socket. The installer validates the
selected adapters and exact public interfaces before mutating user state. There
is no in-place predecessor migration. Unsupported old state is opaque input for
the explicit archive/reset path, which archives the complete owner-only state
root and initializes the exact v1 database.

For an explicit archive-and-reset deployment:

```bash
~/dotfiles/scripts/agent-workflow-install.sh \
  --collie-host myhost.my-tailnet.ts.net \
  --collie-trusted-user you@example.com \
  --reset-pisec-state
```

Review the printed archive path after deployment. Pisec does not patch OMP,
Herdr, or Fence source or binaries. The deployment applies one reviewed,
fail-closed Collie 0.28.0 source patch at the presentation boundary; it derives
`done` from Collie's existing shared unread ledger without changing Herdr or
Pisec lifecycle authority.

The full deployment also links this repository's LazyVim configuration at
`~/.config/nvim`, installs the `chmarax.herdr-nvim` and
`persiyanov.reviewr` Herdr plugins when absent, and sets Herdr's prefix to
`Ctrl-a`. Managed plugin bindings are `Ctrl-a Shift-e` (toggle Neovim),
`Ctrl-a Shift-f` (pick a file), and `Ctrl-a Shift-v` (toggle reviewr);
unrelated Herdr key configuration is preserved.

Every worker receives one independent local Git repository with its own
`HEAD`, refs, index, configuration, and object store; one namespaced branch;
one private harness home containing materialized runtime snapshots and private
sessions; one rendered Fence policy; one immutable broker-owned task packet;
and one adapter-owned workspace surface. Worker commits use the fixed,
non-secret identity `Pisec Worker <pisec-worker@invalid>` and workers never
push. Project permissions are complete project-wide replacements: approved
existing paths are read-only and approved domains are added to each exact
worker binding. Fence and the broker retain all role and write boundaries.

Tool lists and approval prompts are workflow/UX controls, not security
boundaries. Fence remains the hard process, filesystem, and network boundary;
installed plugins and project MCP run inside the same role Fence.

The OMP adapter keeps these execution profiles:

| Profile | Network scope | Intended role |
|---|---|---|
| `secretary-project` | Loopback model gateway and approved public web access | Project secretary |
| `worker-default` | Loopback model gateway, web search, and current project permissions | Isolated worker |

Permission changes use an exact prepare/apply approval and targeted runtime
refresh. Tool and skill improvements are committed source changes distributed
with `pisec update`.

Pisec uses one exact human authorization for delegation and one exact human
authorization for candidate acceptance, then lets the project Secretary own
post-acceptance local integration. A Reviewr PR tab or comment is never Pisec
lifecycle or authorization. The closeout sequence is:

1. The project Secretary prepares one bounded worker delegation. The user
   authorizes that exact scope; the worker commits reviewable changes in its
   independent repository and reports the result through typed records.
2. The Secretary prepares a bounded workstream acceptance showing the
   immutable task and completion packet digests, candidate patch digest,
   changed paths, checks, conflict policy, effects, and non-effects. The user
   accepts that candidate once in the OMP UI. Target and final commit OIDs are
   refreshed integration state, not a second approval input.
3. After acceptance, the Secretary refreshes the target, asks the original
   worker to reconcile ordinary target drift within the accepted paths, reruns
   bounded verification, imports the exact candidate, and applies only a
   `git merge --ff-only`. A successful integration records immutable acceptance
   and verification provenance; it never pushes or changes unrelated paths.
   Authenticated branch publication is a separate explicit host-side operation.
4. The Secretary then completes, retires, and cleans up the worker. Completion
   records desired state; retirement closes the worker task tab and fenced
   runtime; cleanup removes only an unchanged integrated retired worker
   repository. Unintegrated, dirty, untracked, or otherwise retained worker
   repositories stay in place. Material ambiguity, scope expansion, failed
   checks requiring judgment, dirty targets, and new capabilities remain
   user-visible stops.

The installed full bundle is updated with `pisec update`. The stable updater
archives one committed source bundle, switches one `current` deployment
atomically, and refreshes runtimes by generation. Busy workers converge at an
idle boundary; a refresh failure becomes `needs_attention` and ordinary refresh
does not promise automatic runtime restoration. The updater retains one manual
last-known-good recovery bundle but never automatically rolls back. Unsupported
database state requires the explicit archive/reset path. Chat files remain
below each binding's private session home; they are not stored in `control.db`.

The OMP harness is launched through the private `pisec/runtime-bin/omp`
shim. Herdr workspace cold restore therefore re-enters Fence before OMP,
rather than replaying an unfenced surface command. Each managed session
generates its role configuration from explicit safe inputs; it does not copy
the host `~/.omp/agent/config.yml`. Pisec then loads the overlay for gateway,
workspace, and search wiring. Pisec launches
OMP in `yolo` tool-approval mode because Fence owns command, filesystem, and
network enforcement; exact semantic Pisec approvals (such as workstream
creation and workstream acceptance) remain separate extension-level checks.
Pisec-owned OMP panes have exactly one lifecycle reporter. Each isolated OMP
home excludes `herdr-omp-agent-state.ts`; the private launcher explicitly loads
`omp/extensions/pisec.ts`, which publishes the per-runtime
`pisec:omp:<hash>` source. Pisec keeps one current mutable runtime surface per
harness; each binding home contains the private materialized runtime snapshot
and private sessions needed by that binding.

Runtime-affecting inputs have a content-addressed generation. The digest covers
the Pisec extension and private launcher, OMP/Fence executables and policy
templates, generated configuration inputs, copied extensions, skills, rules,
commands, themes, agents and `AGENTS.md`, and the isolated plugin snapshot.
Each binding records desired, launch-reserved, and applied generations. A new
authenticated `session_start` commits only the generation reserved for that
launch.

`pisec project refresh --all` rolls stale active bindings one at a time. It
reserves the runtime against new turns, waits for `idle`, exits OMP gracefully
without closing the pane, regenerates managed artifacts, preserves the native
session and project/workstream/repository/branch identities, resumes the same
session, and verifies fresh runtime attestation for the deployed generation.
Busy bindings remain pending; a failed refresh is explicit `needs_attention`.
Repeating the command at the current generation performs no restart, and an
ordinary refresh never promises automatic runtime restoration. The normal
installer runs this refresh after service startup and before the final doctor.
The secretary is trusted inside exactly one registered project and receives
the standard OMP read/write/edit/bash/task/hub and web-search surface, installed
plugins, project MCP, copied user extensions/skills/rules/commands/themes/agents,
normal local Git, and broad public web access. Fence denies sibling projects,
host secrets, metadata IP, and the real OMP/Herdr state. Raw push and publish
commands remain denied; authenticated fast-forward publication of an existing
non-default branch is brokered by `pisec_push_branch`, so credentials never
enter Fence. Command policy also denies SSH, privilege escalation, and
container engines. Model calls go through the loopback OMP auth gateway; raw
provider credentials are not copied into worker sandboxes.

Worker research is a durable, authenticated packet flow rather than a live
cross-pane chat. A worker submits a bounded idempotent request without
blocking; the broker derives its project/workstream from the runtime binding.
Secretary attention wakes carry only the project and attention revision, so
concurrent requests coalesce without exposing packet bodies. Wake delivery is
at-least-once; a repeated wake is safe because request claims and packets are
idempotent. The secretary claims requests and launches the fixed
`pisec-web-research` `@smol` child in one task batch; that child is read-only
and limited to `read` and `web_search`. Answers use the exact bounded citation
schema, persist as immutable SQLite packets, replay after restart, and are
acknowledged by the worker after consumption. Workspace downtime affects
prompt immediacy only; request and answer truth remains in Pisec.

Secretaries can durably escalate recurring access, permission, lifecycle, or
tooling failures without widening authority:
`pisec_report_secretary_issue` records a bounded category, severity, exact
failure details, requested minimum action, and evidence under the authenticated
project secretary. The First Mate can inspect fleet issue records through
`pisec_fleet_list_issues` and `pisec_fleet_inspect_issue`. Reports are
idempotent and read-only for the First Mate; they never auto-grant paths,
change Fence policy, or approve worker creation.

Every active project has one project Secretary. `project` mode ends automatic
supervision at that Secretary; `fleet` mode permits escalation to the one
active First Mate. Typed durable records remain authoritative. The deterministic
attention watcher only indexes those records and schedules the recipient; it
does not classify prose or replace either model supervisor. Worker help and
issues reach the project Secretary first, and only a Secretary-owned fleet
escalation reaches the First Mate.

Default Pisec output is semantic: it explains status, needs attention, and the
next action. `--json` retains exact machine identifiers and raw desired,
provisioning, and observed fields. Runtime `working`, `blocked`, and `idle` are
activity states; Herdr/Collie `done` is presentation-only. Reviewr
`Resting`/`Working`/`Neither` is review presentation only. Pisec
`ready_review`, `accepted`, `completed`, and `retired` mean candidate ready for
review, one exact human approval, verified `ff-only` integration, and guarded
terminal task closure respectively. Herdr supplies runtime activity and
workspace identity; Pisec owns semantic task lifecycle; Collie `done` means
unread presentation; Reviewr is review-only.

Provider and auth-broker credentials stay outside worker homes. The shared
loopback inference-gateway client token is intentionally role-readable and is
not per-binding isolation; each binding separately has one Pisec control token.
Explicitly approved data or Python paths may contain user data or credentials,
so they are readable-data exceptions rather than an injection guarantee.

Reviewr 0.32.1 opens the independent worker repository through inert base refs.
Its PR tab and comments do not become Pisec lifecycle or authorization. Every
delegation and candidate acceptance has its one exact human authorization;
Secretary-owned post-acceptance integration adds no third merge approval, and
v1 has no project setting that automates either decision.

An idle, exited, or missing harness process never implies completion. Pisec
records runtime state separately and requires an explicit completion decision.
Unexpected cleanup failures persist `needs_attention` on both the operation
and workstream instead of silently losing the recovery signal.


Host commands:

```bash
pisec project register --path ~/src/project
pisec project list
pisec project open ~/src/project
pisec project refresh --all
pisec status
pisec status --project ~/src/project
pisec board
pisec reconcile
pisec doctor
pisec workstream cleanup ws_<32-lowercase-hex> --confirm ws_<32-lowercase-hex>
```

Running `pisec` without a command prints the command guide. Successful commands
use concise human-readable output; add `--json` to any command when a script
needs the complete response, for example `pisec doctor --json > doctor.json`.
The bounded recovery commands are `pisec update --recover-previous`,
`python3 scripts/pisec-update.py --install-updater-only --repo <repo> --ref
<commit>`, and the explicit schema-boundary
`python3 scripts/pisec-update.py --archive-reset-state --repo <repo> --ref
<commit>`. The latter archives opaque prior state and creates fresh v1 state;
it is not a migration.

Final v1 acceptance evidence is written outside the repository only after the
committed source gate and scenario matrix pass:
`${XDG_STATE_HOME:-$HOME/.local/state}/pisec/release-evidence/<finalV1Commit>/acceptance.json`.
The owner-only JSON records the two source commits and trees, bundle/schema
identities, exact pins, commands and exit codes, actual counts, scenario IDs,
sanitized result digests, deployment/current/last-known-good identities, the
archive-manifest digest, timestamps, and an overall pass value. The README does
not carry a mutable current test count; the external record is the current
acceptance run.
The old bare `pisec` secretary-grid launcher belonged to the retired
controller-driven Pi implementation under `archive/custom-pi/`; the active
Pisec command is the host administration CLI shown above.

#### Historical acceptance evidence — tested commit `c816af1c`

The 2026-08-16 acceptance run recorded on commit `c816af1c` is historical
evidence, not current v1 proof. It exercised the Herdr/OMP surfaces, broker
restart and reconciliation checkpoints, SQLite state, Git operations, Fence
boundaries, durable research, and the repository checks available at that
commit. Its implementation and results predate the current v1 contract, so
they must not be used as evidence for the final source or live deployment.
Phase 10 writes the current acceptance record outside the repository after the
final committed run; no mutable current result is kept in this README.


Collie is pinned to the Herdr `main` session and is a mobile presentation of
Pisec project rooms, coordinator chats, and active worker tabs. It has no
project registry or lifecycle authority. Pisec owns semantic task lifecycle;
Herdr supplies runtime activity and workspace identity. The pinned downstream
patch in `patches/collie-v0.28-unread-idle.patch` projects a resting agent as
`done` only while Collie's persisted `lastActiveAt > lastSeenAt`, then returns
it to `idle` after a Collie read. This presentation state never changes a
Pisec task or runtime row.
The installer applies the patch idempotently to the managed Collie checkout
and fails closed if a future 0.28.0 source no longer matches.

The installer configures loopback binding, HTTPS Tailscale Serve,
single-session discovery, the main-session transcript root,
trusted-user/Host/Origin checks, and refuses Funnel. Limit the MagicDNS
service with a Tailscale ACL; Pisec cannot verify a remote device's ACL
membership. Run `pisec doctor` after upgrades and after changing Collie,
Herdr, Fence, or OMP versions. Runtime-only changes use
`pisec project refresh --all`; broker Python changes restart the broker,
Collie bridge changes restart Collie, and Herdr configuration or plugin changes
reload or restart Herdr.

Project OMP is always launched through a Pisec binding and its private Fence.
The ordinary-shell `omp` command is an intentional blocker. Use
`pisec project open <repository>` for project work. Use `omp-admin` only for
explicit broad host work; it runs the pinned vendor OMP without Pisec
credentials or isolated XDG/profile paths and is never broker-restored.

The Linux installer is fail-closed: it checks the pinned binaries, Fence
Bubblewrap/Landlock/network capabilities, configuration domains, executable
targets, Collie inputs, and Funnel state before writing the user installation.
It creates `~/.omp/auth-gateway.token` with mode `0600` when no bearer token
exists; existing token files must already be regular owner-only files. It then
waits for the auth broker, auth gateway, Pisec broker, Pisec secretary, and
Herdr sockets and runs the final live JSON doctor before reporting success.

With user lingering enabled on Linux, the installed auth broker, auth gateway, Pisec
broker, and Herdr `main` session start from the user systemd boot target.
Herdr persists normal terminal/workspace state while Pisec restores only
durable active bindings through exact launchers; retired workers keep their
session files but are not relaunched. Resume is available after those
services become ready, not synchronously at kernel boot. Pisec's SQLite intent
survives independently of Herdr: a missing or mismatched runtime is reported
as `needs_attention`, never guessed complete.

The JSON doctor validates the active user service units, the broker and
gateway health endpoints, Herdr protocol 19, the enabled local Pisec and
Collie 0.28.0 plugins, the loopback-only Collie listener, the single HTTPS
Tailscale Serve root route to Collie, and Funnel being disabled. A missing or
malformed live probe is a failure rather than an informational warning.

Run the repository regression checks with:

```bash
python3 -m unittest discover -s tests
```

The deployment tests use isolated fake user homes and fake service commands.
An actual Linux full install additionally requires a host with user namespaces,
Landlock, network namespaces, a configured auth-broker provider credential,
a configured Tailscale identity, user-service lingering for reboot recovery,
and a reachable Collie/Herdr deployment.

## What's Included

### Tmux

The tmux status line includes Voxtype recording and microphone health:

- `🎤 CHECK` means a recording started and the watchdog is waiting for signal.
- `🎤 REC` means a real microphone signal was detected.
- `⚠ MIC` means no usable signal arrived within three seconds. The warning
  persists after recording stops, and a critical desktop notification and
  error sound are also emitted.

The watchdog reads Voxtype's local audio-level socket; it does not save audio.
It ignores the first second (so the startup beep cannot produce a false pass),
then requires sustained signal before the three-second deadline. Its default
threshold is `-55 dBFS`. Override calibration with
`VOXTYPE_MIC_GRACE_SECONDS`, `VOXTYPE_MIC_IGNORE_SECONDS`,
`VOXTYPE_MIC_REQUIRED_SIGNAL_FRAMES`, and `VOXTYPE_MIC_MIN_DBFS`.

- **Ctrl-a** prefix
- Vim-style navigation
- Session persistence (survives reboot)
- Catppuccin Mocha theme
- Status bar: session | windows | directory | git | app | time
### Neovim
- LazyVim base config
- Linked to `~/.config/nvim` by the full agent workflow installer
- `ChmaraX/herdr-nvim` integration for Herdr sidebars, file picking, and annotations

### Tools installed
- **gitmux** - git status in tmux
- **direnv** - auto-activate venvs

## Tmux Key Bindings

| Keys | Action |
|------|--------|
| `Ctrl-a h/j/k/l` | Navigate panes |
| `Ctrl-a n/p` | Next/previous window |
| `Ctrl-a \|` | Split vertical |
| `Ctrl-a _` | Split horizontal |
| `Ctrl-a \` | Toggle last session |
| `Ctrl-a s` | List sessions |
| `Ctrl-a $` | Rename session |
| `Ctrl-a ,` | Rename window |
| `Ctrl-a d` | Detach |
| `Ctrl-a Ctrl-s` | Save sessions |
| `Ctrl-a Ctrl-r` | Restore sessions |

## Auto-Activate Venvs

```bash
cd ~/project
echo 'source .venv/bin/activate' > .envrc
direnv allow
```
