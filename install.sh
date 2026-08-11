#!/usr/bin/env bash

if (( BASH_VERSINFO[0] < 4 )); then
    for bash_candidate in /opt/homebrew/bin/bash /usr/local/bin/bash; do
        if [[ -x "$bash_candidate" ]] && "$bash_candidate" -c '(( BASH_VERSINFO[0] >= 4 ))' 2>/dev/null; then
            exec "$bash_candidate" "$0" "$@"
        fi
    done
    echo "This installer requires Bash 4 or newer (macOS: install Homebrew bash and put it first on PATH)." >&2
    exit 1
fi

set -eEuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DOTFILES_MACHINE_ID="${DOTFILES_MACHINE:-}"
if [ -z "$DOTFILES_MACHINE_ID" ]; then
    case "$(uname -s):$(uname -m)" in
        Darwin:arm64|Darwin:aarch64) DOTFILES_MACHINE_ID=macos-arm64 ;;
        Linux:x86_64|Linux:amd64) DOTFILES_MACHINE_ID=linux-x86_64 ;;
        *) DOTFILES_MACHINE_ID="" ;;
    esac
fi
MACHINE_PROFILE=""
MACHINE_CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/dotfiles"
MACHINE_CONFIG_PATH="$MACHINE_CONFIG_DIR/machine.env"
if [ -n "$DOTFILES_MACHINE_ID" ]; then
    [[ "$DOTFILES_MACHINE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
        echo "Unsupported machine profile name: $DOTFILES_MACHINE_ID" >&2
        exit 1
    }
    MACHINE_PROFILE="$SCRIPT_DIR/machines/$DOTFILES_MACHINE_ID.env"
    [ -f "$MACHINE_PROFILE" ] && [ ! -L "$MACHINE_PROFILE" ] || {
        echo "Machine profile is missing or unsafe: $MACHINE_PROFILE" >&2
        exit 1
    }
fi

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
elif [ -L "$PI_CORE_DIR" ] ||
     ! python3 - "$PI_CORE_DIR" <<'PY'
from pathlib import Path
import os
import sys

root = Path(sys.argv[1]).resolve(strict=True)
uid = os.getuid()
for entry in (root, *root.rglob("*")):
    try:
        if entry.is_symlink():
            entry.resolve(strict=True).relative_to(root)
            continue
        info = entry.stat()
    except (OSError, ValueError):
        raise SystemExit(1)
    if info.st_mode & 0o022 or info.st_uid not in {uid, 0}:
        raise SystemExit(1)
raise SystemExit(0)
PY
then
    echo "Existing Pi core has unsafe ownership or writable modes, or escaping symlinks; staging a clean replacement" >&2
    CORE_REINSTALL=1
elif ! PI_CORE_DIR="$PI_CORE_DIR" "$SCRIPT_DIR/scripts/pi-patch-core" --check; then
    echo "Existing Pi core does not include the pinned-editor patch; staging a clean replacement" >&2
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
    PI_CORE_DIR="$CORE_STAGING" "$SCRIPT_DIR/scripts/pi-patch-core"
    chmod -R go-w "$CORE_STAGING"
fi

mkdir -p "$PI_CONFIG_DIR"
STAGING_DIR=$(mktemp -d "$PI_CONFIG_DIR/.install.XXXXXX")
mkdir -p "$STAGING_DIR/npm" "$STAGING_DIR/packages" "$STAGING_DIR/control"
# Keep reviewed first-party packages in a separate source tree. npm installs
# regular runtime copies so their dependencies resolve through npm/node_modules.
for package in pi-sandbox-control pi-subagents-control; do
    source_package="$SCRIPT_DIR/pi/packages/$package"
    [ -d "$source_package" ] && [ ! -L "$source_package" ] || {
        echo "First-party npm package is missing or unsafe: $source_package" >&2
        exit 1
    }
    cp -a -- "$source_package" "$STAGING_DIR/packages/"
done
python3 - "$STAGING_DIR" "$STAGING_DIR/packages/pi-sandbox-control" "$STAGING_DIR/packages/pi-subagents-control" <<'PY'
from pathlib import Path
import os
import stat
import sys

root = Path(sys.argv[1]).resolve(strict=True)
for raw in sys.argv[2:]:
    package = Path(raw)
    if package.is_symlink() or not package.is_dir():
        raise SystemExit(f"staged first-party package is not a regular directory: {package}")
    try:
        package.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"staged first-party package escapes staging root: {package}") from exc
    for entry in (package, *package.rglob("*")):
        try:
            entry.absolute().relative_to(root)
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                entry.resolve(strict=True).relative_to(root)
            elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise SystemExit(f"special file in staged first-party package: {entry}")
        except (OSError, ValueError) as exc:
            raise SystemExit(f"unsafe staged first-party package entry: {entry}") from exc
