#!/usr/bin/env bash
set -euo pipefail

DOTFILES_DIR="${DOTFILES_DIR:-$HOME/dotfiles}"
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$HOME/go/bin:$HOME/.npm-global/bin:$HOME/.bun/bin:$PATH"
HOST_OS="$(uname -s)"
HOST_ARCH="$(uname -m)"
COLLIE_HOST=""
COLLIE_TRUSTED_USER=""
SKILLS_ONLY=0
RESET_PISEC_STATE=0
COLLIE_REF="v0.28.0"
HERDR_NVIM_SPEC="ChmaraX/herdr-nvim"
HERDR_NVIM_ID="chmarax.herdr-nvim"
HERDR_NVIM_REF="40aadeab3cef3702ef5e05069181c7168084794f"
HERDR_REVIEWR_SPEC="persiyanov/herdr-reviewr"
HERDR_REVIEWR_ID="persiyanov.reviewr"
HERDR_REVIEWR_REF="v0.32.1"
TREEHOUSE_VERSION="v2.1.1"
TREEHOUSE_ARCHIVE="treehouse-v2.1.1-linux-amd64.tar.gz"
TREEHOUSE_URL="https://github.com/kunchenguid/treehouse/releases/download/v2.1.1/treehouse-v2.1.1-linux-amd64.tar.gz"
TREEHOUSE_SHA256="2fe3e01220ae51a967c3e5ba6ccf10ec83bdbae8e420368d194285a8d04c9ef8"
TREEHOUSE_STAGED_BIN=""
PI_STICKY_SPEC="@burneikis/pi-sticky@1.0.0"
if [[ $# -eq 0 ]]; then
  SKILLS_ONLY=1
fi
usage() {
  cat >&2 <<'EOF'
usage: agent-workflow-install.sh --collie-host MAGICDNS_HOST --collie-trusted-user TAILNET_LOGIN [--reset-pisec-state]
       agent-workflow-install.sh --skills-only
EOF
}

die() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}
apply_collie_patch() {
  local source_dir="$1"
  local patch="$DOTFILES_DIR/patches/collie-v0.28-unread-idle.patch"
  [[ -d "$source_dir" && -f "$source_dir/bridge/server.ts" ]] || die "Collie managed checkout is missing"
  [[ -f "$patch" ]] || die "Collie unread-idle patch is missing"
  if git -C "$source_dir" apply --reverse --check "$patch" >/dev/null 2>&1; then
    return
  fi
  git -C "$source_dir" apply --check "$patch" >/dev/null 2>&1 || die "Collie unread-idle patch does not match $COLLIE_REF"
  git -C "$source_dir" apply "$patch" || die "unable to apply Collie unread-idle patch"
}
if [[ -n "${DOTFILES_MACHINE:-}" ]]; then
  MACHINE_ID="$DOTFILES_MACHINE"
else
  case "$HOST_OS:$HOST_ARCH" in
    Linux:x86_64) MACHINE_ID="linux-x86_64" ;;
    Darwin:arm64) MACHINE_ID="macos-arm64" ;;
    *) die "unsupported host platform $HOST_OS/$HOST_ARCH; set DOTFILES_MACHINE to an approved profile" ;;
  esac
fi
MACHINE_PROFILE="$DOTFILES_DIR/machines/$MACHINE_ID.env"
[[ -f "$MACHINE_PROFILE" ]] || die "machine profile is missing: $MACHINE_PROFILE"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --collie-host)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      COLLIE_HOST="$2"
      shift 2
      ;;
    --collie-trusted-user)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      COLLIE_TRUSTED_USER="$2"
      shift 2
      ;;
    --collie-ref)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      COLLIE_REF="$2"
      shift 2
      ;;
    --skills-only)
      SKILLS_ONLY=1
      shift
      ;;
    --reset-pisec-state)
      RESET_PISEC_STATE=1
      shift
      ;;
    -h|--help)
      usage >&1
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ "$SKILLS_ONLY" -eq 0 && ( -z "$COLLIE_HOST" || -z "$COLLIE_TRUSTED_USER" ) ]]; then
  usage
  printf 'error: full installation requires --collie-host and --collie-trusted-user\n' >&2
  exit 2
fi
if [[ "$SKILLS_ONLY" -eq 0 && "$HOST_OS" != "Linux" ]]; then
  die "full Pisec installation currently requires Linux (systemd, Fence Bubblewrap, Landlock, and network namespaces); on macOS use --skills-only and dotfiles-sync-install.sh"
fi
if [[ "$SKILLS_ONLY" -eq 0 && "$HOST_OS:$HOST_ARCH" != "Linux:x86_64" ]]; then
  die "full Pisec installation currently requires Linux x86_64 for pinned Treehouse $TREEHOUSE_VERSION"
fi

if [[ "$SKILLS_ONLY" -eq 0 && ! "$COLLIE_REF" =~ ^v?0\.28\.[0-9]+$ ]]; then
  die "Collie 0.28.x is required; found $COLLIE_REF"
fi
validate_port_setting() {
  local name="$1"
  local value="$2"
  [[ "$value" =~ ^[0-9]+$ ]] && (( value >= 1 && value <= 65535 )) || die "$name must be a TCP port between 1 and 65535"
}
PISEC_AUTH_BROKER_PORT="${PISEC_AUTH_BROKER_PORT:-8765}"
PISEC_AUTH_GATEWAY_PORT="${PISEC_AUTH_GATEWAY_PORT:-4000}"
PISEC_COLLIE_PORT="${PISEC_COLLIE_PORT:-8787}"
validate_port_setting PISEC_AUTH_BROKER_PORT "$PISEC_AUTH_BROKER_PORT"
validate_port_setting PISEC_AUTH_GATEWAY_PORT "$PISEC_AUTH_GATEWAY_PORT"
validate_port_setting PISEC_COLLIE_PORT "$PISEC_COLLIE_PORT"
export PISEC_AUTH_BROKER_PORT PISEC_AUTH_GATEWAY_PORT PISEC_COLLIE_PORT

INSTALL_TRANSACTION_ROOT=""
INSTALL_TRANSACTION_ACTIVE=0
INSTALL_TRANSACTION_COLLIE_STARTED=0
INSTALL_RESET_STATE_ROOT=""
INSTALL_RESET_STATE_ARCHIVE=""
declare -a INSTALL_TRANSACTION_PATHS=()
declare -a INSTALL_TRANSACTION_BACKUPS=()
declare -a INSTALL_TRANSACTION_CREATED_PATHS=()
declare -a INSTALL_TRANSACTION_SERVICES=()
declare -a INSTALL_TRANSACTION_LEGACY_SERVICES=()
declare -a INSTALL_TRANSACTION_SERVICE_WAS_ACTIVE=()
declare -a INSTALL_RESET_STOPPED_SERVICES=()

