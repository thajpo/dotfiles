# dotfiles

## Pi Harness System

The fresh Pi system in
[PI_IMPLEMENTATION_PLAN.md](PI_IMPLEMENTATION_PLAN.md)
is the working Pi product. It is installed and activated on this machine
(`release-passed` on 2026-08-11 against
`build_2faecfee6b4280149422721827640bb4`), and the daily surface commands
(`pi-restart`, `pisec`, `pi-personal`, `pi-start`, `pidev`) drive the
Pi controller and its controller-bound conversations.

Controller state uses `~/.local/state/pi-system/`, installed artifacts use
`~/.local/share/pi-system/`, and controller-owned working copies use
`~/.local/share/pi-system-work/`. Pre-controller chats and lifecycle files are
not imported or consulted.

My terminal setup: tmux + neovim (LazyVim).

## Pi Harness Development

```bash
git clone https://github.com/thajpo/dotfiles.git
cd dotfiles
stage=$(mktemp -d /tmp/pi-system-stage.XXXXXX)
rmdir "$stage"
./bin/pi-install stage --staging-root "$stage"
./bin/pi-install verify --staging-root "$stage"
./bin/pi-install activate --staging-root "$stage" --data-root "$HOME/.local/share/pi-system"
./bin/pi-install init-state --state-root "$HOME/.local/state/pi-system"
./bin/pi-control schema status
```

These commands exercise the staging API; they are not proof that the installed
Pi chat product is ready. Acceptance must launch real controller-bound
secretary and coding conversations through the final installed paths.

Machine-specific non-secret settings live in `machines/`. Repository trust for
the Pi harness is bound to controller-registered projects by host policy,
not inferred from old chat or presentation state.

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
- Let host policy and the registered project select the allowed execution mode.
- Bind each coding run to one controller-assigned working copy and runtime.
- Use Sol for unresolved architecture, difficult debugging, and high-risk review.
- Unsandboxed machine maintenance is an explicit host-only decision outside the
  Pi harness; reviewed Pi control-plane activation runs through `bin/pi-activate`.

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
- Fresh controller projects, conversations, runs, workstreams, and messages
- Controller-scoped host readers and exact one-use command approvals
- One assigned working copy, writer generation, and isolated runtime per coding run
- Exact submitted revisions, independent review receipts, and guarded integration
- Fresh session continuity without importing pre-controller Pi chats
- Tmux presentation derived from controller state rather than pane identity
- Reproducible installation and rollback gates before replacing OpenCode

See [`pi/README.md`](pi/README.md) for the product boundary and
[`pi/control-plane/README.md`](pi/control-plane/README.md) for canonical
contracts and current readiness.

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