PY
cp "$SCRIPT_DIR/pi/npm/package.json" "$SCRIPT_DIR/pi/npm/package-lock.json" "$SCRIPT_DIR/pi/npm/.npmrc" "$STAGING_DIR/npm/"
npm ci --prefix "$STAGING_DIR/npm" --install-links --legacy-peer-deps --no-audit --no-fund
python3 - "$STAGING_DIR" "$STAGING_DIR/npm/node_modules/pi-sandbox-control" "$STAGING_DIR/packages/pi-sandbox-control" "$STAGING_DIR/npm/node_modules/pi-subagents" "$STAGING_DIR/packages/pi-subagents-control" <<'PY'
import json
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve(strict=True)
runtime_root = (root / "npm" / "node_modules").resolve(strict=True)
for runtime_raw, source_raw in zip(sys.argv[2::2], sys.argv[3::2]):
    dependency = Path(runtime_raw)
    source = Path(source_raw)
    if dependency.is_symlink() or not dependency.is_dir():
        raise SystemExit(f"npm local dependency is not a regular directory: {dependency}")
    try:
        dependency.resolve(strict=True).relative_to(runtime_root)
    except OSError as exc:
        raise SystemExit(f"npm local dependency is broken: {dependency}") from exc
    except ValueError as exc:
        raise SystemExit(f"npm local dependency escapes npm tree: {dependency}") from exc
    source_package = json.loads((source / "package.json").read_text(encoding="utf-8"))
    runtime_package = json.loads((dependency / "package.json").read_text(encoding="utf-8"))
    if (runtime_package.get("name"), runtime_package.get("version")) != (
        source_package.get("name"),
        source_package.get("version"),
    ):
        raise SystemExit(f"npm local dependency does not match reviewed source: {dependency}")
PY
# Pi extensions declare the SDK as peer dependencies; expose the exact SDK
# packages owned by the dedicated core to the isolated extension tree. Without
# this, jiti resolves an extension from ~/.pi/agent/npm but cannot see the
# core's nested peer packages and fails closed at runtime.
PI_CORE_PARENT=$(CDPATH= cd -- "$(dirname "$PI_CORE_DIR")" && pwd -P)
PI_CORE_REAL="$PI_CORE_PARENT/$(basename "$PI_CORE_DIR")"
if [ -n "$CORE_STAGING" ]; then
    PI_SDK_SOURCE_CORE=$(realpath "$CORE_STAGING")
else
    PI_SDK_SOURCE_CORE=$(realpath "$PI_CORE_REAL")
fi
locate_sdk_peer() {
    local root="$1" peer="$2" candidate
    if [ "$peer" = pi-coding-agent ]; then
        candidate="$root/node_modules/@earendil-works/pi-coding-agent"
        [ -d "$candidate" ] && { printf '%s\n' "$candidate"; return 0; }
        return 1
    fi
    for candidate in \
        "$root/node_modules/@earendil-works/$peer" \
        "$root/node_modules/@earendil-works/pi-coding-agent/node_modules/@earendil-works/$peer"; do
        [ -d "$candidate" ] && { printf '%s\n' "$candidate"; return 0; }
    done
    return 1
}