transaction_record_created_path() {
  local path="$1"
  [[ "$INSTALL_TRANSACTION_ACTIVE" -eq 1 ]] || return 0
  local existing
  for existing in "${INSTALL_TRANSACTION_CREATED_PATHS[@]}"; do
    [[ "$existing" == "$path" ]] && return 0
  done
  INSTALL_TRANSACTION_CREATED_PATHS+=("$path")
}

transaction_capture_path() {
  local path="$1"
  [[ "$INSTALL_TRANSACTION_ACTIVE" -eq 1 ]] || return 0
  local existing
  for existing in "${INSTALL_TRANSACTION_PATHS[@]}"; do
    [[ "$existing" == "$path" ]] && return 0
  done
  local backup=""
  if [[ -e "$path" || -L "$path" ]]; then
    backup="$INSTALL_TRANSACTION_ROOT/path-${#INSTALL_TRANSACTION_PATHS[@]}"
    cp -a -- "$path" "$backup"
  fi
  INSTALL_TRANSACTION_PATHS+=("$path")
  INSTALL_TRANSACTION_BACKUPS+=("$backup")
}

transaction_begin() {
  INSTALL_TRANSACTION_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/pisec-install.XXXXXX")"
  chmod 0700 "$INSTALL_TRANSACTION_ROOT"
  INSTALL_TRANSACTION_ACTIVE=1
  trap transaction_rollback EXIT
}

transaction_start_service() {
  local unit="$1"
  local was_active=0
  if systemctl --user is-active --quiet "$unit" >/dev/null 2>&1; then
    was_active=1
  fi
  INSTALL_TRANSACTION_SERVICES+=("$unit")
  INSTALL_TRANSACTION_SERVICE_WAS_ACTIVE+=("$was_active")
  systemctl --user enable --now "$unit"
}
transaction_retire_service() {
  local unit="$1"
  if systemctl --user is-active --quiet "$unit" >/dev/null 2>&1; then
    INSTALL_TRANSACTION_LEGACY_SERVICES+=("$unit")
    systemctl --user disable --now "$unit"
  fi
}


transaction_commit() {
  [[ "$INSTALL_TRANSACTION_ACTIVE" -eq 1 ]] || return 0
  rm -rf -- "$INSTALL_TRANSACTION_ROOT"
  INSTALL_TRANSACTION_ACTIVE=0
  trap - EXIT
}

