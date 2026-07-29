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
uid=$(id -u); gid=$(id -g)
container="pi-harness-integration-$$"
mounts=(--mount "type=bind,src=$worktree,dst=$worktree,rw,bind-propagation=rprivate")
case "$common" in "$worktree"/*) ;; *) mounts+=(--mount "type=bind,src=$common,dst=$common,rw,bind-propagation=rprivate");; esac
case "$gitdir" in "$worktree"/*|"$common"/*) ;; *) mounts+=(--mount "type=bind,src=$gitdir,dst=$gitdir,rw,bind-propagation=rprivate");; esac
volume="pi-integration-cache-$$"
docker create --name "$container" --user "$uid:$gid" --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs "/tmp/pi-home:rw,nosuid,nodev,mode=0700,uid=$uid,gid=$gid" \
  "${mounts[@]}" \
  --mount "type=bind,src=$context,dst=/run/pi/HOST_CONTEXT.md,ro,bind-propagation=rprivate" \
  --mount "type=volume,src=$volume,dst=/var/cache/pi-packages" \
  -e HOME=/tmp/pi-home -e CI=1 \
  pi-tool-sandbox:node22-bookworm-20260728 sleep infinity >/dev/null
docker start "$container" >/dev/null

printf 'host\n' > "$worktree/host-visible.txt"
docker exec -w "$worktree" "$container" test -f host-visible.txt
docker exec -w "$worktree" "$container" sh -c 'printf "container\n" > container-visible.txt'
test "$(cat "$worktree/container-visible.txt")" = container
test "$(stat -c %u:%g "$worktree/container-visible.txt")" = "$uid:$gid"
test "$(docker exec "$container" sh -c 'id -u; id -g' | paste -sd: -)" = "$uid:$gid"
docker exec "$container" test ! -e /var/run/docker.sock
docker exec "$container" test ! -e "$root/host-home-sentinel"
docker exec "$container" test ! -e /dev/dri
docker exec "$container" test -r /run/pi/HOST_CONTEXT.md
docker exec "$container" test ! -w /run/pi/HOST_CONTEXT.md
inspect=$(docker inspect "$container")
INSPECT_JSON="$inspect" python3 - "$worktree" "$common" "$gitdir" "$context" <<'PY'
import json, os, sys
item = json.loads(os.environ["INSPECT_JSON"])[0]
mounts = item["Mounts"]
expected_list = []
for raw in sys.argv[1:4]:
    candidate = os.path.realpath(raw)
    if not any(os.path.commonpath([source, candidate]) == source for source in expected_list):
        expected_list.append(candidate)
expected = set(expected_list)
actual = {os.path.realpath(m["Source"]) for m in mounts if m["Type"] == "bind" and m["Destination"] != "/run/pi/HOST_CONTEXT.md"}
assert actual == expected, (actual, expected)
context = next(m for m in mounts if m["Destination"] == "/run/pi/HOST_CONTEXT.md")
assert not context["RW"] and os.path.realpath(context["Source"]) == os.path.realpath(sys.argv[4])
assert "no-new-privileges:true" in item["HostConfig"]["SecurityOpt"]
assert item["HostConfig"]["CapDrop"] == ["ALL"]
PY
printf 'PASS trusted-live Docker mount, ownership, and boundary integration\n'