declare -A PI_SDK_LINKS=()
for peer in pi-agent-core pi-ai pi-coding-agent pi-tui; do
    peer_path=$(locate_sdk_peer "$PI_SDK_SOURCE_CORE" "$peer") || {
        echo "Pi SDK peer is missing from the dedicated core: $peer" >&2
        exit 1
    }
    peer_real=$(realpath "$peer_path")
    case "$peer_real" in "$PI_SDK_SOURCE_CORE"|"$PI_SDK_SOURCE_CORE"/*) ;; *) echo "Pi SDK peer escapes the dedicated core: $peer_path" >&2; exit 1 ;; esac
    peer_version=$(node -p 'require(process.argv[1]).version' "$peer_path/package.json")
    [ "$peer_version" = "$PI_VERSION" ] || { echo "Pi SDK peer version mismatch: $peer_path ($peer_version)" >&2; exit 1; }
    relative_peer=${peer_real#"$PI_SDK_SOURCE_CORE"/}
    [ "$relative_peer" != "$peer_real" ] || { echo "Pi SDK peer has no core-relative path: $peer_path" >&2; exit 1; }
    PI_SDK_LINKS[$peer]="$PI_CORE_REAL/$relative_peer"
done
mkdir -p "$STAGING_DIR/npm/node_modules/@earendil-works"
for peer in pi-agent-core pi-ai pi-coding-agent pi-tui; do
    ln -s "${PI_SDK_LINKS[$peer]}" "$STAGING_DIR/npm/node_modules/@earendil-works/$peer"
done
install -m 600 "$SCRIPT_DIR/pi/pi-image-tools.json" "$STAGING_DIR/npm/node_modules/pi-image-tools/config.json"
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
python3 - "$POLICY_PATH" "$POLICY_STAGING" "$SCRIPT_DIR" "$HOME/.local/share/pi/worktrees" "$MACHINE_PROFILE" <<'PY'
import json
import os
from pathlib import Path
import sys

source, destination, repository, worktree_root = map(Path, sys.argv[1:5])
machine_profile = Path(sys.argv[5]) if sys.argv[5] else None

def machine_values(path):
    values = {}
    if not path:
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SystemExit(f"invalid machine profile line: {line}")
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in {"PI_TRUSTED_PROJECT_ROOTS"}:
            continue
        value = value.strip().strip('"').strip("'").replace("${HOME}", str(Path.home()))
        values[key] = [item for item in value.split(os.pathsep) if item]
    return values

policy = json.loads(source.read_text(encoding="utf-8"))
if not isinstance(policy, dict):
    raise SystemExit("repository policy must be a JSON object")
policy.setdefault("version", 1)
policy.setdefault("defaultMode", "isolated")
policy.setdefault("trustedRoots", [])
policy.setdefault("isolatedRoots", [])
policy.setdefault("controlPlaneRepositories", [])
policy.setdefault("protectedBranches", ["main", "master"])
policy.setdefault("worktreeRoot", str(worktree_root.expanduser()))
if policy["version"] != 1 or policy["defaultMode"] != "isolated":
    raise SystemExit("repository policy must remain version 1 with defaultMode isolated")

def canonical(value):
    expanded = Path(os.path.expanduser(value))
    if not expanded.is_absolute():
        raise SystemExit(f"policy path is not absolute after expansion: {value}")
    return str(expanded.resolve(strict=True))

repository = str(repository.resolve(strict=True))
trusted = [canonical(item) for item in policy["trustedRoots"]]
for item in machine_values(machine_profile).get("PI_TRUSTED_PROJECT_ROOTS", []):
    resolved = canonical(item)
    if resolved not in trusted:
        trusted.append(resolved)
policy["trustedRoots"] = trusted
policy["isolatedRoots"] = [canonical(item) for item in policy["isolatedRoots"]]
policy["controlPlaneRepositories"] = [canonical(item) for item in policy["controlPlaneRepositories"]]
policy["controlPlaneRepositories"] = list(dict.fromkeys([repository, *policy["controlPlaneRepositories"]]))
policy["worktreeRoot"] = str(Path(os.path.expanduser(policy["worktreeRoot"])).resolve())
Path(destination).write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
Path(destination).chmod(0o600)
PY
    echo "Staging the existing repository policy with machine trusted roots: $POLICY_PATH"
else
    python3 - "$SCRIPT_DIR/pi/repository-policy.json" "$POLICY_STAGING" "$SCRIPT_DIR" "$HOME/.local/share/pi/worktrees" "$MACHINE_PROFILE" <<'PY'
import json
import os
from pathlib import Path
import sys

source, destination, repository, worktree_root = map(Path, sys.argv[1:5])
machine_profile = Path(sys.argv[5]) if sys.argv[5] else None
policy = json.loads(source.read_text(encoding="utf-8"))
repository = str(repository.resolve())
policy["trustedRoots"] = [repository]
if machine_profile:
    for line in machine_profile.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("PI_TRUSTED_PROJECT_ROOTS="):
            for item in line.split("=", 1)[1].strip().strip('"').strip("'").replace("${HOME}", str(Path.home())).split(os.pathsep):
                if item:
                    policy["trustedRoots"].append(str(Path(os.path.expanduser(item)).resolve()))
policy["trustedRoots"] = list(dict.fromkeys(policy["trustedRoots"]))
policy["controlPlaneRepositories"] = [repository]
policy["worktreeRoot"] = str(worktree_root.expanduser())
Path(destination).write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
PY
    chmod 600 "$POLICY_STAGING"
fi
"$SCRIPT_DIR/scripts/pi-workspace.py" validate-policy "$POLICY_STAGING" >/dev/null

# Prepare every host control-plane file before changing the active generation.
for config in settings.json keybindings.json pi-goal.json pi-chrome-devtools.json pi-plan-mode.json pi-statusline.json pr-review.json; do
    install -m 600 "$SCRIPT_DIR/pi/$config" "$STAGING_DIR/control/$config"
done
# Preserve user keybindings while making the image-paste ownership decision
# deterministic. The Pi built-in binding is the only key we intentionally
# override; extension-specific shortcuts remain user-editable.
python3 - "$STAGING_DIR/control/keybindings.json" "$PI_CONFIG_DIR/keybindings.json" <<'PY'
import json
import os
from pathlib import Path
import stat
import sys
staged, existing = map(Path, sys.argv[1:])
value = json.loads(staged.read_text(encoding="utf-8"))
if existing.exists() or existing.is_symlink():
    info = existing.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise SystemExit(f"unsafe existing keybindings: {existing}")
    current = json.loads(existing.read_text(encoding="utf-8"))
    if not isinstance(current, dict):
        raise SystemExit(f"existing keybindings must be an object: {existing}")
    current.update(value)
    value = current
staged.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
staged.chmod(0o600)
PY
if [ -n "$MACHINE_PROFILE" ]; then
    install -m 600 "$MACHINE_PROFILE" "$STAGING_DIR/control/machine.env"
fi
for tree in extensions agents prompts themes; do cp -a "$SCRIPT_DIR/pi/$tree" "$STAGING_DIR/control/$tree"; done
# Install the controller as a self-contained Python package under the stable
# activated helper root. The bin launcher selects this artifact when installed
# and only uses scripts.pi_control for an explicit repository checkout.
cp -a "$SCRIPT_DIR/scripts/pi_control" "$STAGING_DIR/control/pi_control"
install -m 755 "$SCRIPT_DIR/bin/pi-control" "$STAGING_DIR/control/pi-control"
# npm and copied extension trees can inherit group-writable modes from the
# staging filesystem; never activate a writable installed runtime tree.
chmod -R go-w "$STAGING_DIR"
python3 - "$STAGING_DIR/control/agents" "$PI_CONFIG_DIR" <<'PY'
from pathlib import Path
import sys

agents_dir = Path(sys.argv[1])
agent_dir = sys.argv[2]
for path in agents_dir.glob("*.md"):
    path.write_text(path.read_text(encoding="utf-8").replace("__PI_AGENT_DIR__", agent_dir), encoding="utf-8")
PY
install -m 600 "$SCRIPT_DIR/agent/AGENTS.md" "$STAGING_DIR/control/AGENTS.md"
install -m 755 "$SCRIPT_DIR/scripts/pi-workspace.py" "$STAGING_DIR/control/pi-workspace.py"
install -m 755 "$SCRIPT_DIR/scripts/pi-surface.py" "$STAGING_DIR/control/pi-surface.py"
install -m 755 "$SCRIPT_DIR/scripts/pi-runtime.py" "$STAGING_DIR/control/pi-runtime.py"
install -m 755 "$SCRIPT_DIR/scripts/pi-sandbox-gc.py" "$STAGING_DIR/control/pi-sandbox-gc.py"
install -m 755 "$SCRIPT_DIR/scripts/pi-root-session.py" "$STAGING_DIR/control/pi-root-session.py"
install -m 755 "$SCRIPT_DIR/scripts/pi-harness-feedback.py" "$STAGING_DIR/control/pi-harness-feedback.py"
install -m 755 "$SCRIPT_DIR/scripts/pi-personal-herdr.py" "$STAGING_DIR/control/pi-personal-herdr.py"
cp -a "$SCRIPT_DIR/skills/project-status" "$STAGING_DIR/control/project-status-skill"
for launcher in pi pi-start pi-help-custom pi-host pidev pi-tmux-session pisec pi-personal pi-personal-herdr pi-root-session pi-harness-feedback pi-sandbox-gc pi-restart; do
    install -m 755 "$SCRIPT_DIR/bin/$launcher" "$STAGING_DIR/control/$launcher"
done
python3 - "$STAGING_DIR/control/pi" "$STAGING_DIR/control/pi-host" <<'PY'
from pathlib import Path
import sys
for name in sys.argv[1:]:
    path = Path(name)
    text = path.read_text().replace(
        'helper="$dotfiles_dir/scripts/pi-workspace.py"',
        'helper="$HOME/.local/share/pi/control/pi-workspace.py"',
    ).replace(
        'root_helper="$dotfiles_dir/scripts/pi-root-session.py"',
        'root_helper="$HOME/.local/share/pi/control/pi-root-session.py"',
    )
    path.write_text(text)
    path.chmod(0o755)
PY
# Record the complete disposable generation only after every staged launcher
# and path rewrite is final. The manifest digest is the staged build identity;
# it intentionally excludes the random staging path and is never accepted as
# proof of live activation by itself.
STAGED_IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$DOCKER_STAGING_IMAGE")
PI_STAGE_REPOSITORY="$SCRIPT_DIR" PI_STAGE_IMAGE_ID="$STAGED_IMAGE_ID" python3 - "$STAGING_DIR" "$STAGING_DIR/control/build-manifest.json" <<'PY'
import os
import sys
from pathlib import Path
from scripts.pi_control.staged_build import create_build_manifest, load_build_manifest, write_build_manifest

staging_root = Path(sys.argv[1])
destination = Path(sys.argv[2])
manifest = create_build_manifest(
    staging_root,
    repository=os.environ["PI_STAGE_REPOSITORY"],
    manifest_path=destination,
    require_repository_metadata=True,
    metadata={
        "piVersion": Path(os.environ["PI_STAGE_REPOSITORY"]).joinpath("pi/PI_VERSION").read_text(encoding="utf-8").strip(),
        "imageId": os.environ["PI_STAGE_IMAGE_ID"],
        "installer": "install.sh",
    },
)
# Verify the complete staged file/symlink set before writing the envelope, then
# load the serialized envelope and verify again with only the envelope itself
# excluded. This rejects omissions, extras, traversal, and special files.
manifest.verify_files(staging_root)
saved = write_build_manifest(manifest, destination)
loaded = load_build_manifest(destination)
loaded.verify_files(staging_root, exclude_paths=[destination])
print(saved.build_id)
PY
if [ -n "${PI_EXPECTED_BUILD_MANIFEST:-}" ]; then
    python3 - "$STAGING_DIR/control/build-manifest.json" "$PI_EXPECTED_BUILD_MANIFEST" <<'PY'
import sys
from scripts.pi_control.staged_build import load_build_manifest

actual = load_build_manifest(sys.argv[1])
expected = load_build_manifest(sys.argv[2])
if actual.digest != expected.digest or actual.build_id != expected.build_id or actual.payload != expected.payload:
    raise SystemExit("staged build manifest does not match PI_EXPECTED_BUILD_MANIFEST")
PY
fi

INSTALL_TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ).$$
OLD_IMAGE_ID=$(docker image inspect --format '{{.Id}}' "$FINAL_IMAGE" 2>/dev/null || true)
PENDING_SIGNAL=0
honor_pending_signal() {
    local pending=$PENDING_SIGNAL
    PENDING_SIGNAL=0
    [ "$pending" = 0 ] || exit "$pending"
}
activate_path() {
    local source=$1 target=$2 rollback_dir backup=""
    rollback_dir=${3:-$(dirname "$target")}
    mkdir -p "$(dirname "$target")"
    if [ -e "$target" ] || [ -L "$target" ]; then
        mkdir -p "$rollback_dir"
        backup="$rollback_dir/$(basename "$target").rollback.$INSTALL_TIMESTAMP"
    fi
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
activate_path "$STAGING_DIR/control/build-manifest.json" "$PI_CONFIG_DIR/control-build-manifest.json"
# Stop package reconciliation before replacing the shared npm tree. The new
# local package sources are valid against both the previous and next generation.
activate_path "$STAGING_DIR/control/settings.json" "$PI_CONFIG_DIR/settings.json"
activate_path "$STAGING_DIR/packages" "$PI_CONFIG_DIR/packages"
activate_path "$STAGING_DIR/npm" "$PI_CONFIG_DIR/npm"
for config in keybindings.json pi-goal.json pi-chrome-devtools.json pi-plan-mode.json pi-statusline.json pr-review.json AGENTS.md; do
    activate_path "$STAGING_DIR/control/$config" "$PI_CONFIG_DIR/$config"
done
for tree in extensions agents prompts themes; do activate_path "$STAGING_DIR/control/$tree" "$PI_CONFIG_DIR/$tree"; done
activate_path "$STAGING_DIR/control/pi-workspace.py" "$HOME/.local/share/pi/control/pi-workspace.py"
activate_path "$STAGING_DIR/control/pi-surface.py" "$HOME/.local/share/pi/control/pi-surface.py"
activate_path "$STAGING_DIR/control/pi-runtime.py" "$HOME/.local/share/pi/control/pi-runtime.py"
activate_path "$STAGING_DIR/control/pi-sandbox-gc.py" "$HOME/.local/share/pi/control/pi-sandbox-gc.py"
activate_path "$STAGING_DIR/control/pi-root-session.py" "$HOME/.local/share/pi/control/pi-root-session.py"
activate_path "$STAGING_DIR/control/pi-harness-feedback.py" "$HOME/.local/share/pi/control/pi-harness-feedback.py"
activate_path "$STAGING_DIR/control/pi_control" "$HOME/.local/share/pi/control/pi_control"
activate_path "$STAGING_DIR/control/pi-personal-herdr.py" "$HOME/.local/share/pi/control/pi-personal-herdr.py"
if [ -n "$MACHINE_PROFILE" ]; then
    mkdir -p -m 700 "$MACHINE_CONFIG_DIR"
    chmod 700 "$MACHINE_CONFIG_DIR"
    activate_path "$STAGING_DIR/control/machine.env" "$MACHINE_CONFIG_PATH"
fi
skill_rollback_dir="${XDG_STATE_HOME:-$HOME/.local/state}/pi/rollback/skills"
activate_path "$STAGING_DIR/control/project-status-skill" "$PI_CONFIG_DIR/skills/project-status" "$skill_rollback_dir"
for launcher in pi pi-start pi-help-custom pi-host pidev pi-tmux-session pisec pi-personal pi-personal-herdr pi-root-session pi-harness-feedback pi-sandbox-gc pi-restart; do
    activate_path "$STAGING_DIR/control/$launcher" "$HOME/.local/bin/$launcher"
done
activate_path "$STAGING_DIR/control/pi-control" "$HOME/.local/bin/pi-control"

# Validate the activated generation before the commit point. The projection is
# made from the actual active files (hard links where possible), so manifest
# verification covers every split activation target rather than only IDs.
python3 - "$PI_CONFIG_DIR/control-build-manifest.json" "$PI_CONFIG_DIR" "$HOME" "$MACHINE_CONFIG_PATH" "$STAGING_DIR" <<'PY'
from pathlib import Path
import json
import os
import shutil
import stat
import sys
import tempfile

from scripts.pi_control.staged_build import load_build_manifest

manifest_path, pi_config, home, machine_config, staging = map(Path, sys.argv[1:])
manifest = load_build_manifest(manifest_path)
pi_config = pi_config.resolve(strict=True)
home = home.resolve(strict=True)
staging = staging.resolve(strict=True)
control_root = home / ".local" / "share" / "pi" / "control"
local_bin = home / ".local" / "bin"
policy_path = home / ".config" / "pi" / "repository-policy.json"
machine_config = Path(machine_config)

package_root = pi_config / "packages"
if package_root.is_symlink() or not package_root.is_dir() or control_root.is_symlink() or not control_root.is_dir():
    raise SystemExit("activated package or controller root is missing or symlinked")
allowed_packages = {
    (package_root / "pi-sandbox-control").resolve(strict=True),
    (package_root / "pi-subagents-control").resolve(strict=True),
}
for package in sorted(allowed_packages):
    if package.is_symlink() or not package.is_dir():
        raise SystemExit(f"activated first-party package is not a regular directory: {package}")
    try:
        package.relative_to(package_root.resolve(strict=True))
    except ValueError as exc:
        raise SystemExit(f"activated first-party package escapes its root: {package}") from exc
    for entry in (package, *package.rglob("*")):
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            try:
                entry.resolve(strict=True).relative_to(package_root.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise SystemExit(f"activated first-party package has an unsafe symlink: {entry}") from exc
        elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise SystemExit(f"special file in activated first-party package: {entry}")

runtime_root = (pi_config / "npm" / "node_modules").resolve(strict=True)
for name, source in {
    "pi-sandbox-control": package_root / "pi-sandbox-control",
    "pi-subagents": package_root / "pi-subagents-control",
}.items():
    dependency = runtime_root / name
    if dependency.is_symlink() or not dependency.is_dir():
        raise SystemExit(f"activated npm dependency is not a regular directory: {dependency}")
    try:
        dependency.resolve(strict=True).relative_to(runtime_root)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"activated npm dependency has invalid placement: {dependency}") from exc
    source_package = json.loads((source / "package.json").read_text(encoding="utf-8"))
    runtime_package = json.loads((dependency / "package.json").read_text(encoding="utf-8"))
    if (runtime_package.get("name"), runtime_package.get("version")) != (
        source_package.get("name"),
        source_package.get("version"),
    ):
        raise SystemExit(f"activated npm dependency does not match reviewed source: {dependency}")

configs = {"settings.json", "keybindings.json", "pi-goal.json", "pi-chrome-devtools.json", "pi-plan-mode.json", "pi-statusline.json", "pr-review.json", "AGENTS.md"}
helpers = {"pi-workspace.py", "pi-surface.py", "pi-runtime.py", "pi-sandbox-gc.py", "pi-root-session.py", "pi-harness-feedback.py", "pi-personal-herdr.py"}
launchers = {"pi", "pi-start", "pi-help-custom", "pi-host", "pidev", "pi-tmux-session", "pisec", "pi-personal", "pi-personal-herdr", "pi-root-session", "pi-harness-feedback", "pi-sandbox-gc", "pi-restart", "pi-control"}

def active_path(relative: str) -> Path:
    if relative.startswith("npm/"):
        return pi_config / relative
    if relative.startswith("packages/"):
        return pi_config / relative
    if not relative.startswith("control/"):
        raise SystemExit(f"manifest path is outside known activation surfaces: {relative}")
    suffix = relative.removeprefix("control/")
    if suffix == "repository-policy.json":
        return policy_path
    if suffix == "machine.env":
        return machine_config
    if suffix in configs:
        return pi_config / suffix
    if suffix.startswith(("extensions/", "agents/", "prompts/", "themes/")):
        return pi_config / suffix
    if suffix == "project-status-skill" or suffix.startswith("project-status-skill/"):
        return pi_config / "skills" / "project-status" / suffix.removeprefix("project-status-skill/")
    if suffix == "pi_control" or suffix.startswith("pi_control/") or suffix in helpers:
        return control_root / suffix
    if suffix in launchers:
        return local_bin / suffix
    raise SystemExit(f"manifest path has no activation target: {relative}")

projection = Path(tempfile.mkdtemp(prefix=".post-verify.", dir=staging))
try:
    for entry in manifest.payload["files"]:
        relative = entry["path"]
        target = active_path(relative)
        if not target.exists() and not target.is_symlink():
            raise SystemExit(f"activated manifest path is missing: {target}")
        projected = projection / relative
        projected.parent.mkdir(parents=True, exist_ok=True)
        if entry["kind"] == "file":
            if target.is_symlink() or not target.is_file():
                raise SystemExit(f"activated manifest file is not regular: {target}")
            try:
                os.link(target, projected)
            except OSError:
                shutil.copyfile(target, projected)
                os.chmod(projected, stat.S_IMODE(target.stat().st_mode))
        elif entry["kind"] == "symlink":
            if not target.is_symlink():
                raise SystemExit(f"activated manifest symlink is not a symlink: {target}")
            os.symlink(os.readlink(target), projected)
        else:
            raise SystemExit(f"unexpected manifest entry kind: {entry['kind']}")
    manifest.verify_files(projection)
finally:
    shutil.rmtree(projection, ignore_errors=True)
PY

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
[ -z "$MACHINE_PROFILE" ] || echo "Installed machine profile: $DOTFILES_MACHINE_ID"

if [ "${PI_HARNESS_ONLY:-0}" = "1" ]; then
    echo "Pi harness installation complete (host-only scope)"
    exit 0
fi

echo "Installing tmux config..."

# Link tmux and gitmux configs into the repository.
backup_and_link "$SCRIPT_DIR/tmux.conf" "$HOME/.tmux.conf"
echo "Linked tmux.conf to ~/.tmux.conf"

backup_and_link "$SCRIPT_DIR/scripts/tmux-copy.sh" "$HOME/.local/bin/tmux-copy"
backup_and_link "$SCRIPT_DIR/scripts/tmux-voxtype-status.sh" "$HOME/.local/bin/tmux-voxtype-status"

if [ -f gitmux.conf ]; then
    backup_and_link "$SCRIPT_DIR/gitmux.conf" "$HOME/.gitmux.conf"
    echo "Linked gitmux.conf to ~/.gitmux.conf"
fi

# Install the Voxtype microphone-signal watchdog when Voxtype is present.
if command -v voxtype &> /dev/null && command -v systemctl &> /dev/null && [ -f systemd/user/voxtype-mic-watchdog.service ]; then
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
    case "$(uname -s)" in
        Darwin)
            if command -v brew &> /dev/null; then
                brew install gitmux
            else
                case "$(uname -m)" in
                    arm64|aarch64) gitmux_arch=arm64 ;;
                    x86_64|amd64) gitmux_arch=amd64 ;;
                    *) echo "Unsupported macOS architecture for gitmux: $(uname -m)" >&2; exit 1 ;;
                esac
                curl -fsSL "https://github.com/arl/gitmux/releases/download/v0.11.5/gitmux_v0.11.5_macOS_${gitmux_arch}.tar.gz" | tar xz -C ~/.local/bin
            fi
            ;;
        Linux)
            case "$(uname -m)" in
                arm64|aarch64) gitmux_arch=arm64 ;;
                x86_64|amd64) gitmux_arch=amd64 ;;
                i386|i686) gitmux_arch=386 ;;
                *) echo "Unsupported Linux architecture for gitmux: $(uname -m)" >&2; exit 1 ;;
            esac
            curl -fsSL "https://github.com/arl/gitmux/releases/download/v0.11.5/gitmux_v0.11.5_linux_${gitmux_arch}.tar.gz" | tar xz -C ~/.local/bin
            ;;
        *) echo "Unsupported operating system for gitmux: $(uname -s)" >&2; exit 1 ;;
    esac
    shell_name=$(basename "${SHELL:-bash}")
    case "$shell_name" in
        zsh) startup_file="$HOME/.zshrc" ;;
        *) startup_file="$HOME/.bashrc" ;;
    esac
    if ! grep -Fq '.local/bin' "$startup_file" 2>/dev/null; then
        printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$startup_file"
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

# Add the direnv hook to the user's active shell startup file if not present.
shell_name=$(basename "${SHELL:-bash}")
case "$shell_name" in
    zsh) startup_file="$HOME/.zshrc"; direnv_hook='eval "$(direnv hook zsh)"' ;;
    *) startup_file="$HOME/.bashrc"; direnv_hook='eval "$(direnv hook bash)"' ;;
esac
if ! grep -Fq "direnv hook $shell_name" "$startup_file" 2>/dev/null; then
    printf '\n# direnv - auto-activate venvs when entering directories\n%s\n' "$direnv_hook" >> "$startup_file"
    echo "Added direnv hook to $startup_file"
fi

# Do not live-reload tmux while managed panes are running. The config starts
# restore/repair hooks, so reloading here can race the explicit activation
# restart and duplicate panes. pi-restart applies the complete generation.
if tmux info &> /dev/null; then
    echo "tmux is running; use pi-restart to activate the new generation"
fi

echo ""
echo "Done! Open a new terminal or source your shell startup file."
echo "Next: review 'pi-root-session migrate --dry-run', then run 'pi-restart' to start the managed grid."
echo "If launchers are not on PATH yet: '$HOME/.local/bin/pi-root-session' and '$HOME/.local/bin/pi-restart'"