transaction_rollback() {
  local status=$?
  trap - EXIT
  if [[ "$INSTALL_TRANSACTION_ACTIVE" -eq 1 ]]; then
    set +e
    local index
    if [[ "$INSTALL_TRANSACTION_COLLIE_STARTED" -eq 1 ]]; then
      systemctl --user disable --now collie.service >/dev/null 2>&1
    fi
    for ((index=${#INSTALL_TRANSACTION_SERVICES[@]} - 1; index >= 0; index--)); do
      if [[ "${INSTALL_TRANSACTION_SERVICE_WAS_ACTIVE[index]}" != "1" ]]; then
        systemctl --user disable --now "${INSTALL_TRANSACTION_SERVICES[index]}" >/dev/null 2>&1
      fi
    done
    for ((index=${#INSTALL_TRANSACTION_CREATED_PATHS[@]} - 1; index >= 0; index--)); do
      rm -rf -- "${INSTALL_TRANSACTION_CREATED_PATHS[index]}"
    done
    for ((index=${#INSTALL_TRANSACTION_PATHS[@]} - 1; index >= 0; index--)); do
      local path="${INSTALL_TRANSACTION_PATHS[index]}"
      local backup="${INSTALL_TRANSACTION_BACKUPS[index]}"
      rm -rf -- "$path"
      if [[ -n "$backup" && ( -e "$backup" || -L "$backup" ) ]]; then
        mkdir -p "$(dirname "$path")"
        mv -- "$backup" "$path"
      fi
    done
    rm -rf -- "$INSTALL_TRANSACTION_ROOT"
    INSTALL_TRANSACTION_ACTIVE=0
    if [[ -n "$INSTALL_RESET_STATE_ARCHIVE" && -d "$INSTALL_RESET_STATE_ARCHIVE" ]]; then
      rm -rf -- "$INSTALL_RESET_STATE_ROOT"
      mkdir -p "$(dirname "$INSTALL_RESET_STATE_ROOT")"
      cp -a -- "$INSTALL_RESET_STATE_ARCHIVE" "$INSTALL_RESET_STATE_ROOT"
      chmod 0700 "$INSTALL_RESET_STATE_ROOT"
    fi
    for unit in "${INSTALL_TRANSACTION_LEGACY_SERVICES[@]}"; do
      systemctl --user enable --now "$unit" >/dev/null 2>&1 || true
    done
    for unit in "${INSTALL_RESET_STOPPED_SERVICES[@]}"; do
      systemctl --user enable --now "$unit" >/dev/null 2>&1
    done
    set -e
  fi
  exit "$status"
}

transaction_begin

link_file() {
  local source_path="$1"
  local target_path="$2"
  if [[ -L "$target_path" && "$(readlink "$target_path")" == "$source_path" ]]; then
    printf 'ok: %s -> %s\n' "$target_path" "$source_path"
    return
  fi
  transaction_capture_path "$target_path"
  mkdir -p "$(dirname "$target_path")"
  if [[ -e "$target_path" || -L "$target_path" ]]; then
    local backup_path="${target_path}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
    transaction_record_created_path "$backup_path"
    mv "$target_path" "$backup_path"
    printf 'backup: %s -> %s\n' "$target_path" "$backup_path"
  fi
  ln -s "$source_path" "$target_path"
  printf 'link: %s -> %s\n' "$target_path" "$source_path"
}

check_command() {
  local name="$1"
  command -v "$name" >/dev/null 2>&1 || die "missing required command: $name"
}

retire_managed_path() {
  local path="$1"
  if [[ -e "$path" || -L "$path" ]]; then
    transaction_capture_path "$path"
    rm -rf -- "$path"
    printf 'remove: retired managed path %s\n' "$path"
  fi
}

write_wrapper() {
  local target="$1"
  local body="$2"
  transaction_capture_path "$target"
  local temporary="${target}.tmp.$$"
  transaction_record_created_path "$temporary"
  printf '%s\n' "$body" >"$temporary"
  chmod 0700 "$temporary"
  mv -f "$temporary" "$target"
}


verify_executable() {
  local path="$1"
  [[ -f "$path" && -x "$path" && ! -L "$path" ]] || die "required stable executable is not a regular executable: $path"
}
stage_treehouse() {
  local archive="$INSTALL_TRANSACTION_ROOT/$TREEHOUSE_ARCHIVE"
  local checksum_file="$INSTALL_TRANSACTION_ROOT/treehouse.sha256"
  local extract_root="$INSTALL_TRANSACTION_ROOT/treehouse-extract"
  local archive_candidate="$extract_root/treehouse"
  local candidate="$extract_root/treehouse-staged"
  local members
  local permissions
  local version
  [[ "$INSTALL_TRANSACTION_ACTIVE" -eq 1 ]] || die "Treehouse staging requires an active install transaction"
  printf 'Downloading pinned Treehouse %s\n' "$TREEHOUSE_VERSION"
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 "$TREEHOUSE_URL" --output "$archive" || die "unable to download pinned Treehouse"
  [[ -f "$archive" && ! -L "$archive" ]] || die "downloaded Treehouse archive is missing or unsafe"
  printf '%s  %s\n' "$TREEHOUSE_SHA256" "$archive" >"$checksum_file"
  sha256sum -c "$checksum_file" || die "pinned Treehouse checksum failed"
  mkdir -p "$extract_root"
  members="$(tar -tzf "$archive")" || die "unable to inspect pinned Treehouse archive"
  [[ "$members" == "treehouse" ]] || die "pinned Treehouse archive must contain only top-level treehouse"
  tar --no-same-owner --no-same-permissions -xzf "$archive" -C "$extract_root" -- treehouse || die "unable to extract pinned Treehouse"
  mv -- "$archive_candidate" "$candidate"
  [[ -f "$candidate" && ! -L "$candidate" ]] || die "extracted Treehouse is not a regular file"
  [[ "$(stat -c '%u' "$candidate")" == "$(id -u)" ]] || die "extracted Treehouse is not user-owned"
  permissions="$(stat -c '%A' "$candidate")"
  [[ "${permissions:0:1}" == "-" && "${permissions:3:1}" == "x" && "${permissions:5:1}" != "w" && "${permissions:8:1}" != "w" ]] || die "extracted Treehouse has unsafe permissions"
  version="$("$candidate" --version 2>&1)" || die "pinned Treehouse --version failed"
  [[ "$version" == "$TREEHOUSE_VERSION" ]] || die "pinned Treehouse version mismatch: $version"
  TREEHOUSE_STAGED_BIN="$candidate"
}
secure_secret() {
  local path="$1"
  [[ -f "$path" && ! -L "$path" ]] || return 1
  [[ "$(stat -c '%u' "$path")" == "$(id -u)" && "$(stat -c '%a' "$path")" == "600" ]]
}

validate_pisec_config() {
  local path="$1"
  PYTHONPATH="$DOTFILES_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 - "$path" <<'PY'
from pathlib import Path
import sys

from scripts.pisec.bootstrap import build_adapters
from scripts.pisec.config import load_config
from scripts.pisec.pi_store import default_state_root

try:
    config = load_config(Path(sys.argv[1]))
    build_adapters(config, default_state_root())
except Exception as error:
    raise SystemExit(str(error)[:512])
PY
}
pisec_state_root() {
  printf '%s\n' "${PISEC_STATE_ROOT:-${XDG_STATE_HOME:-$HOME/.local/state}/pisec}"
}

check_pisec_state_epoch() {
  local root="$1"
  PISEC_STATE_CHECK_ROOT="$root" PYTHONPATH="$DOTFILES_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY'
from pathlib import Path
import os
import sqlite3

from scripts.pisec.pi_schema import MIGRATION_NAME, PREVIOUS_MIGRATION_NAME, PREVIOUS_SCHEMA_DIGEST, PREVIOUS_SCHEMA_VERSION, SCHEMA_NAME, SCHEMA_VERSION, schema_digest

root = Path(os.environ["PISEC_STATE_CHECK_ROOT"])
if not root.exists() and not root.is_symlink():
    print("absent")
    raise SystemExit(0)
if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.geteuid() or (root.stat().st_mode & 0o777) != 0o700:
    raise SystemExit("Pisec state root is unsafe")
database = root / "control.db"
if database.is_symlink() or not database.is_file() or database.stat().st_uid != os.geteuid() or (database.stat().st_mode & 0o777) != 0o600:
    raise SystemExit("Pisec state database is unsafe")
try:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2.0)
    row = connection.execute("SELECT schema_name,schema_version,schema_sha256,migration_name FROM control_meta WHERE singleton=1").fetchone()
except sqlite3.Error:
    print("stale")
    raise SystemExit(2)
finally:
    if connection is not None:
        connection.close()
expected = (SCHEMA_NAME, SCHEMA_VERSION, schema_digest(), MIGRATION_NAME)
actual = None if row is None else tuple(row)
if actual == expected:
    print("current")
elif actual == (SCHEMA_NAME, PREVIOUS_SCHEMA_VERSION, PREVIOUS_SCHEMA_DIGEST, PREVIOUS_MIGRATION_NAME):
    print("migratable")
else:
    print("stale")
    raise SystemExit(2)
PY
}

stop_for_pisec_services() {
  local unit
  for unit in collie.service pisec-auth-broker.service pisec-auth-gateway.service pisec-broker.service herdr.service herdr-pisec.service herdr-pi-personal.service; do
    if systemctl --user is-active --quiet "$unit" >/dev/null 2>&1; then
      INSTALL_RESET_STOPPED_SERVICES+=("$unit")
      systemctl --user stop "$unit" >/dev/null 2>&1 || die "unable to stop $unit for Pisec cutover"
      systemctl --user disable "$unit" >/dev/null 2>&1 || true
    fi
  done
}

archive_and_reset_pisec_state() {
  local root="$1"
  INSTALL_RESET_STATE_ROOT="$root"
  stop_for_pisec_services
  if [[ ! -e "$root" && ! -L "$root" ]]; then
    printf 'reset: no existing Pisec state at %s\n' "$root"
    return
  fi
  local archive="${root}.archive-$(date -u +%Y%m%dT%H%M%SZ)"
  local suffix=0
  while [[ -e "$archive" || -L "$archive" ]]; do
    suffix=$((suffix + 1))
    archive="${root}.archive-$(date -u +%Y%m%dT%H%M%SZ)-$suffix"
  done
  mv -- "$root" "$archive"
  chmod 0700 "$archive"
  INSTALL_RESET_STATE_ARCHIVE="$archive"
  printf 'archive: %s\n' "$archive"
}

collie_plugin_state() {
  local listing="$1"
  python3 - "$listing" <<'PY'
import json
import re
import sys

try:
    document = json.loads(sys.argv[1])
except json.JSONDecodeError:
    raise SystemExit("invalid")
if not isinstance(document, dict):
    raise SystemExit("invalid")
result = document.get("result", document)
plugins = result.get("plugins") if isinstance(result, dict) else None
if not isinstance(plugins, list):
    raise SystemExit("invalid")
for plugin in plugins:
    if not isinstance(plugin, dict) or plugin.get("plugin_id", plugin.get("id")) != "herdr.collie":
        continue
    version = str(plugin.get("version", ""))
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        print("wrong:" + (version or "missing"))
    elif (int(match.group(1)), int(match.group(2))) == (0, 28):
        print("valid")
    else:
        print("wrong:" + version)
    raise SystemExit(0)
print("absent")
PY
}

collie_plugin_root() {
  local listing="$1"
  python3 - "$listing" <<'PY'
import json
import os
import sys

try:
    document = json.loads(sys.argv[1])
except json.JSONDecodeError:
    raise SystemExit(1)
result = document.get("result", document) if isinstance(document, dict) else None
plugins = result.get("plugins") if isinstance(result, dict) else None
if not isinstance(plugins, list):
    raise SystemExit(1)
for plugin in plugins:
    if not isinstance(plugin, dict) or plugin.get("plugin_id", plugin.get("id")) != "herdr.collie":
        continue
    root = plugin.get("plugin_root")
    if not isinstance(root, str) or not os.path.isabs(root):
        raise SystemExit(1)
    print(root)
    raise SystemExit(0)
raise SystemExit(1)
PY
}
herdr_plugin_state() {
  local listing="$1"
  local plugin_id="$2"
  python3 - "$listing" "$plugin_id" <<'PY'
import json
import os
import sys

try:
    document = json.loads(sys.argv[1])
except json.JSONDecodeError:
    raise SystemExit("invalid")
result = document.get("result", document) if isinstance(document, dict) else None
plugins = result.get("plugins") if isinstance(result, dict) else None
if not isinstance(plugins, list):
    raise SystemExit("invalid")
for plugin in plugins:
    if isinstance(plugin, dict) and plugin.get("plugin_id", plugin.get("id")) == sys.argv[2]:
        root = plugin.get("plugin_root")
        manifest = plugin.get("manifest_path")
        valid = (
            isinstance(root, str)
            and os.path.isabs(root)
            and os.path.isdir(root)
            and isinstance(manifest, str)
            and os.path.isabs(manifest)
            and os.path.isfile(manifest)
        )
        print("present" if valid else "absent")
        raise SystemExit(0)
print("absent")
PY
}

install_herdr_plugin() {
  local repository="$1"
  local plugin_id="$2"
  local ref="$3"
  local listing
  local state
  listing="$("$HERDR_PATH" plugin list --json 2>&1)" || die "unable to inspect Herdr plugin $plugin_id"
  state="$(herdr_plugin_state "$listing" "$plugin_id")" || die "Herdr returned invalid plugin metadata"
  if [[ "$state" == "absent" ]]; then
    "$HERDR_PATH" plugin install "$repository" --ref "$ref" --yes
    listing="$("$HERDR_PATH" plugin list --json 2>&1)" || die "unable to verify Herdr plugin $plugin_id"
    state="$(herdr_plugin_state "$listing" "$plugin_id")" || die "Herdr returned invalid plugin metadata after installing $plugin_id"
  fi
  [[ "$state" == "present" ]] || die "Herdr plugin installation did not register $plugin_id"
  printf 'ok: Herdr plugin %s\n' "$plugin_id"
}


ensure_omp_token() {
  local path="$1"
  local command_name="$2"
  local action="$3"
  local label="$4"
  if [[ ! -e "$path" ]]; then
    transaction_capture_path "$path"
    transaction_record_created_path "$path"
    "$PISC_BIN_DIR/real-omp" "$command_name" "$action" >/dev/null || die "unable to create $label"
  fi
  secure_secret "$path" || die "$label is missing or not an owner-only regular file: $path"
}
normalize_omp_plugin_source() {
  PISEC_PLUGIN_SOURCE="$HOME/.omp/plugins" python3 - <<'PY'
from pathlib import Path
import os
import stat

root = Path(os.environ["PISEC_PLUGIN_SOURCE"])
if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.geteuid():
    raise SystemExit("OMP plugin source is not a user-owned directory")
for path in (root, *sorted(root.rglob("*"))):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        target = path.resolve(strict=True)
        if not target.is_relative_to(root.resolve()):
            raise SystemExit("OMP plugin source symlink escapes its root")
        continue
    if info.st_uid != os.geteuid() or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise SystemExit("OMP plugin source contains an unsafe entry")
    if stat.S_ISDIR(info.st_mode):
        os.chmod(path, 0o700)
    else:
        os.chmod(path, 0o700 if info.st_mode & stat.S_IXUSR else 0o600)
PY
}

wait_http() {
  local url="$1"
  local label="$2"
  for _ in $(seq 1 30); do
    if python3 -c 'from urllib.request import urlopen; import sys; response=urlopen(sys.argv[1], timeout=1); raise SystemExit(0 if 200 <= response.status < 300 else 1)' "$url" >/dev/null 2>&1; then
      printf 'ok: %s\n' "$label"
      return
    fi
    sleep 1
  done
  die "$label did not become healthy: $url"
}

verify_collie_surface() {
  local status_json="${1:-}"
  [[ -n "$status_json" ]] || status_json="$(tailscale serve status --json 2>&1)" || die "unable to verify Tailscale Serve route"
  if ! COLLIE_SERVE_STATUS="$status_json" COLLIE_PUBLIC_HOST="$COLLIE_HOST" COLLIE_PORT="$PISEC_COLLIE_PORT" python3 - <<'PY'
import json
import os
import sys

try:
    document = json.loads(os.environ["COLLIE_SERVE_STATUS"])
except (KeyError, json.JSONDecodeError):
    raise SystemExit("Tailscale Serve returned invalid JSON")
if not isinstance(document, dict):
    raise SystemExit("Tailscale Serve returned invalid JSON")
host = os.environ["COLLIE_PUBLIC_HOST"]
try:
    port = int(os.environ["COLLIE_PORT"])
except (KeyError, ValueError):
    raise SystemExit("Collie port is invalid")
web = document.get("Web", {})
if not isinstance(web, dict):
    raise SystemExit("Tailscale Serve returned invalid JSON")
tcp = document.get("TCP", {})
listener = tcp.get("443") if isinstance(tcp, dict) else None
expected_host = f"{host}:443"
expected_proxy = f"http://127.0.0.1:{port}"
route_ok = (
    (listener is None or (isinstance(listener, dict) and listener.get("HTTPS") is True))
    and set(web) == {expected_host}
    and isinstance(web.get(expected_host), dict)
    and isinstance(web[expected_host].get("Handlers"), dict)
    and isinstance(web[expected_host]["Handlers"].get("/"), dict)
    and web[expected_host]["Handlers"]["/"].get("Proxy") == expected_proxy
)
if not route_ok:
    raise SystemExit("Tailscale Serve has no Collie HTTPS root route")
PY
  then
    die "Tailscale Serve route is not Collie-only HTTPS on 127.0.0.1:$PISEC_COLLIE_PORT"
  fi
  local probe_url="${PISEC_COLLIE_PROBE_URL:-https://$COLLIE_HOST}"
  if ! COLLIE_TRUSTED_USER="$COLLIE_TRUSTED_USER" COLLIE_PORT="$PISEC_COLLIE_PORT" python3 - "$probe_url" <<'PY'
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import os
import sys

base = sys.argv[1].rstrip("/")
local_base = f"http://127.0.0.1:{int(os.environ['COLLIE_PORT'])}"
public_api = f"{base}/api/config"
local_api = f"{local_base}/api/config"

def status(url, headers):
    try:
        with urlopen(Request(url, headers=headers), timeout=3) as response:
            return int(response.status)
    except HTTPError as error:
        return int(error.code)
    except URLError as error:
        raise SystemExit(f"Collie public probe failed: {error}")

if not 200 <= status(base + "/", {}) < 300:
    raise SystemExit("Collie public HTTPS probe did not succeed")
if status(local_api, {"Tailscale-User-Login": "untrusted@example.invalid"}) not in {401, 403}:
    raise SystemExit("Collie did not reject an untrusted Tailscale user")
if status(public_api, {"Host": "wrong.example.invalid"}) not in {400, 401, 403}:
    raise SystemExit("Collie did not reject a wrong Host header")
if status(public_api, {"Origin": "https://wrong.example.invalid"}) not in {400, 401, 403}:
    raise SystemExit("Collie did not reject a wrong Origin header")
PY
  then
    printf 'ok: Collie Tailscale Serve route and local rejection probes\n'
  fi
}

wait_socket() {
  local path="$1"
  local label="$2"
  for _ in $(seq 1 30); do
    if [[ -S "$path" ]] && [[ "$(stat -c '%a' "$path")" == "600" ]]; then
      printf 'ok: %s\n' "$label"
      return
    fi
    sleep 1
  done
  die "$label did not become an owner-only socket: $path"
}
wait_herdr() {
  local label="$1"
  for _ in $(seq 1 30); do
    if "$HERDR_PATH" --session main status >/dev/null 2>&1; then
      printf 'ok: %s\n' "$label"
      return
    fi
    sleep 1
  done
  die "$label did not become ready"
}
feature_row_ok() {
  local label="$1"
  local line
  local normalized
  while IFS= read -r line; do
    normalized="${line#"${line%%[![:space:]]*}"}"
    if [[ "${normalized,,}" == "${label,,}"* ]]; then
      [[ "$normalized" =~ [[:space:]]ok([[:space:]]|$) ]]
      return
    fi
  done <<<"$features"
  return 1
}


if [[ "$SKILLS_ONLY" -eq 0 ]]; then
  printf '\nChecking pinned Pisec dependencies\n'
  check_command git
  check_command herdr
  check_command python3
  check_command curl
  check_command sha256sum
  check_command tar
  check_command systemctl
  check_command loginctl
  check_command tailscale
  check_command bun
  check_command fence
  check_command bwrap
  check_command socat
  check_command omp
  REAL_OMP_PATH="${REAL_OMP_PATH:-$(command -v omp)}"
  HERDR_PATH="${HERDR_PATH:-$(command -v herdr)}"
  FENCE_REAL_PATH="${FENCE_REAL_PATH:-$(command -v fence)}"
  [[ "$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 12) else 0)')" == 1 ]] || die "Python 3.12 or newer is required"

  herdr_version="$("$HERDR_PATH" --version 2>&1)"
  if ! python3 -c 'import re,sys; m=re.search(r"(\d+)\.(\d+)\.(\d+)",sys.stdin.read()); raise SystemExit(0 if m and tuple(map(int,m.groups())) >= (0,8,0) and tuple(map(int,m.groups())) < (0,9,0) else 1)' <<<"$herdr_version"; then
    die "Herdr 0.8.x is required; found $herdr_version"
  fi
  omp_version="$("$REAL_OMP_PATH" --version 2>&1)"
  if ! python3 -c 'import re,sys; m=re.search(r"(?:omp/|v)(\d+)\.(\d+)\.(\d+)",sys.stdin.read()); raise SystemExit(0 if m and tuple(map(int,m.groups())) >= (17,3,4) and tuple(map(int,m.groups())) < (18,0,0) else 1)' <<<"$omp_version"; then
    die "OMP 17.3.4-compatible API is required; found $omp_version"
  fi
  fence_version="$("$FENCE_REAL_PATH" --version 2>&1)"
  if ! python3 -c 'import re,sys; m=re.search(r"Version:\s*(\d+)\.(\d+)\.(\d+)",sys.stdin.read()); raise SystemExit(0 if m and tuple(map(int,m.groups())) >= (0,1,66) else 1)' <<<"$fence_version"; then
    die "Fence >=0.1.66 is required; found $fence_version"
  fi
  features="$("$FENCE_REAL_PATH" --linux-features 2>&1)"
  feature_row_ok "Bubblewrap" && bubblewrap_ok=1 || bubblewrap_ok=0
  feature_row_ok "Landlock" && landlock_ok=1 || landlock_ok=0
  feature_row_ok "Network namespace" && network_namespace_ok=1 || network_namespace_ok=0
  [[ "$bubblewrap_ok" -eq 1 && "$landlock_ok" -eq 1 ]] || die "Fence user namespaces/Landlock are unavailable; install bwrap and enable user namespaces/Landlock"
  [[ "$network_namespace_ok" -eq 1 ]] || die "Fence network namespaces are unavailable; Pisec cannot install without network isolation"
  [[ "$COLLIE_HOST" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$ ]] || die "Collie host is invalid"
  [[ -n "$COLLIE_TRUSTED_USER" && "$COLLIE_TRUSTED_USER" != *[[:space:]=$'\r'$'\n']* ]] || die "Collie trusted user is invalid"
  [[ -f "$DOTFILES_DIR/pisec/config.example.json" && -f "$DOTFILES_DIR/bin/pisec" ]] || die "Pisec source files are missing"
  [[ -f "$DOTFILES_DIR/nvim/init.lua" && -f "$DOTFILES_DIR/nvim/lua/plugins/herdr.lua" ]] || die "Neovim configuration is missing"
  funnel_status="$(tailscale funnel status --json 2>&1)" || die "unable to verify Tailscale Funnel state before installation"
  if ! FUNNEL_STATUS="$funnel_status" python3 - <<'PY'
import json
import os

try:
    value = json.loads(os.environ["FUNNEL_STATUS"])
except (KeyError, json.JSONDecodeError):
    raise SystemExit("Tailscale Funnel returned invalid JSON")

def contains_true(node):
    if node is True:
        return True
    if isinstance(node, dict):
        return any(contains_true(item) for item in node.values())
    if isinstance(node, list):
        return any(contains_true(item) for item in node)
    return False

def enabled(node):
    if isinstance(node, dict):
        for key, item in node.items():
            if "funnel" in str(key).lower() and contains_true(item):
                return True
            if enabled(item):
                return True
    elif isinstance(node, list):
        return any(enabled(item) for item in node)
    return False

raise SystemExit(1 if enabled(value) else 0)
PY
  then
    die "Tailscale Funnel is enabled or its state is invalid; disable Funnel before installation"
  fi
  validate_pisec_config "$DOTFILES_DIR/pisec/config.example.json" || die "Pisec example configuration is invalid"
  collie_listing="$("$HERDR_PATH" plugin list --json 2>&1)" || die "unable to inspect installed Collie plugin"
  collie_state="$(collie_plugin_state "$collie_listing")" || die "Herdr returned invalid plugin metadata"
  case "$collie_state" in
    wrong:*) die "Collie 0.28.x is required; found ${collie_state#wrong:}" ;;
    valid|absent) ;;
    *) die "Herdr returned invalid Collie plugin metadata" ;;
  esac
  pisec_state="$(pisec_state_root)"
  state_epoch_output=""
  state_epoch_status=0
  if state_epoch_output="$(check_pisec_state_epoch "$pisec_state" 2>&1)"; then
    :
  else
    state_epoch_status=$?
  fi
  if [[ "$state_epoch_status" -ne 0 && ( "$RESET_PISEC_STATE" -eq 0 || "$state_epoch_status" -ne 2 ) ]]; then
    die "existing Pisec state is unsafe or has an unsupported schema; rerun with --reset-pisec-state only after reviewing the state"
  fi
  if [[ "$RESET_PISEC_STATE" -eq 1 ]]; then
    archive_and_reset_pisec_state "$pisec_state"
  fi
  stage_treehouse
  printf 'ok: real OMP %s\n' "$REAL_OMP_PATH"
  printf 'ok: %s\n' "$herdr_version"
  printf 'ok: %s\n' "$omp_version"
  printf 'ok: %s\n' "$(printf '%s' "$fence_version" | tr '\n' ' ')"
fi
if [[ "$SKILLS_ONLY" -eq 0 ]]; then
  systemctl --user daemon-reload >/dev/null 2>&1 || die "unable to reload user services for Pisec cutover"
  stop_for_pisec_services
  printf '\nInstalling OMP UI plugin %s\n' "$PI_STICKY_SPEC"
  "$REAL_OMP_PATH" plugin install "$PI_STICKY_SPEC" || die "unable to install $PI_STICKY_SPEC"
  pi_sticky_extension="$HOME/.omp/plugins/node_modules/@burneikis/pi-sticky/index.ts"
  normalize_omp_plugin_source || die "installed OMP plugin source is unsafe"
  [[ -f "$pi_sticky_extension" && ! -L "$pi_sticky_extension" ]] || die "installed pi-sticky extension is missing or unsafe"
fi
printf 'Installing shared agent workflow links\n'
link_file "$DOTFILES_DIR/opencode/opencode.jsonc" "$HOME/.config/opencode/opencode.jsonc"
mkdir -p "$HOME/.config/dotfiles"
link_file "$MACHINE_PROFILE" "$HOME/.config/dotfiles/machine.env"
mkdir -p "$DOTFILES_DIR/omp/extensions" "$HOME/.omp/agent/extensions"
old_pisec="$HOME/.omp/agent/extensions/pisec.ts"
if [[ -L "$old_pisec" && "$(readlink "$old_pisec")" == "$DOTFILES_DIR/omp/extensions/pisec.ts" ]]; then
  transaction_capture_path "$old_pisec"
  rm "$old_pisec"
  printf 'remove: retired global Pisec extension link %s\n' "$old_pisec"
fi
mkdir -p "$DOTFILES_DIR/skills" "$DOTFILES_DIR/agent" "$HOME/.config/opencode" "$HOME/.codex" "$HOME/.omp/agent"
[[ -f "$DOTFILES_DIR/agent/AGENTS.md" && ! -L "$DOTFILES_DIR/agent/AGENTS.md" && -d "$DOTFILES_DIR/skills" && ! -L "$DOTFILES_DIR/skills" ]] || die "canonical agent instructions or skills are missing or unsafe"
chmod go-w "$DOTFILES_DIR/agent/AGENTS.md" "$DOTFILES_DIR/skills"
link_file "$DOTFILES_DIR/agent/AGENTS.md" "$HOME/.omp/agent/AGENTS.md"
link_file "$DOTFILES_DIR/agent/AGENTS.md" "$HOME/.codex/AGENTS.md"
link_file "$DOTFILES_DIR/skills" "$HOME/.skills"
link_file "$HOME/.skills" "$HOME/.config/opencode/skills"
link_file "$HOME/.skills" "$HOME/.codex/skills"
link_file "$HOME/.skills" "$HOME/.omp/agent/skills"

if [[ "$SKILLS_ONLY" -eq 1 ]]; then
  transaction_commit
  printf '\nDone. Shared agent workflow links are installed (--skills-only).\n'
  exit 0
fi
printf '\nLinking Neovim configuration\n'
link_file "$DOTFILES_DIR/nvim" "$HOME/.config/nvim"



PISC_BIN_DIR="$HOME/.local/lib/pisec/bin"
PISC_LEGACY_PERSONAL_BIN_DIR="$HOME/.local/lib/pisec/personal-bin"
TREEHOUSE_HOST_PATH="$HOME/.local/bin/treehouse"
mkdir -p "$PISC_BIN_DIR" "$HOME/.local/bin"
chmod 0700 "$PISC_BIN_DIR"
retire_managed_path "$PISC_LEGACY_PERSONAL_BIN_DIR"
retire_managed_path "$PISC_BIN_DIR/herdr-personal"
retire_managed_path "$PISC_BIN_DIR/pisec-shell"
transaction_capture_path "$PISC_BIN_DIR/real-omp"
cp --reflink=auto -- "$REAL_OMP_PATH" "$PISC_BIN_DIR/real-omp"
chmod 0755 "$PISC_BIN_DIR/real-omp"
write_wrapper "$PISC_BIN_DIR/omp" "#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' 'Pisec owns project OMP sessions. Use \"pisec project open <repository>\" for project work or \"omp-admin\" for broad host work.' >&2
exit 126"
write_wrapper "$PISC_BIN_DIR/omp-admin" "#!/usr/bin/env bash
set -euo pipefail
for variable in \${!PISEC_@}; do
  unset \"\$variable\"
done
unset PI_CONFIG_DIR PI_CODING_AGENT_DIR XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME XDG_CONFIG_HOME
unset GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_CONFIG_GLOBAL GIT_CONFIG_NOSYSTEM GIT_CONFIG_COUNT
path_value="\${PATH:-}"
clean_path=""
IFS=: read -r -a path_entries <<< "\$path_value"
for entry in "\${path_entries[@]}"; do
  [[ "\$entry" == "$PISC_BIN_DIR" ]] && continue
  if [[ -n "\$clean_path" ]]; then clean_path+=":"; fi
  clean_path+="\$entry"
done
export PATH="$HOME/.local/bin\${clean_path:+:\$clean_path}"
if [[ -n \"\${HERDR_PANE_ID:-}\" ]]; then
  \"$PISC_BIN_DIR/herdr\" pane rename \"\$HERDR_PANE_ID\" Admin >/dev/null 2>&1 || true
fi
exec \"$REAL_OMP_PATH\" \"\$@\""
write_wrapper "$PISC_BIN_DIR/fence" "#!/usr/bin/env bash
set -euo pipefail
exec \"$FENCE_REAL_PATH\" \"\$@\""
write_wrapper "$PISC_BIN_DIR/pisec-broker" "#!/usr/bin/env bash
set -euo pipefail
exec \"$DOTFILES_DIR/bin/pisec\" broker \"\$@\""
write_wrapper "$PISC_BIN_DIR/pisec-auth-broker" "#!/usr/bin/env bash
set -euo pipefail
exec \"$PISC_BIN_DIR/real-omp\" auth-broker serve --bind=127.0.0.1:\${PISEC_AUTH_BROKER_PORT:-8765} \"\$@\""
write_wrapper "$PISC_BIN_DIR/pisec-auth-gateway" "#!/usr/bin/env bash
set -euo pipefail
token_file=\"\${OMP_AUTH_BROKER_TOKEN_FILE:-\$HOME/.omp/auth-broker.token}\"
[[ -r \"\$token_file\" ]] || { printf 'auth broker token is missing: %s\n' \"\$token_file\" >&2; exit 1; }
export OMP_AUTH_BROKER_TOKEN=\"\$(<\"\$token_file\")\"
broker_port=\"\${PISEC_AUTH_BROKER_PORT:-8765}\"
export OMP_AUTH_BROKER_URL=\"\${OMP_AUTH_BROKER_URL:-http://127.0.0.1:\$broker_port}\"
exec \"$PISC_BIN_DIR/real-omp\" auth-gateway serve --bind=127.0.0.1:\${PISEC_AUTH_GATEWAY_PORT:-4000} \"\$@\""
write_wrapper "$PISC_BIN_DIR/herdr" "#!/usr/bin/env bash
set -euo pipefail
if [[ \$# -eq 0 ]]; then
  exec \"$HERDR_PATH\" --session main server
fi
exec \"$HERDR_PATH\" \"\$@\""
transaction_capture_path "$TREEHOUSE_HOST_PATH"
install -m 0755 -- "$TREEHOUSE_STAGED_BIN" "$TREEHOUSE_HOST_PATH"
for stable_executable in "$PISC_BIN_DIR/real-omp" "$PISC_BIN_DIR/fence" "$PISC_BIN_DIR/omp" "$PISC_BIN_DIR/omp-admin" "$PISC_BIN_DIR/pisec-broker" "$PISC_BIN_DIR/pisec-auth-broker" "$PISC_BIN_DIR/pisec-auth-gateway" "$PISC_BIN_DIR/herdr" "$TREEHOUSE_HOST_PATH"; do
  verify_executable "$stable_executable"
done
transaction_capture_path "$HOME/.bashrc"
python3 "$DOTFILES_DIR/scripts/pisec/host_config.py" patch-bashrc "$HOME/.bashrc" "$PISC_BIN_DIR"

printf '\nSeeding Pisec configuration\n'
mkdir -p "$HOME/.local/bin" "$HOME/.config/pisec"
pisec_config="$HOME/.config/pisec/config.json"
transaction_capture_path "$pisec_config"
if [[ ! -e "$pisec_config" && ! -L "$pisec_config" ]]; then
  install -m 0600 "$DOTFILES_DIR/pisec/config.example.json" "$pisec_config"
  printf 'seed: %s\n' "$pisec_config"
elif [[ -L "$pisec_config" ]]; then
  die "Pisec configuration path is a symlink: $pisec_config"
fi
python3 "$DOTFILES_DIR/scripts/pisec/host_config.py" patch-pisec "$pisec_config" "$PISC_BIN_DIR/real-omp" "$PISC_BIN_DIR/fence"
validate_pisec_config "$pisec_config" || die "Pisec configuration is invalid"
retire_managed_path "$HOME/.config/pisec/herdr.toml"
gateway_token_file="$(python3 - "$pisec_config" <<'PY'
import json
from pathlib import Path
import sys

document = json.loads(Path(sys.argv[1]).read_text())
value = document.get("harness", {}).get("config", {}).get("gateway", {}).get("tokenFile", "~/.omp/auth-gateway.token")
path = Path(value).expanduser()
if not path.is_absolute():
    raise SystemExit("gateway tokenFile must be absolute or home-relative")
print(path)
PY
)"
ensure_omp_token "$HOME/.omp/auth-broker.token" auth-broker token "auth broker bearer token"
ensure_omp_token "$gateway_token_file" auth-gateway token "auth gateway bearer token"
link_file "$DOTFILES_DIR/bin/pisec" "$HOME/.local/bin/pisec"
transaction_capture_path "$HOME/.config/herdr/config.toml"
python3 "$DOTFILES_DIR/scripts/pisec/host_config.py" patch-herdr "$HOME/.config/herdr/config.toml"
herdr_plugins_dir="${XDG_CONFIG_HOME:-$HOME/.config}/herdr/plugins"
transaction_capture_path "$herdr_plugins_dir"
printf '\nRegistering Herdr Pisec plugin\n'
"$HERDR_PATH" plugin link "$DOTFILES_DIR/herdr/plugins/pisec" --enabled

ports_env="$HOME/.config/pisec/ports.env"
transaction_capture_path "$ports_env"
ports_env_tmp="${ports_env}.tmp.$$"
transaction_record_created_path "$ports_env_tmp"
printf 'PISEC_AUTH_BROKER_PORT=%s\nPISEC_AUTH_GATEWAY_PORT=%s\nPISEC_COLLIE_PORT=%s\n' "$PISEC_AUTH_BROKER_PORT" "$PISEC_AUTH_GATEWAY_PORT" "$PISEC_COLLIE_PORT" >"$ports_env_tmp"
chmod 0600 "$ports_env_tmp"
mv -f "$ports_env_tmp" "$ports_env"
printf '\nInstalling user services\n'
for legacy_unit in herdr-pisec.service herdr-pi-personal.service; do
  transaction_retire_service "$legacy_unit"
  retire_managed_path "$HOME/.config/systemd/user/$legacy_unit"
done
for unit in pisec-auth-broker.service pisec-auth-gateway.service pisec-broker.service herdr.service; do
  link_file "$DOTFILES_DIR/systemd/user/$unit" "$HOME/.config/systemd/user/$unit"
done
python3 - "$HOME" "$PISC_BIN_DIR" <<'PY'
import pathlib
import re
import sys

home = pathlib.Path(sys.argv[1])
stable = pathlib.Path(sys.argv[2])
targets = {
    "pisec-auth-broker.service": stable / "pisec-auth-broker",
    "pisec-auth-gateway.service": stable / "pisec-auth-gateway",
    "pisec-broker.service": stable / "pisec-broker",
    "herdr.service": stable / "herdr",
}
unit_dir = home / ".config" / "systemd" / "user"
for name, target in targets.items():
    text = (unit_dir / name).read_text()
    match = re.search(r"^ExecStart=(\S+)$", text, re.MULTILINE)
    if match is None or pathlib.Path(match.group(1).replace("%h", str(home))) != target:
        raise SystemExit(f"unit ExecStart is not the stable Pisec target: {name}")
PY
loginctl enable-linger "$USER" || die "user lingering is unavailable; run: loginctl enable-linger $USER"
systemctl --user daemon-reload
transaction_start_service pisec-auth-broker.service
wait_http "http://127.0.0.1:$PISEC_AUTH_BROKER_PORT/v1/healthz" "auth broker health"
transaction_start_service pisec-auth-gateway.service
wait_http "http://127.0.0.1:$PISEC_AUTH_GATEWAY_PORT/healthz" "auth gateway health"
auth_broker_token_file="$HOME/.omp/auth-broker.token"
[[ -s "$auth_broker_token_file" ]] || die "auth broker did not create its bearer token"
if ! OMP_AUTH_BROKER_URL="http://127.0.0.1:$PISEC_AUTH_BROKER_PORT" OMP_AUTH_BROKER_TOKEN="$(<"$auth_broker_token_file")" "$PISC_BIN_DIR/real-omp" auth-gateway check --strict --json; then
  die "auth gateway has no usable configured credential"
fi
transaction_start_service pisec-broker.service
runtime_root="${PISEC_RUNTIME_ROOT:-${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/pisec}"
wait_socket "$runtime_root/admin/control.sock" "Pisec admin socket"
wait_socket "$runtime_root/secretary/control.sock" "Pisec secretary socket"
wait_socket "$runtime_root/runtime/control.sock" "Pisec runtime socket"
transaction_start_service herdr.service
wait_socket "$HOME/.config/herdr/sessions/main/herdr.sock" "Herdr main socket"
wait_herdr "Herdr main readiness"
printf '\nInstalling Herdr workspace plugins\n'
install_herdr_plugin "$HERDR_NVIM_SPEC" "$HERDR_NVIM_ID" "$HERDR_NVIM_REF"
install_herdr_plugin "$HERDR_REVIEWR_SPEC" "$HERDR_REVIEWR_ID" "$HERDR_REVIEWR_REF"

printf '\nInstalling pinned Collie %s\n' "$COLLIE_REF"
if [[ "$collie_state" == "absent" ]]; then
  "$HERDR_PATH" plugin install AltanS/collie --ref "$COLLIE_REF" --yes
  collie_listing="$("$HERDR_PATH" plugin list --json 2>&1)" || die "unable to verify installed Collie plugin"
  collie_state="$(collie_plugin_state "$collie_listing")" || die "Herdr returned invalid plugin metadata after Collie installation"
  [[ "$collie_state" == "valid" ]] || die "Collie installation did not provide a 0.28.x plugin"
fi
collie_source_dir="$(collie_plugin_root "$collie_listing")" || die "Herdr did not report the Collie managed checkout"
apply_collie_patch "$collie_source_dir"
collie_dir="${XDG_CONFIG_HOME:-$HOME/.config}/herdr/plugins/config/herdr.collie"
mkdir -p "$collie_dir"
python3 "$DOTFILES_DIR/scripts/pisec/host_config.py" collie-env "$collie_dir/.env" "$COLLIE_HOST" "$COLLIE_TRUSTED_USER"
funnel_status="$(tailscale funnel status --json 2>&1)" || die "unable to verify Tailscale Funnel state before starting Collie"
if ! FUNNEL_STATUS="$funnel_status" python3 - <<'PY'
import json
import os

try:
    value = json.loads(os.environ["FUNNEL_STATUS"])
except (KeyError, json.JSONDecodeError):
    raise SystemExit("Tailscale Funnel returned invalid JSON")

def contains_true(node):
    if node is True:
        return True
    if isinstance(node, dict):
        return any(contains_true(item) for item in node.values())
    if isinstance(node, list):
        return any(contains_true(item) for item in node)
    return False

def enabled(node):
    if isinstance(node, dict):
        for key, item in node.items():
            if "funnel" in str(key).lower() and contains_true(item):
                return True
            if enabled(item):
                return True
    elif isinstance(node, list):
        return any(enabled(item) for item in node)
    return False

raise SystemExit(1 if enabled(value) else 0)
PY
then
  die "Tailscale Funnel is enabled or its state is invalid; disable Funnel before starting Collie"
fi
"$HERDR_PATH" --session main plugin action invoke start --plugin herdr.collie
INSTALL_TRANSACTION_COLLIE_STARTED=1
if [[ -z "${PISEC_COLLIE_PROBE_URL:-}" ]]; then
  wait_http "https://${COLLIE_HOST}/" "Collie public HTTPS"
fi
verify_collie_surface

printf '\nRolling stale Pisec runtimes to the deployed generation\n'
if ! refresh_output="$(python3 "$HOME/.local/bin/pisec" --json project refresh --all --wait-seconds 300)"; then
  die "Pisec runtime refresh could not run"
fi
printf '%s\n' "$refresh_output"
if ! REFRESH_OUTPUT="$refresh_output" python3 - <<'PY'
import json
import os

try:
    result = json.loads(os.environ["REFRESH_OUTPUT"])
except (KeyError, json.JSONDecodeError):
    raise SystemExit("Pisec runtime refresh returned invalid JSON")
if not isinstance(result, dict) or not isinstance(result.get("upgraded"), list) or not isinstance(result.get("pending"), list):
    raise SystemExit("Pisec runtime refresh returned an invalid result")
if result.get("failed"):
    raise SystemExit("Pisec runtime refresh reported failed bindings")
PY
then
  die "Pisec runtime refresh reported failed bindings"
fi

printf '\nRunning final Pisec doctor\n'
if ! doctor_output="$(python3 "$HOME/.local/bin/pisec" doctor --json)"; then
  die "final Pisec doctor could not run"
fi
printf '%s\n' "$doctor_output"
if ! DOCTOR_OUTPUT="$doctor_output" python3 - <<'PY'
import json
import os

try:
    result = json.loads(os.environ["DOCTOR_OUTPUT"])
except (KeyError, json.JSONDecodeError):
    raise SystemExit("final Pisec doctor returned invalid JSON")
if not isinstance(result, dict) or result.get("ok") is not True:
    raise SystemExit("final Pisec doctor reported failed checks")
PY
then
  die "final Pisec doctor reported failed checks"
fi
transaction_commit
printf '\nDone. Pisec services, fenced Herdr session, and tailnet-only Collie are configured.\n'
