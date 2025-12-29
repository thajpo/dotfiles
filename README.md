# tmux-config

My tmux configuration with session persistence and auto-activating venvs.

## Features

- **Ctrl-a** prefix (instead of Ctrl-b)
- **Vim-style** pane navigation and copy mode
- **Mouse support** enabled
- **Session persistence** via tmux-resurrect + tmux-continuum
- **Auto-restore** sessions on tmux start
- **Nvim session restoration** - reopens your files
- **direnv integration** - auto-activates venvs per directory

## Installation

```bash
git clone https://github.com/thajpo/tmux-config.git
cd tmux-config
./install.sh
```

## Key Bindings

### Prefix

All commands start with `Ctrl-a` (not the default `Ctrl-b`).

### Navigation

| Keys | Action |
|------|--------|
| `Ctrl-a h/j/k/l` | Move between panes (vim-style) |
| `Ctrl-a H/J/K/L` | Resize panes |
| `Ctrl-a \|` | Split vertical |
| `Ctrl-a _` | Split horizontal |

### Sessions

| Keys | Action |
|------|--------|
| `Ctrl-a s` | List/switch sessions |
| `Ctrl-a $` | Rename session |
| `Ctrl-a ,` | Rename window |
| `Ctrl-a d` | Detach |

### Persistence

| Keys | Action |
|------|--------|
| `Ctrl-a Ctrl-s` | Save sessions |
| `Ctrl-a Ctrl-r` | Restore sessions |

Sessions auto-save every 15 minutes and auto-restore on tmux start.

## Multi-Project Workflow

Create named sessions for each project:

```bash
tmux new -s ml-research
tmux new -s agent-swarm
tmux new -s infra
```

Switch between them with `Ctrl-a s`.

## Auto-Activating Venvs

For each project with a venv:

```bash
cd ~/your-project
echo 'source .venv/bin/activate' > .envrc
direnv allow
```

Now the venv activates automatically when you enter the directory.

## Plugins

- [tpm](https://github.com/tmux-plugins/tpm) - Plugin manager
- [vim-tmux-navigator](https://github.com/christoomey/vim-tmux-navigator) - Seamless vim/tmux navigation
- [tmux-resurrect](https://github.com/tmux-plugins/tmux-resurrect) - Save/restore sessions
- [tmux-continuum](https://github.com/tmux-plugins/tmux-continuum) - Auto-save/restore

## File Locations

- Config: `~/.tmux.conf`
- Saved sessions: `~/.tmux/resurrect/`
- Plugins: `~/.tmux/plugins/`
