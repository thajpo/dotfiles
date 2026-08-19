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
Codex, and keeps OpenCode as an existing skills-only adapter through `~/.skills`:

- `git pull --rebase --autostash origin master`
- commit/push only when there are local changes

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

The default agent workflow is harness-agnostic and uses OpenCode when a harness
choice is needed.

Install or repair the shared memory/config symlinks:

```bash
~/dotfiles/scripts/agent-workflow-install.sh
```

Inspect the setup:

```bash
~/dotfiles/scripts/agent-workflow-doctor.sh
```
On Apple Silicon macOS, the shared OMP/skills workflow and launchd sync are
supported. The full Pisec fenced stack is intentionally Linux-only until a
security-equivalent macOS sandbox and service adapter are approved; the
installer fails before mutation instead of silently weakening isolation.



### Pisec workflow broker

Pisec is the durable, product-neutral workflow and security core. It owns
projects, proposals, approvals, workstream intent, decisions, runtime
bindings, research packets, and audit events. The selected `HarnessAdapter`
and `WorkspaceAdapter` own product wire protocols and artifacts. The tested
production adapters are OMP 17.3.4-compatible (`omp`) and Herdr 0.8.x
protocol 19 (`herdr`); Collie remains deployment and presentation glue.

The epoch-three configuration is explicit and adapter-neutral:

```json
{
  "schemaVersion": 3,
  "fencePath": "/absolute/path/to/fence",
  "harness": {"id": "omp", "config": {}},
  "workspace": {"id": "herdr", "config": {}}
}
```

The concrete adapter config fills the OMP executable/gateway/model/network
values and the dedicated Herdr session/socket. The installer validates the
selected adapters and pinned public interfaces before mutating user state.
`--reset-pisec-state` is an explicit epoch-two archive-and-reset operation,
not an in-place migration: it atomically archives the owner-only state
directory, deploys epoch three, retains the archive, and prints its path.

For an explicit archive-and-reset deployment:

```bash
~/dotfiles/scripts/agent-workflow-install.sh \
  --collie-host myhost.my-tailnet.ts.net \
  --collie-trusted-user you@example.com \
  --reset-pisec-state
```

Review the printed epoch-two archive path after deployment. Pisec does not
patch OMP, Herdr, or Fence source or binaries. The deployment applies one
reviewed, fail-closed Collie 0.28.x source patch at the presentation boundary;
it derives `done` from Collie's existing shared unread ledger without changing
Herdr or Pisec lifecycle authority.

The full deployment also links this repository's LazyVim configuration at
`~/.config/nvim`, installs the `chmarax.herdr-nvim` and
`persiyanov.reviewr` Herdr plugins when absent, and sets Herdr's prefix to
`Ctrl-a`. Managed plugin bindings are `Ctrl-a Shift-e` (toggle Neovim),
`Ctrl-a Shift-f` (pick a file), and `Ctrl-a Shift-v` (toggle reviewr);
unrelated Herdr key configuration is preserved.

Every worker receives one linked Git worktree, one namespaced branch, one
private Git object store, one isolated harness home, one rendered Fence
policy, one immutable broker-owned task packet, and one adapter-owned
workspace surface.
Workers retain the OMP adapter's built-in `web_search` path through
DuckDuckGo and include `html.duckduckgo.com` in the immutable approval scope:

- `worker-default` permits only that web-search domain plus the loopback model
  gateway.
- `worker-networked` additionally permits the exact external domains named in
  the approved proposal. It does not receive live policy widening.

Both profiles deny undeclared domains and direct `curl`, `wget`, `http`, and
`httpie` execution. They also deny push, publish, SSH, container engines, and
other destructive Git maintenance. The networked profile widens destination
reachability; it does not relax filesystem or command restrictions. A live
Fence smoke check should use `real-omp search` from a materialized worker,
because that exercises OMP's actual built-in search provider rather than a
stand-in HTTP client.

Tool lists and approval prompts are workflow/UX controls, not security
boundaries. Fence remains the hard process, filesystem, and network boundary;
installed plugins and project MCP run inside the same role Fence.

The OMP adapter keeps these execution profiles:

