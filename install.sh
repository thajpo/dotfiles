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

install_control_file() {
    local source="$1" target="$2" mode="${3:-600}"
    mkdir -p "$(dirname "$target")"
    local temporary="${target}.new.$$"
    install -m "$mode" "$source" "$temporary"
    if [ -e "$target" ] || [ -L "$target" ]; then
        mv "$target" "${target}.rollback.$(date -u +%Y%m%dT%H%M%SZ).$$"
    fi
    mv "$temporary" "$target"
}

install_control_tree() {
    local source="$1" target="$2"
    mkdir -p "$(dirname "$target")"
    local temporary="${target}.new.$$"
    rm -rf "$temporary"
    cp -a "$source" "$temporary"
    if [ -e "$target" ] || [ -L "$target" ]; then
        mv "$target" "${target}.rollback.$(date -u +%Y%m%dT%H%M%SZ).$$"
    fi
    mv "$temporary" "$target"
}

if ! command -v pi >/dev/null 2>&1 || [ "$(pi --version 2>/dev/null || true)" != "$PI_VERSION" ]; then
    echo "Installing Pi CLI ${PI_VERSION}..."
    npm install --global "@earendil-works/pi-coding-agent@${PI_VERSION}"
fi

mkdir -p "$PI_CONFIG_DIR"
STAGING_DIR=$(mktemp -d "$PI_CONFIG_DIR/.install.XXXXXX")
DOCKER_STAGING_IMAGE=""
cleanup_staging() {
    rm -rf "$STAGING_DIR"
    if [ -n "$DOCKER_STAGING_IMAGE" ]; then docker image rm "$DOCKER_STAGING_IMAGE" >/dev/null 2>&1 || true; fi
}
trap cleanup_staging EXIT
mkdir -p "$STAGING_DIR/npm"
cp "$SCRIPT_DIR/pi/npm/package.json" "$SCRIPT_DIR/pi/npm/package-lock.json" "$STAGING_DIR/npm/"
npm ci --prefix "$STAGING_DIR/npm" --legacy-peer-deps --no-audit --no-fund
PI_CODING_AGENT_DIR="$STAGING_DIR" "$SCRIPT_DIR/scripts/pi-patch-subagents"
if command -v docker >/dev/null 2>&1; then
    DOCKER_STAGING_IMAGE="pi-tool-sandbox:staging-$$"
    docker build --pull=false -t "$DOCKER_STAGING_IMAGE" "$SCRIPT_DIR/pi/sandbox"
else
    echo "Warning: Docker is unavailable; Pi coding sessions will fail closed" >&2
fi

POLICY_DIR="$HOME/.config/pi"
POLICY_PATH="$POLICY_DIR/repository-policy.json"
mkdir -p -m 700 "$POLICY_DIR" "$PI_CONFIG_DIR/generated"
if [ -e "$POLICY_PATH" ] || [ -L "$POLICY_PATH" ]; then
    [ ! -L "$POLICY_PATH" ] && [ -f "$POLICY_PATH" ] && [ -O "$POLICY_PATH" ] || {
        echo "Refusing unsafe existing repository policy: $POLICY_PATH" >&2
        exit 1
    }
    chmod 600 "$POLICY_PATH"
    "$SCRIPT_DIR/scripts/pi-workspace.py" validate-policy "$POLICY_PATH" >/dev/null
    echo "Preserved existing repository policy: $POLICY_PATH"
else
    install -m 600 "$SCRIPT_DIR/pi/repository-policy.json" "$POLICY_PATH"
    echo "Installed repository policy: $POLICY_PATH"
fi

# Activate only after package patching, image build, and policy validation pass.
if [ -e "$PI_CONFIG_DIR/npm" ]; then
    mv "$PI_CONFIG_DIR/npm" "$PI_CONFIG_DIR/npm.rollback.$(date -u +%Y%m%dT%H%M%SZ).$$"
fi
mv "$STAGING_DIR/npm" "$PI_CONFIG_DIR/npm"
# Host-executing Pi extensions/settings are installed copies, never live
# symlinks into the editable repository. AGENTS.md remains the requested global
# dotfiles symlink.
install_control_file "$SCRIPT_DIR/pi/settings.json" "$PI_CONFIG_DIR/settings.json"
backup_and_link "$SCRIPT_DIR/agent/AGENTS.md" "$PI_CONFIG_DIR/AGENTS.md"
for config in pi-chrome-devtools.json pi-plan-mode.json pi-statusline.json pr-review.json; do
    install_control_file "$SCRIPT_DIR/pi/$config" "$PI_CONFIG_DIR/$config"
done
install_control_tree "$SCRIPT_DIR/pi/extensions" "$PI_CONFIG_DIR/extensions"
install_control_tree "$SCRIPT_DIR/pi/agents" "$PI_CONFIG_DIR/agents"
install_control_tree "$SCRIPT_DIR/pi/prompts" "$PI_CONFIG_DIR/prompts"
install_control_tree "$SCRIPT_DIR/pi/themes" "$PI_CONFIG_DIR/themes"
if [ -n "$DOCKER_STAGING_IMAGE" ]; then
    docker image tag "$DOCKER_STAGING_IMAGE" pi-tool-sandbox:node22-bookworm-20260728
    docker image rm "$DOCKER_STAGING_IMAGE" >/dev/null
    DOCKER_STAGING_IMAGE=""
    echo "Built hardened Pi task image"
fi
rmdir "$STAGING_DIR"
trap - EXIT

mkdir -p "$HOME/.local/bin"
install_control_file "$SCRIPT_DIR/scripts/pi-workspace.py" "$HOME/.local/share/pi/control/pi-workspace.py" 755
install_control_file "$SCRIPT_DIR/bin/pi" "$HOME/.local/bin/pi" 755
install_control_file "$SCRIPT_DIR/bin/pi-host" "$HOME/.local/bin/pi-host" 755
# Installed launchers resolve their helper from the reviewed control directory.
python3 - "$HOME/.local/bin/pi" "$HOME/.local/bin/pi-host" <<'PY'
from pathlib import Path
import sys
for name in sys.argv[1:]:
    path = Path(name)
    text = path.read_text()
    text = text.replace('helper="$dotfiles_dir/scripts/pi-workspace.py"', 'helper="$HOME/.local/share/pi/control/pi-workspace.py"')
    path.write_text(text)
    path.chmod(0o755)
PY
echo "Installed Pi ${PI_VERSION} configuration and deterministic launchers"

if [ "${PI_HARNESS_ONLY:-0}" = "1" ]; then
    echo "Pi harness installation complete (host-only scope)"
    exit 0
fi

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

# Install shared workflow files. Legacy orchestrators remain dormant.
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
