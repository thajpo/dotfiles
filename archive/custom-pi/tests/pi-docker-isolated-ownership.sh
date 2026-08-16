#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  printf 'NOT TESTED: Docker daemon is unavailable in this execution plane\n'
  exit 77
fi

probe_root=$(mktemp -d)
container="pi-isolated-ownership-$$"
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  rm -rf "$probe_root"
}
trap cleanup EXIT

uid=$(id -u)
gid=$(id -g)
source_dir="$probe_root/source"
workspace=/Users/pi-test/projects/repository
mkdir -p "$source_dir/subdir"
printf 'tracked\n' > "$source_dir/tracked.txt"
printf 'nested\n' > "$source_dir/subdir/nested.txt"

docker create --name "$container" \
  --user "$uid:$gid" \
  --cap-drop ALL \
  --cap-add CHOWN \
  --security-opt no-new-privileges:true \
  --tmpfs "/tmp/pi-home:rw,nosuid,nodev,mode=0700,uid=$uid,gid=$gid" \
  pi-tool-sandbox:node22-bookworm-20260728 sleep infinity >/dev/null
docker start "$container" >/dev/null

# Root receives only CAP_CHOWN and uses it once to hand an empty directory to
# the unprivileged runtime identity. Project data is never recursively chowned.
docker exec -u root "$container" mkdir -p "$workspace"
docker exec -u root "$container" chown "$uid:$gid" "$workspace"
tar -cf - -C "$source_dir" . | docker exec -i "$container" tar --no-same-owner -xf - -C "$workspace"

test "$(docker exec "$container" sh -c 'id -u; id -g' | paste -sd: -)" = "$uid:$gid"
test "$(docker exec "$container" awk '/CapEff/{print $2}' /proc/self/status)" = 0000000000000000
test "$(docker exec -u root "$container" awk '/CapEff/{print $2}' /proc/self/status)" = 0000000000000001
test -z "$(docker exec "$container" find "$workspace" \! -user "$uid" -o \! -group "$gid")"
docker exec -w "$workspace" "$container" sh -c 'printf "first\n" > first-tool-call.txt'
docker exec -w "$workspace" "$container" sh -c 'printf "second\n" > second-tool-call.txt'
test "$(docker exec "$container" stat -c %u:%g "$workspace/second-tool-call.txt")" = "$uid:$gid"

inspect=$(docker inspect "$container")
INSPECT_JSON="$inspect" python3 - <<'PY'
import json
import os

item = json.loads(os.environ["INSPECT_JSON"])[0]
host = item["HostConfig"]
assert host["CapDrop"] == ["ALL"]
assert host["CapAdd"] == ["CAP_CHOWN"]
assert host["SecurityOpt"] == ["no-new-privileges:true"]
PY

printf 'PASS isolated workspace ownership and repeated tool-call execution\n'