| Profile | Network scope | Intended role |
|---|---|---|
| `secretary-project` | Loopback model gateway and approved public web access | Project secretary |
| `worker-default` | Loopback model gateway plus `html.duckduckgo.com` | Isolated worker |
| `worker-networked` | Worker baseline plus exact proposal domains | Worker needing approved external access |

Additional worker domains are immutable approval inputs; no live policy
widening is available.

Pisec deliberately does not merge or delete branches. The closeout sequence is:

1. The worker commits reviewable changes on its Pisec branch and reports the
   result to the project coordinator.
2. The coordinator can inspect and use normal local Git inside the registered
   project. A trusted host shell may review independently; fenced workers
   cannot push. A coordinator publishes an existing non-default origin branch
   with `pisec_push_branch`. The host broker pins the registered origin,
   requires exact local and remote commit IDs, proves the update is a
   fast-forward, and uses a lease to reject a remote race. Raw `git push`,
   branch creation/deletion, force push, and autonomous default-branch updates
   remain denied. Worker creation and merge application still require exact
   user approval in the OMP UI.
3. After the human accepts the result, the coordinator marks the workstream
   complete and then retires it. Completion records desired state only;
   retirement closes the worker task tab and fenced runtime; the project room
   survives.
4. The host runs `pisec workstream cleanup ...` to remove the linked checkout
   and isolated runtime capability. Cleanup refuses an active or dirty worktree
   unless explicitly forced. It retains both the branch and private Git object
   store because the branch may still depend on those objects.
5. Delete the retained branch separately, only after verifying integration.
   Private-object purging is intentionally not part of cleanup; before adding
   such a purge, prove no retained Git ref depends on that store.

The OMP harness is launched through the private `pisec/runtime-bin/omp`
shim. Herdr workspace cold restore therefore re-enters Fence before OMP,
rather than replaying an unfenced surface command. Each managed session
snapshots `~/.omp/agent/config.yml` as its base OMP configuration, then loads
the Pisec overlay for gateway, workspace, and search wiring. Pisec launches
OMP in `yolo` tool-approval mode because Fence owns command, filesystem, and
network enforcement; exact semantic Pisec approvals (such as workstream
creation and merge application) remain separate extension-level checks.
Pisec-owned OMP panes have exactly one lifecycle reporter. Each isolated OMP
home excludes `herdr-omp-agent-state.ts`; the private launcher explicitly loads
`omp/extensions/pisec.ts`, which publishes the per-runtime
`pisec:omp:<hash>` source. Pisec never tries to release `herdr:omp`: Herdr 0.8
intentionally ignores releases of official sources even though the protocol
returns `type: ok`.

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
session and project/workstream/worktree/branch identities, resumes the same
session, and verifies a real Pisec-to-Herdr `working` to `idle` transition.
Busy bindings remain pending; failures are explicit and trigger a best-effort
runtime restore. Repeating the command at the current generation performs no
restart. The normal installer runs this refresh after migration and service
startup and before the final doctor.
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
Secretary wake-ups carry only the project and inbox generation, so concurrent
requests coalesce without exposing packet bodies. Wake delivery is
at-least-once; a repeated wake is safe because request claims and packets are
idempotent. The secretary claims requests and launches the fixed
`pisec-web-research` `@smol` child in one task batch; that child is read-only
and limited to `read` and `web_search`. Answers use the exact bounded citation
schema, persist as immutable SQLite packets, replay after restart, and are
acknowledged by the worker after consumption. Workspace downtime affects
prompt immediacy only; request and answer truth remains in Pisec.

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
The old bare `pisec` secretary-grid launcher belonged to the retired
controller-driven Pi implementation under `archive/custom-pi/`; the active
Pisec command is the host administration CLI shown above.

#### Verified epoch-three acceptance

The live acceptance reset and adapter cutover were rerun on 2026-08-16. The
installer archived the prior owner-only state and deployed schema epoch three.
`pisec doctor --json` reported `schemaVersion=3`,
`pisec-core-epoch-3`, selected harness `omp`, selected workspace `herdr`,
OMP 17.3.4-compatible health, Herdr protocol 19, launch-map v2, and healthy
Fence/plugin/MCP/search checks. After worker cleanup, the same doctor command
returned `ok: true`; cleaned retired bindings no longer require deleted
harness artifacts.

