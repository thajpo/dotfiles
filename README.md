# dotfiles

My terminal setup: tmux + neovim (LazyVim).

## Install and start

```bash
git clone https://github.com/thajpo/dotfiles.git
cd dotfiles
./install.sh
pi-start all
```

`pi-start all` boots tmux, the personal workspace, and the secretary grid. If
`~/.local/bin` is not on `PATH` yet, run `~/.local/bin/pi-start all` instead.

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
- Delegate bounded fresh-context scouts and workers through `pi-subagents`; fork
  only when the complete parent history is explicitly required.
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
- `pidev` opens or attaches a repo-scoped session with Neovim on the left and Pi on the right

### Neovim
- LazyVim base config
- Syncs to `~/.config/nvim`

### Pi
- Reproducible Pi CLI and exact extension package pins with verified patches
- Pi keeps the prompt editor and footer pinned while mouse-wheel scrolling through conversation history, so drafts remain editable while reading earlier responses
- Deterministic trusted-live/isolated repository policy and hardened Docker task containers
- One shared execution plane for parent, subagents, and pi-btw
- Explicit unsandboxed `pi-host` maintenance launcher
- `pi-start` boots tmux from a cold start; `pi --help-custom` (also `pi -help-custom`) documents workspace startup, recovery, and session switching
- `pidev` is the managed Pi development launcher; it records the canonical project directory and uses a stable session store so tmux-resurrect can reopen the same conversation even after a temporary worktree disappears
- In Pi, `Ctrl-g` enters conversation browse mode: use `j/k`, `Ctrl-u/d`, `gg/G`, `v`, and `y`; press `i`, `q`, or `Esc` to return to the pinned prompt
- `/fast` enables OpenAI's priority service tier for the current session; use `/fast off` to disable it
- `pisec` opens the default three secretary projects (`vla-lens`, `csv-agent`, `SleepyDreamyV3`); secretaries may fan out to any useful number of existing read-only investigator agents without creating Git worktrees, while implementation still requires an explicit full workstream; with an odd active count `pisec` gives the first project its own window and puts the remaining projects into two side-by-side panes; use `pisec activate ALIAS ...` or `pisec swap OLD_ALIAS NEW_ALIAS` to change the active set
- `pi-personal` maintains a separate four-pane session for `mlre-transition`, the financial workbook (`investing/investment-os`), dotfiles, and an explicit `pi-host` session; tmux ensures it on startup and `Ctrl-a p` switches to it
- Installs to `~/.pi/agent` without tracking credentials, sessions, or runtime state

### Tools installed
- **gitmux** - git status in tmux
- **direnv** - auto-activate venvs

## Tmux Key Bindings

| Keys | Action |
|------|--------|
| `Ctrl-a h/j/k/l` | Navigate panes |
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
