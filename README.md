# dotfiles

## Pi system implementation

The fresh Pi system is implemented in modular slices from
[PI_GREENFIELD_IMPLEMENTATION_PLAN.md](PI_GREENFIELD_IMPLEMENTATION_PLAN.md).
Its controller state lives under `~/.local/state/pi-system/`, coding work is
isolated under `~/.local/share/pi-system-work/`, and OpenCode remains
available until the installed acceptance and rollback gates pass.

My terminal setup: tmux + neovim (LazyVim).

## Install and start

```bash
git clone https://github.com/thajpo/dotfiles.git
cd dotfiles
./install.sh
pi-root-session migrate --dry-run
pi-restart
```

Review and apply the root-session migration before restarting an existing Pi
workspace. `pi-image-tools@1.4.0` owns `Ctrl+V` and persists native image
attachments without sharing host `/tmp` with task containers.

`pi-restart` applies the new generation and always starts both personal and
secretary. Desktop tmux is the default for every invocation; add `-mobile`,
`-herdr`, or both to select mobile tmux, desktop Herdr, or mobile Herdr. Herdr
uses separate named `pi-personal` and `pi-secretary` sessions. Flags are not
persisted. If the launcher is not on `PATH` yet, run
`~/.local/bin/pi-restart` instead.

Machine-specific non-secret settings live in `machines/`. On Apple Silicon
macOS and Linux x86_64, `install.sh` selects the matching profile and installs
it as `~/.config/dotfiles/machine.env`; this is where the Pi personal project
paths and trusted project roots are defined.

## Shared CLI Skills + Dotfiles Auto-Sync

This repo can track shared skills for both Codex and OpenCode by using one
canonical directory (`~/dotfiles/skills`) and symlinking all CLI skill paths to
`~/.skills`:

- `git pull --rebase --autostash origin master`
- commit/push only when there are local changes

### Linux (systemd user timer)

Symlink setup:

```bash
mkdir -p ~/dotfiles/skills
ln -sfn ~/dotfiles/skills ~/.skills
ln -sfn ~/.skills ~/.codex/skills
ln -sfn ~/.skills ~/.config/opencode/skills
ln -sfn ~/dotfiles ~/.dotfiles
```

Optional drift repair (recommended):

```bash
~/dotfiles/scripts/skills-doctor.sh
```

Create sync script:

```bash
mkdir -p ~/dotfiles/scripts
cat > ~/dotfiles/scripts/dotfiles-sync.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$HOME/dotfiles"
LOCK_DIR="/tmp/dotfiles-sync.lockdir"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  exit 0
fi
trap 'rmdir "$LOCK_DIR"' EXIT

cd "$REPO_DIR"
git pull --rebase --autostash origin master

if [ -n "$(git status --porcelain)" ]; then
  git add -A
  git commit -m "chore(dotfiles): automated sync $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  git push origin master
fi
EOF
chmod +x ~/dotfiles/scripts/dotfiles-sync.sh
```

Create and enable timer (every 2 hours):

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/dotfiles-sync.service <<'EOF'
[Unit]
Description=Dotfiles auto sync (pull, commit, push)

[Service]
Type=oneshot
ExecStart=%h/dotfiles/scripts/dotfiles-sync.sh
EOF

cat > ~/.config/systemd/user/dotfiles-sync.timer <<'EOF'
[Unit]
Description=Run dotfiles auto sync every 2 hours

[Timer]
OnBootSec=10m
OnUnitActiveSec=2h
Persistent=true
Unit=dotfiles-sync.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now dotfiles-sync.timer
```

### macOS (launchd)

Use the same symlink setup as above, then create:

```bash
mkdir -p ~/Library/LaunchAgents
cat > ~/Library/LaunchAgents/com.user.dotfiles-sync.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.user.dotfiles-sync</string>
    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>-lc</string>
      <string>$HOME/dotfiles/scripts/dotfiles-sync.sh</string>
    </array>
    <key>StartInterval</key>
    <integer>7200</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/dotfiles-sync.out</string>
    <key>StandardErrorPath</key>
    <string>/tmp/dotfiles-sync.err</string>
  </dict>
</plist>
EOF

