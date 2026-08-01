#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  printf 'NOT TESTED: Docker daemon is unavailable in this execution plane\n'
  exit 77
fi

root=$(mktemp -d)
container=""
volume=""
cleanup() {
  [[ -z "$container" ]] || docker rm -f "$container" >/dev/null 2>&1 || true
  [[ -z "$volume" ]] || docker volume rm "$volume" >/dev/null 2>&1 || true
  rm -rf "$root"
}
trap cleanup EXIT
home="$root/home"
repo="$root/authored/repo"
mkdir -p "$home/.config/pi" "$repo"
git -C "$repo" init -b main >/dev/null
git -C "$repo" config user.name 'Pi Test'
git -C "$repo" config user.email pi-test@example.invalid
printf 'base\n' > "$repo/tracked.txt"
git -C "$repo" add tracked.txt
git -C "$repo" commit -m base >/dev/null
cat > "$home/.config/pi/repository-policy.json" <<JSON
{
  "version": 1,
  "defaultMode": "isolated",
  "trustedRoots": ["$root/authored"],
  "isolatedRoots": [],
  "controlPlaneRepositories": [],
  "protectedBranches": ["main", "master"],
  "worktreeRoot": "$root/worktrees"
}
JSON
chmod 600 "$home/.config/pi/repository-policy.json"
route_result=$(HOME="$home" XDG_RUNTIME_DIR="$root/runtime" scripts/pi-workspace.py prepare --cwd "$repo" --owner-pid "$$")
route=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["route"])' <<<"$route_result")
worktree=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["worktree"])' "$route")
common=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gitCommonDir"])' "$route")
gitdir=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gitDir"])' "$route")
context=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["hostContext"])' "$route")
gitconfig=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["gitConfig"])' "$route")
# Prove the task snapshot, rather than repository-local identity, supplies commits.
git -C "$repo" config --unset-all user.name
git -C "$repo" config --unset-all user.email
uid=$(id -u); gid=$(id -g)
host_owner() {
  stat -c %u:%g "$1" 2>/dev/null || stat -f %u:%g "$1"
}
container="pi-harness-integration-$$"
mounts=(--mount "type=bind,src=$worktree,dst=$worktree,bind-propagation=rprivate")
case "$common" in "$worktree"/*) ;; *) mounts+=(--mount "type=bind,src=$common,dst=$common,bind-propagation=rprivate");; esac
case "$gitdir" in "$worktree"/*|"$common"/*) ;; *) mounts+=(--mount "type=bind,src=$gitdir,dst=$gitdir,bind-propagation=rprivate");; esac
volume="pi-package-cache-v2-integration-$$"
docker volume create \
  --label pi.package-cache.managed=true \
  --label pi.package-cache.scope=repo \
  --label pi.package-cache.environment-key=task-local \
  "$volume" >/dev/null
docker create --name "$container" \
  --label pi.container-sandbox.managed=true \
  --label pi.container-sandbox.target=trusted-live \
  --user "$uid:$gid" --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs "/tmp/pi-home:rw,nosuid,nodev,mode=0700,uid=$uid,gid=$gid" \
  "${mounts[@]}" \
  --mount "type=bind,src=$context,dst=/run/pi/HOST_CONTEXT.md,readonly=true,bind-propagation=rprivate" \
  --mount "type=bind,src=$gitconfig,dst=/run/pi/GIT_CONFIG_GLOBAL,readonly=true,bind-propagation=rprivate" \
  --mount "type=volume,src=$volume,dst=/var/cache/pi-packages" \
  -e HOME=/tmp/pi-home -e CI=1 \
  -e GIT_CONFIG_GLOBAL=/run/pi/GIT_CONFIG_GLOBAL -e GIT_CONFIG_NOSYSTEM=1 \
  -e npm_config_cache=/var/cache/pi-packages/npm \
  -e npm_config_store_dir=/var/cache/pi-packages/pnpm \
  -e PNPM_STORE_DIR=/var/cache/pi-packages/pnpm \
  -e BUN_INSTALL_CACHE_DIR=/var/cache/pi-packages/bun \
  -e PIP_CACHE_DIR=/var/cache/pi-packages/pip \
  -e UV_CACHE_DIR=/var/cache/pi-packages/uv \
  -e VIRTUAL_ENV=/opt/pi/task-env \
  -e UV_PROJECT_ENVIRONMENT=/opt/pi/task-env \
  -e PATH=/opt/pi/task-env/bin:/home/sandbox/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  pi-tool-sandbox:node22-bookworm-20260728 sleep infinity >/dev/null
docker start "$container" >/dev/null

printf 'host\n' > "$worktree/host-visible.txt"
docker exec -w "$worktree" "$container" test -f host-visible.txt
docker exec -w "$worktree" "$container" sh -c 'printf "container\n" > container-visible.txt'
test "$(cat "$worktree/container-visible.txt")" = container
test "$(host_owner "$worktree/container-visible.txt")" = "$uid:$gid"
test "$(docker exec "$container" sh -c 'id -u; id -g' | paste -sd: -)" = "$uid:$gid"
docker exec "$container" test ! -e /var/run/docker.sock
docker exec "$container" test ! -e "$root/host-home-sentinel"
docker exec "$container" test ! -e /dev/dri
docker exec "$container" test -r /run/pi/HOST_CONTEXT.md
docker exec "$container" test ! -w /run/pi/HOST_CONTEXT.md
docker exec "$container" test -r /run/pi/GIT_CONFIG_GLOBAL
docker exec "$container" test ! -w /run/pi/GIT_CONFIG_GLOBAL
test "$(docker exec "$container" git config --global --name-only --list)" = $'user.name\nuser.email'
printf 'identity\n' > "$worktree/identity.txt"
docker exec -w "$worktree" "$container" git add identity.txt
docker exec -w "$worktree" "$container" git commit -m 'verify mounted Git identity' >/dev/null
mapfile -t committed_identity < <(docker exec -w "$worktree" "$container" git show -s --format='%an%n%ae%n%cn%n%ce')
test "${committed_identity[0]}" = 'Pi Test'
test "${committed_identity[1]}" = 'pi-test@example.invalid'
test "${committed_identity[2]}" = 'Pi Test'
test "${committed_identity[3]}" = 'pi-test@example.invalid'
inspect=$(docker inspect "$container")
image_id=$(docker image inspect --format '{{.Id}}' pi-tool-sandbox:node22-bookworm-20260728)
INSPECT_JSON="$inspect" python3 - "$worktree" "$common" "$gitdir" "$context" "$gitconfig" "$image_id" <<'PY'
import json, os, sys
item = json.loads(os.environ["INSPECT_JSON"])[0]
mounts = item["Mounts"]
expected_list = []
for raw in sys.argv[1:4]:
    candidate = os.path.realpath(raw)
    if not any(os.path.commonpath([source, candidate]) == source for source in expected_list):
        expected_list.append(candidate)
expected = set(expected_list)
special_destinations = {"/run/pi/HOST_CONTEXT.md", "/run/pi/GIT_CONFIG_GLOBAL"}
actual = {os.path.realpath(m["Source"]) for m in mounts if m["Type"] == "bind" and m["Destination"] not in special_destinations}
assert actual == expected, (actual, expected)
for mount in mounts:
    if mount["Type"] == "bind": assert mount["Propagation"] == "rprivate"
context = next(m for m in mounts if m["Destination"] == "/run/pi/HOST_CONTEXT.md")
gitconfig = next(m for m in mounts if m["Destination"] == "/run/pi/GIT_CONFIG_GLOBAL")
assert not context["RW"] and os.path.realpath(context["Source"]) == os.path.realpath(sys.argv[4])
assert not gitconfig["RW"] and os.path.realpath(gitconfig["Source"]) == os.path.realpath(sys.argv[5])
assert item["Image"] == sys.argv[6]
host = item["HostConfig"]
config = item["Config"]
assert config["User"] == f"{os.getuid()}:{os.getgid()}"
assert host["SecurityOpt"] == ["no-new-privileges:true"]
assert not (host.get("CapAdd") or [])
assert host["CapDrop"] == ["ALL"]
assert not host["Privileged"] and not host["PidMode"]
assert host["NetworkMode"] != "host" and host["IpcMode"] != "host"
assert set(item["NetworkSettings"]["Networks"]) == {"bridge"}
assert not host["Devices"] and not (host.get("DeviceRequests") or [])
assert config["Cmd"] == ["sleep", "infinity"]
tmpfs = set(host["Tmpfs"]["/tmp/pi-home"].split(","))
assert {"rw", "nosuid", "nodev", f"uid={os.getuid()}", f"gid={os.getgid()}"} <= tmpfs
assert "mode=0700" in tmpfs or "mode=700" in tmpfs
expected_env = {
    "HOME": "/tmp/pi-home", "CI": "1",
    "GIT_CONFIG_GLOBAL": "/run/pi/GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM": "1",
    "npm_config_cache": "/var/cache/pi-packages/npm",
    "npm_config_store_dir": "/var/cache/pi-packages/pnpm",
    "PNPM_STORE_DIR": "/var/cache/pi-packages/pnpm",
    "BUN_INSTALL_CACHE_DIR": "/var/cache/pi-packages/bun",
    "PIP_CACHE_DIR": "/var/cache/pi-packages/pip",
    "UV_CACHE_DIR": "/var/cache/pi-packages/uv",
    "VIRTUAL_ENV": "/opt/pi/task-env",
    "UV_PROJECT_ENVIRONMENT": "/opt/pi/task-env",
    "PATH": "/opt/pi/task-env/bin:/home/sandbox/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}
environment_entries = [entry.split("=", 1) for entry in config["Env"]]
environment = dict(environment_entries)
assert len(environment_entries) == len(environment)
assert all(environment.get(key) == value for key, value in expected_env.items())
assert host.get("PortBindings") in ({}, None)
PY
printf 'PASS trusted-live Docker mount, ownership, no-port default, and boundary integration\n'
