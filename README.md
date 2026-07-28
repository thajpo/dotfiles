# dotfiles

My terminal setup: tmux + neovim (LazyVim).

## Install

```bash
git clone https://github.com/thajpo/dotfiles.git
cd dotfiles
./install.sh
```

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
LOCK_FILE="/tmp/dotfiles-sync.lock"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  exit 0
fi

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

Note: macOS may not have `flock` by default. If needed, install `flock` (`brew install flock`) or remove the lock block from the script.

## Agent Engineering Workflow

The default agent workflow is harness-agnostic and uses OpenCode when a harness
choice is needed.

Install or repair the shared memory/config symlinks:

```bash
~/dotfiles/scripts/agent-workflow-install.sh
```

Install the optional workflow tools and clone Firstmate:

```bash
~/dotfiles/scripts/agent-workflow-install.sh --all
```

Inspect the setup:

```bash
~/dotfiles/scripts/agent-workflow-doctor.sh
```

Core loop:

- Run parallel agents in isolated worktrees with Treehouse.
- Validate PR-bound work with No Mistakes: `git push no-mistakes`
- Run bounded long tasks with GNHF using explicit token/iteration caps.
- For multiple parallel tasks or multi-repo work, launch OpenCode from
  `~/agent-workflows/firstmate` inside tmux and let Firstmate supervise the
  worker agents.

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
- Syncs to `~/.config/nvim`

### Pi
- Reproducible Pi CLI version and pinned extension package manifests
- Local extensions, prompts, theme, statusline, LSP, plan-mode, review, and workboard configuration
- Installs to `~/.pi/agent` without tracking credentials, sessions, or runtime state
- Keeps completion desktop notifications but disables completion sounds by default

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