launchctl unload ~/Library/LaunchAgents/com.user.dotfiles-sync.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/com.user.dotfiles-sync.plist
```

The checked-in sync script uses a portable `mkdir` lock, so the same approach works on Linux and macOS.

## Agent Engineering Workflow

The default agent workflow is harness-agnostic and uses OpenCode when a harness
choice is needed.

Install or repair the shared memory/config symlinks:

```bash
~/dotfiles/scripts/agent-workflow-install.sh
```

Legacy coding-agent orchestrator installation has been removed. Pi uses `pi-subagents` as its only coding-agent orchestrator.

Inspect the setup:

```bash
~/dotfiles/scripts/agent-workflow-doctor.sh
```

Core loop:

- Use Luna as the persistent Pi parent and decision authority.
- Select FAST, RIP, BUILD, or MAJOR from uncertainty and consequence; select
  OFF, LIGHT, or DEEP learning independently.
- Delegate fresh-context scouts and workers through `pi-subagents`; the brief and
  contract are bounded for anti-slop, but spawned models have no automatic turn,
  elapsed-time, token, or tool-call limit. Fork only when the complete parent
  history is explicitly required.
- Keep current task state in Pi's session-scoped task packet and keep detailed
  artifacts outside repositories.
- Let the host policy select trusted-live for authored repositories and isolated
  private clones for unknown/external repositories.
- Share one task container across collaborating agents; use linked worktrees and
  separate containers only for explicit independent candidates.
- Use Sol for unresolved architecture, difficult debugging, and high-risk review.
- Use `pi-host` explicitly for unsandboxed machine maintenance and reviewed Pi
  control-plane activation.

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
- `pidev` opens or attaches a repo-scoped session with Neovim on the left and Pi on the right; `pidev --mobile` opens Pi in a full-width window without a split

### Neovim
- LazyVim base config
- Syncs to `~/.config/nvim`

### Pi
- Reproducible Pi CLI and exact extension package pins with verified patches
- Pi keeps the prompt editor and footer pinned while mouse-wheel scrolling through conversation history, so drafts remain editable while reading earlier responses
- Deterministic trusted-live/isolated repository policy and hardened Docker task containers
- One shared execution plane for parent, subagents, and pi-btw
- Explicit unsandboxed `pi-host` maintenance launcher; it resumes one stable host conversation per invocation directory, enforces one writer for that session directory, stays in that directory, and does not create a Git worktree
- `pi-start all` always starts personal plus secretary with a simple per-invocation matrix: no flags = desktop tmux, `-mobile` = mobile tmux, `-herdr` = desktop Herdr, and `-mobile -herdr` = mobile Herdr; if a proven managed grid is in another matrix cell, `pi-start all` performs the same guarded rebuild automatically, while `pi-restart` with the same flags forces a clean rebuild; flags are not persisted; `pi help` is the short memorable guide, `pi help all` is the full workspace/recovery reference, and `pi --help` remains the upstream CLI reference
- `pidev` is the managed Pi development launcher; it records the canonical project directory and uses a stable session store so tmux-resurrect can reopen the same conversation even after a temporary worktree disappears
- In Pi, `Ctrl-g` enters conversation browse mode: use `j/k`, `Ctrl-u/d`, `gg/G`, `v`, and `y`; press `i`, `q`, or `Esc` to return to the pinned prompt
- `/fast` uses OpenAI's priority service tier by default for the current session; use `/fast off` to disable it or `/fast on` to re-enable it
- `/goal <objective>` uses pi-goal for same-session continuation until the task
  completes, blocks on an external dependency, or the user stops it; no default
  response-count, elapsed-time, or no-progress cutoff is imposed
- `/observe` or `Ctrl+I` opens a read-only Task/Fleet/Messages Inspector for explicit task packets, child instructions, context summaries, status, results, and failures
- Every project and subagent can submit bounded harness self-improvement feedback through `harness_feedback` or the parent channel; Pi logs it centrally under `~/.pi/agent/feedback/records/`, and `pi-harness-feedback` reviews the all-project feed
- Interactive subagent runs return control instead of blocking on `subagent_wait`; completion notifications and status/control actions remain available
- `pisec` opens the persisted active secretary set (initially `vla-lens`, `csv-agent`, and `SleepyDreamyV3`) without restarting live panes; `pisec launch` is the explicit idle-pane repair path; secretaries may fan out to any useful number of existing read-only investigator agents without creating Git worktrees, while implementation still requires an explicit full workstream; with an odd active count `pisec` gives the first project its own window and puts the remaining projects into two side-by-side panes; use `pisec activate ALIAS ...` or `pisec swap OLD_ALIAS NEW_ALIAS` to change the backend-neutral active set; add `--mobile` for one full-width conversation per window
- `-herdr` starts two named Herdr surfaces: `pi-personal` for the four durable personal conversations and `pi-secretary` with one project Space per active project; desktop personal pairs roles in two Spaces, while mobile personal gives each role its own Space; workstreams remain guarded and backend-pinned, and are never auto-migrated
- `pi-personal` maintains a separate session with at most two side-by-side panes per window for `mlre-transition`, the financial workbook (`investing/investment-os`), dotfiles, and an explicit `pi-host` session; additional roles get additional windows, and `Ctrl-a p` switches to it; `pi-personal --mobile` gives each role its own full-width window
- Installs to `~/.pi/agent` without tracking credentials, sessions, or runtime state

### Tools installed
- **gitmux** - git status in tmux
- **direnv** - auto-activate venvs

## Tmux Key Bindings

| Keys | Action |
|------|--------|
| `Ctrl-a h/j/k/l` | Navigate panes |
| `Ctrl-a n/p` | Next/previous conversation window in mobile mode |
| `Ctrl-a \|` | Split vertical |
| `Ctrl-a _` | Split horizontal |
| `Ctrl-a \` | Toggle last session |
| `Ctrl-a p` | Switch to `pi-personal` |
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
