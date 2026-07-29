#!/bin/bash
set -eEuo pipefail

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

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "Docker is unavailable; refusing partial Pi harness activation" >&2
    exit 1
fi

BASELINE_OID=549e57cb4951253b6f88c82de79c06af4035427c
ROLLBACK_REF=refs/heads/rollback/pi-harness-pre-trusted-live-20260729
current_rollback=$(git -C "$SCRIPT_DIR" rev-parse --verify --quiet "$ROLLBACK_REF" || true)
if [ -n "$current_rollback" ] && [ "$current_rollback" != "$BASELINE_OID" ]; then
    echo "Refusing to replace mismatched rollback ref: $ROLLBACK_REF" >&2
    exit 1
fi
git -C "$SCRIPT_DIR" cat-file -e "$BASELINE_OID^{commit}"
[ -n "$current_rollback" ] || git -C "$SCRIPT_DIR" update-ref "$ROLLBACK_REF" "$BASELINE_OID"

PI_CORE_DIR="${PI_CORE_DIR:-$HOME/.local/share/pi/core}"
PI_CORE_BIN="$PI_CORE_DIR/node_modules/.bin/pi"
CORE_STAGING=""
STAGING_DIR=""
DOCKER_STAGING_IMAGE=""
FINAL_IMAGE="pi-tool-sandbox:node22-bookworm-20260728"
OLD_IMAGE_ID=""
IMAGE_ACTIVATED=0
ACTIVATION_STARTED=0
ACTIVATION_COMMITTED=0
ACTIVATED_TARGETS=()
ACTIVATED_BACKUPS=()
finish_install() {
    local status=$? rollback_failed=0
    trap - EXIT INT TERM
    set +e
    if [ "$ACTIVATION_STARTED" = 1 ] && [ "$ACTIVATION_COMMITTED" = 0 ]; then
        for ((index=${#ACTIVATED_TARGETS[@]}-1; index>=0; index--)); do
            rm -rf -- "${ACTIVATED_TARGETS[index]}" || rollback_failed=1
            if [ -n "${ACTIVATED_BACKUPS[index]}" ]; then
                mv "${ACTIVATED_BACKUPS[index]}" "${ACTIVATED_TARGETS[index]}" || rollback_failed=1
            fi
        done
        if [ "$IMAGE_ACTIVATED" = 1 ]; then
            if [ -n "$OLD_IMAGE_ID" ]; then docker image tag "$OLD_IMAGE_ID" "$FINAL_IMAGE" >/dev/null 2>&1 || rollback_failed=1
            else docker image rm "$FINAL_IMAGE" >/dev/null 2>&1 || rollback_failed=1
            fi
        fi
    fi
    [ -z "$STAGING_DIR" ] || rm -rf "$STAGING_DIR" || rollback_failed=1
    [ -z "$CORE_STAGING" ] || rm -rf "$CORE_STAGING" || rollback_failed=1
    if [ -n "$DOCKER_STAGING_IMAGE" ]; then docker image rm "$DOCKER_STAGING_IMAGE" >/dev/null 2>&1 || true; fi
    if [ "$rollback_failed" = 1 ]; then
        echo "Pi harness rollback encountered errors; inspect recorded .rollback paths before retrying" >&2
        status=1
    fi
    exit "$status"
}
trap finish_install EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

CORE_REINSTALL=0
if [ ! -x "$PI_CORE_BIN" ] || [ "$("$PI_CORE_BIN" --version 2>/dev/null || true)" != "$PI_VERSION" ]; then
    CORE_REINSTALL=1
elif [ -L "$PI_CORE_DIR" ] || find "$PI_CORE_DIR" -xdev \( -perm /022 -o \( ! -uid "$(id -u)" ! -uid 0 \) \) -print -quit | grep -q .; then
    echo "Existing Pi core has unsafe ownership or writable modes; staging a clean replacement" >&2
    CORE_REINSTALL=1
fi
if [ "$CORE_REINSTALL" = 1 ]; then
    echo "Staging dedicated Pi CLI ${PI_VERSION}..."
    mkdir -p "$(dirname "$PI_CORE_DIR")"
    CORE_STAGING=$(mktemp -d "$(dirname "$PI_CORE_DIR")/.core-install.XXXXXX")
    npm install --prefix "$CORE_STAGING" --no-save --no-package-lock --no-audit --no-fund \
        "@earendil-works/pi-coding-agent@${PI_VERSION}"
    [ "$("$CORE_STAGING/node_modules/.bin/pi" --version 2>/dev/null || true)" = "$PI_VERSION" ] || {
        echo "Dedicated Pi CLI version verification failed" >&2
        exit 1
    }
    chmod -R go-w "$CORE_STAGING"
fi

mkdir -p "$PI_CONFIG_DIR"
STAGING_DIR=$(mktemp -d "$PI_CONFIG_DIR/.install.XXXXXX")
mkdir -p "$STAGING_DIR/npm" "$STAGING_DIR/control"
cp "$SCRIPT_DIR/pi/npm/package.json" "$SCRIPT_DIR/pi/npm/package-lock.json" "$STAGING_DIR/npm/"
npm ci --prefix "$STAGING_DIR/npm" --legacy-peer-deps --no-audit --no-fund
PI_CODING_AGENT_DIR="$STAGING_DIR" "$SCRIPT_DIR/scripts/pi-patch-subagents"

DOCKER_STAGING_IMAGE="pi-tool-sandbox:staging-$$"
docker build --pull=false -t "$DOCKER_STAGING_IMAGE" "$SCRIPT_DIR/pi/sandbox"

POLICY_DIR="$HOME/.config/pi"
POLICY_PATH="$POLICY_DIR/repository-policy.json"
mkdir -p -m 700 "$POLICY_DIR" "$PI_CONFIG_DIR/generated"
POLICY_STAGING="$STAGING_DIR/control/repository-policy.json"
if [ -e "$POLICY_PATH" ] || [ -L "$POLICY_PATH" ]; then
    [ ! -L "$POLICY_PATH" ] && [ -f "$POLICY_PATH" ] && [ -O "$POLICY_PATH" ] || {
        echo "Refusing unsafe existing repository policy: $POLICY_PATH" >&2
        exit 1
    }
    install -m 600 "$POLICY_PATH" "$POLICY_STAGING"
    echo "Staging the existing repository policy unchanged: $POLICY_PATH"
else
    install -m 600 "$SCRIPT_DIR/pi/repository-policy.json" "$POLICY_STAGING"
fi
"$SCRIPT_DIR/scripts/pi-workspace.py" validate-policy "$POLICY_STAGING" >/dev/null

# Prepare every host control-plane file before changing the active generation.
for config in settings.json pi-chrome-devtools.json pi-plan-mode.json pi-statusline.json pr-review.json; do
    install -m 600 "$SCRIPT_DIR/pi/$config" "$STAGING_DIR/control/$config"
done
for tree in extensions agents prompts themes; do cp -a "$SCRIPT_DIR/pi/$tree" "$STAGING_DIR/control/$tree"; done
ln -s "$SCRIPT_DIR/agent/AGENTS.md" "$STAGING_DIR/control/AGENTS.md"
install -m 755 "$SCRIPT_DIR/scripts/pi-workspace.py" "$STAGING_DIR/control/pi-workspace.py"
install -m 755 "$SCRIPT_DIR/bin/pi" "$STAGING_DIR/control/pi"
install -m 755 "$SCRIPT_DIR/bin/pi-host" "$STAGING_DIR/control/pi-host"
python3 - "$STAGING_DIR/control/pi" "$STAGING_DIR/control/pi-host" <<'PY'
from pathlib import Path
import sys
for name in sys.argv[1:]:
    path = Path(name)
    text = path.read_text().replace(
        'helper="$dotfiles_dir/scripts/pi-workspace.py"',
        'helper="$HOME/.local/share/pi/control/pi-workspace.py"',
    )
    path.write_text(text)
    path.chmod(0o755)
PY

INSTALL_TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ).$$
OLD_IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$FINAL_IMAGE" 2>/dev/null || true)
PENDING_SIGNAL=0
honor_pending_signal() {
    local pending=$PENDING_SIGNAL
    PENDING_SIGNAL=0
    [ "$pending" = 0 ] || exit "$pending"
}
activate_path() {
    local source=$1 target=$2 backup=""
    mkdir -p "$(dirname "$target")"
    if [ -e "$target" ] || [ -L "$target" ]; then backup="$target.rollback.$INSTALL_TIMESTAMP"; fi
    # INT/TERM are deferred until mutation and rollback bookkeeping agree.
    if [ -n "$backup" ]; then
        mv "$target" "$backup"
        ACTIVATED_TARGETS+=("$target")
        ACTIVATED_BACKUPS+=("$backup")
        mv "$source" "$target"
    else
        mv "$source" "$target"
        ACTIVATED_TARGETS+=("$target")
        ACTIVATED_BACKUPS+=("")
    fi
    honor_pending_signal
}

# The image tag and all filesystem replacements form one EXIT/signal-rollback transaction.
ACTIVATION_STARTED=1
trap 'PENDING_SIGNAL=130' INT
trap 'PENDING_SIGNAL=143' TERM
docker image tag "$DOCKER_STAGING_IMAGE" "$FINAL_IMAGE"
IMAGE_ACTIVATED=1
honor_pending_signal
[ -z "$CORE_STAGING" ] || { activate_path "$CORE_STAGING" "$PI_CORE_DIR"; CORE_STAGING=""; }
activate_path "$POLICY_STAGING" "$POLICY_PATH"
POLICY_STAGING=""
activate_path "$STAGING_DIR/npm" "$PI_CONFIG_DIR/npm"
for config in settings.json pi-chrome-devtools.json pi-plan-mode.json pi-statusline.json pr-review.json AGENTS.md; do
    activate_path "$STAGING_DIR/control/$config" "$PI_CONFIG_DIR/$config"
done
for tree in extensions agents prompts themes; do activate_path "$STAGING_DIR/control/$tree" "$PI_CONFIG_DIR/$tree"; done
activate_path "$STAGING_DIR/control/pi-workspace.py" "$HOME/.local/share/pi/control/pi-workspace.py"
activate_path "$STAGING_DIR/control/pi" "$HOME/.local/bin/pi"
activate_path "$STAGING_DIR/control/pi-host" "$HOME/.local/bin/pi-host"

# All active paths now form a complete generation. Switch back to immediate
# signal handling before the commit point so no pending signal is discarded.
trap 'exit 130' INT
trap 'exit 143' TERM
honor_pending_signal
ACTIVATION_COMMITTED=1
docker image rm "$DOCKER_STAGING_IMAGE" >/dev/null 2>&1 || true
DOCKER_STAGING_IMAGE=""
rm -rf "$STAGING_DIR"
STAGING_DIR=""
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