The disposable repository `/tmp/pisec-live-acceptance-epoch3` exercised the
secretary and worker through the actual Herdr/OMP surfaces. Exact OMP approval
scopes bound both adapter IDs before effects. The secretary performed local
Git read/write/commit operations; the worker used a namespaced worktree,
private Git objects, an immutable task packet, and the OMP adapter's
`worker-default` Fence policy. The worker committed, completed, retired, and
was cleaned without deleting its branch or private object store. Cleanup
removed only the checkout, harness home, and launch-map entry.

The acceptance run also verified copied user/plugin surface materialization
at launch, project MCP/search settings, sibling/host-secret/metadata and
denied raw-push/SSH/publish/privilege/container commands, baseline web search,
unapproved-domain denial, approved fast-forward merge with object promotion,
and no common Git alternate back-link. Built-in OMP `web_search` returned a
public result; durable research produced one fixed `@smol` task batch for
coalesced requests, schema-valid sourced packets, decline and
needs-context/context-add paths, replay-safe acknowledgement, and no
duplicate durable packets. Wake delivery remains at-least-once, so prompt
retries are expected to be harmless.

Broker, secretary, worker, and Herdr restart/reconcile checkpoints were
exercised against SQLite, broker/Herdr observations, and Git state rather than
model prose. The repository checks completed with:

```text
python3 -m unittest discover -s tests       93 tests, OK
bun test omp/extensions/pisec.test.ts      3 pass, 0 fail
```

Workstream creation is deliberately two-stage. The secretary first prepares
an immutable scope containing the title, purpose, full brief, target/base
commit, branch, checkout, Fence profile, domains, and effect/non-effect
lists. The user must approve that exact scope in the OMP UI; declining it
creates no Git, harness, or workspace resource. Completion and retirement
change desired state only: they never delete a checkout or branch. Cleanup is
a separate host-admin operation, refuses active/dirty worktrees by default,
removes the linked checkout, isolated harness home, and launch-map entry, and
proves the branch and private Git object store remain.


Collie is pinned to the Herdr `main` session and is a mobile presentation of
Pisec project rooms, coordinator chats, and active worker tabs. It has no
project registry or lifecycle authority. Herdr supplies semantic lifecycle
state; the pinned downstream patch in `patches/collie-v0.28-unread-idle.patch`
projects a resting agent as `done` only while Collie's persisted
`lastActiveAt > lastSeenAt`, then returns it to `idle` after a Collie read.
The installer applies the patch idempotently to the managed Collie checkout
and fails closed if a future 0.28.x source no longer matches.

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

The installer is fail-closed: it checks the pinned binaries, Fence
Bubblewrap/Landlock/network capabilities, configuration domains, executable
targets, Collie inputs, and Funnel state before writing the user installation.
It creates `~/.omp/auth-gateway.token` with mode `0600` when no bearer token
exists; existing token files must already be regular owner-only files. It then
waits for the auth broker, auth gateway, Pisec broker, Pisec secretary, and
Herdr sockets and runs the final live JSON doctor before reporting success.

With user lingering enabled, the installed auth broker, auth gateway, Pisec
broker, and Herdr `main` session start from the user systemd boot target.
Herdr persists normal terminal/workspace state while Pisec restores only
durable active bindings through exact launchers; retired workers keep their
session files but are not relaunched. Resume is available after those
services become ready, not synchronously at kernel boot. Pisec's SQLite intent
survives independently of Herdr: a missing or mismatched runtime is reported
as `needs_attention`, never guessed complete.

The JSON doctor validates the active user service units, the broker and
gateway health endpoints, Herdr protocol 19, the enabled local Pisec and
Collie 0.28.x plugins, the loopback-only Collie listener, the single HTTPS
Tailscale Serve root route to Collie, and Funnel being disabled. A missing or
malformed live probe is a failure rather than an informational warning.

Run the repository regression checks with:

```bash
python3 -m unittest discover -s tests
```

The deployment tests use isolated fake user homes and fake service commands.
An actual full install additionally requires a host with user namespaces,
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
