#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Installing Pi configuration..."
PI_CONFIG_DIR="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
PI_VERSION="$(cat "$SCRIPT_DIR/pi/PI_VERSION")"

backup_and_link() {
    local source="$1"
    local target="$2"
    if [ -e "$target" ] && [ ! -L "$target" ]; then
        mv "$target" "$target.bak.$(date +%Y%m%d%H%M%S)"
    fi
    mkdir -p "$(dirname "$target")"
    ln -sfn "$source" "$target"
}

if ! command -v pi >/dev/null 2>&1 || [ "$(pi --version 2>/dev/null || true)" != "$PI_VERSION" ]; then
    echo "Installing Pi CLI ${PI_VERSION}..."
    npm install --global "@earendil-works/pi-coding-agent@${PI_VERSION}"
fi

mkdir -p "$PI_CONFIG_DIR"
backup_and_link "$SCRIPT_DIR/pi/settings.json" "$PI_CONFIG_DIR/settings.json"
for config in pi-chrome-devtools.json pi-plan-mode.json pi-statusline.json pr-review.json; do
    backup_and_link "$SCRIPT_DIR/pi/$config" "$PI_CONFIG_DIR/$config"
done
backup_and_link "$SCRIPT_DIR/pi/extensions" "$PI_CONFIG_DIR/extensions"
backup_and_link "$SCRIPT_DIR/pi/agents" "$PI_CONFIG_DIR/agents"
backup_and_link "$SCRIPT_DIR/pi/prompts" "$PI_CONFIG_DIR/prompts"
backup_and_link "$SCRIPT_DIR/pi/themes" "$PI_CONFIG_DIR/themes"
mkdir -p "$PI_CONFIG_DIR/npm"
cp "$SCRIPT_DIR/pi/npm/package.json" "$SCRIPT_DIR/pi/npm/package-lock.json" "$PI_CONFIG_DIR/npm/"
npm ci --prefix "$PI_CONFIG_DIR/npm" --legacy-peer-deps --no-audit --no-fund
PI_CODING_AGENT_DIR="$PI_CONFIG_DIR" "$SCRIPT_DIR/scripts/pi-patch-subagents"

mkdir -p "$HOME/.local/bin"
backup_and_link "$SCRIPT_DIR/bin/pi" "$HOME/.local/bin/pi"
echo "Installed Pi ${PI_VERSION} configuration and silent completion wrapper"

echo "Installing tmux config..."

# Link tmux and gitmux configs into the repository.
backup_and_link "$SCRIPT_DIR/tmux.conf" "$HOME/.tmux.conf"
echo "Linked tmux.conf to ~/.tmux.conf"

if [ -f gitmux.conf ]; then
    backup_and_link "$SCRIPT_DIR/gitmux.conf" "$HOME/.gitmux.conf"
    echo "Linked gitmux.conf to ~/.gitmux.conf"
fi

# Install the Voxtype microphone-signal watchdog when Voxtype is present.
if command -v voxtype &> /dev/null && [ -f systemd/user/voxtype-mic-watchdog.service ]; then
    backup_and_link "$SCRIPT_DIR/systemd/user/voxtype-mic-watchdog.service" "$HOME/.config/systemd/user/voxtype-mic-watchdog.service"
    systemctl --user daemon-reload
    systemctl --user enable --now voxtype-mic-watchdog.service
    echo "Installed Voxtype microphone watchdog"
fi

# Install shared agent workflow defaults for OpenCode, Claude Code, and Codex.
if [ -x scripts/agent-workflow-install.sh ]; then
    scripts/agent-workflow-install.sh
fi

# Link nvim config into the repository.
if [ -d nvim ]; then
    backup_and_link "$SCRIPT_DIR/nvim" "$HOME/.config/nvim"
    echo "Linked nvim config to ~/.config/nvim"
fi

# Install TPM if not present
if [ ! -d ~/.tmux/plugins/tpm ]; then
    echo "Installing TPM (Tmux Plugin Manager)..."
    git clone https://github.com/tmux-plugins/tpm ~/.tmux/plugins/tpm
fi

# Install plugins
echo "Installing tmux plugins..."
~/.tmux/plugins/tpm/bin/install_plugins

# Install gitmux if not present
if ! command -v gitmux &> /dev/null; then
    echo "Installing gitmux..."
    mkdir -p ~/.local/bin
    curl -sL https://github.com/arl/gitmux/releases/download/v0.11.5/gitmux_v0.11.5_linux_amd64.tar.gz | tar xz -C ~/.local/bin
    if ! grep -q '.local/bin' ~/.bashrc 2>/dev/null; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
    fi
    echo "Installed gitmux to ~/.local/bin"
fi

# Install direnv if not present
if ! command -v direnv &> /dev/null; then
    echo "Installing direnv..."
    if command -v apt &> /dev/null; then
        sudo apt install -y direnv
    elif command -v brew &> /dev/null; then
        brew install direnv
    else
        echo "Please install direnv manually: https://direnv.net/docs/installation.html"
    fi
fi

# Add direnv hook to bashrc if not present
if ! grep -q "direnv hook bash" ~/.bashrc 2>/dev/null; then
    echo "" >> ~/.bashrc
    echo "# direnv - auto-activate venvs when entering directories" >> ~/.bashrc
    echo 'eval "$(direnv hook bash)"' >> ~/.bashrc
    echo "Added direnv hook to ~/.bashrc"
fi

# Reload tmux config if tmux is running
if tmux info &> /dev/null; then
    tmux source-file ~/.tmux.conf
    echo "Reloaded tmux config"
fi

echo ""
echo "Done! Run 'source ~/.bashrc' or open a new terminal."
echo "Then start tmux with: tmux new -s <session-name>"
