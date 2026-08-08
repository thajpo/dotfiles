#!/usr/bin/env bash
# Disposable Phase 5C manifest/runtime proof. It never touches a repository
# outside its temporary fixture and deliberately skips when Docker is absent.
set -euo pipefail

# Fail closed if this proof ever acquires a force-kill cleanup path.
if grep -nE 'docker[[:space:]]+rm[[:space:]]+-f|docker[[:space:]]+kill|kill[[:space:]]+-9|S[I]GKILL' "$0" >/dev/null; then
  echo 'forbidden force-kill behavior in disposable proof' >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  printf 'NOT TESTED: Docker daemon is unavailable in this execution plane\n'
  exit 77
fi

image=${PI_CONTROL_PLANE_DOCKER_IMAGE:-pi-tool-sandbox:node22-bookworm-20260728}
if ! docker image inspect "$image" >/dev/null 2>&1; then
  printf 'NOT TESTED: disposable image is unavailable: %s\n' "$image"
  exit 77
fi

root=$(mktemp -d)
container=""
cleanup() {
  if [[ -n "$container" ]]; then
    docker stop --time 10 "$container" >/dev/null 2>&1 || true
    docker rm "$container" >/dev/null 2>&1 || true
  fi
  rm -rf "$root"
}
trap cleanup EXIT

uid=$(id -u)
gid=$(id -g)
repo="$root/repo"
manifest="$root/run-manifest.json"
mkdir -p "$repo"
git -C "$repo" init -q -b main
git -C "$repo" config user.name 'Pi Phase5C Test'
git -C "$repo" config user.email pi-phase5c@example.invalid
printf 'base\n' > "$repo/tracked.txt"
git -C "$repo" add tracked.txt
git -C "$repo" commit -qm base
head_oid=$(git -C "$repo" rev-parse HEAD)
tree_oid=$(git -C "$repo" rev-parse 'HEAD^{tree}')
image_id=$(docker image inspect --format '{{.Id}}' "$image")
run_id=run_$(printf '1%.0s' {1..32})
project_id=prj_$(printf '2%.0s' {1..32})
working_copy_id=wc_$(printf '3%.0s' {1..32})
conversation_id=conv_$(printf '4%.0s' {1..32})
operation_id=op_$(printf '5%.0s' {1..32})
policy_hash=sha256:$(printf '6%.0s' {1..64})
runtime_hash=sha256:$(printf '7%.0s' {1..64})
capability_hash=sha256:$(printf '8%.0s' {1..64})
python3 - "$manifest" "$repo" "$head_oid" "$tree_oid" "$image_id" "$run_id" "$project_id" "$working_copy_id" "$conversation_id" "$operation_id" "$policy_hash" "$runtime_hash" "$capability_hash" "$uid" "$gid" <<'PY'
import hashlib, json, os, sys
(path, repo, head, tree, image_id, run_id, project_id, wc_id, conv_id, op_id, policy, runtime_hash, capability, uid, gid) = sys.argv[1:]
manifest = {
    "schemaVersion": 1, "runId": run_id, "operationId": op_id, "taskId": None,
    "conversationId": conv_id, "piSessionId": "pi-phase5c", "parentRunId": None,
    "project": {"projectId": project_id, "resourceVersion": 1, "objectFormat": "sha1", "trustMode": "trusted", "policyHash": policy},
    "workingCopy": {"workingCopyId": wc_id, "resourceVersion": 2, "kind": "primary", "purpose": "personal", "effectiveMode": "trusted-live", "hostPath": os.path.realpath(repo), "gitCommonDir": os.path.realpath(os.path.join(repo, ".git")), "gitDir": os.path.realpath(os.path.join(repo, ".git")), "branchRef": "refs/heads/main", "headOid": head, "treeOid": tree, "dirtyFingerprint": None, "writerEpoch": 1},
    "authority": "writer",
    "runtime": {"runtimeSpecVersion": 1, "runtimeSpecHash": runtime_hash, "executionTarget": "linux-container", "platform": "linux/amd64", "imageDigest": image_id, "controllerBuildId": "build_" + "9" * 32, "piVersion": "phase5c"},
    "owner": {"uid": int(uid), "gid": int(gid), "pid": os.getpid(), "processStartIdentity": "phase5c"},
    "capabilityHash": capability, "attestationNonce": "phase5c-attestation-nonce-abcdefghijklmnopqrstuvwxyz", "createdAt": "2024-01-01T00:00:00Z", "expiresAt": None, "manifestDigest": "",
}
content = dict(manifest); content.pop("manifestDigest")
manifest["manifestDigest"] = "sha256:" + hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
with open(path, "w", encoding="utf-8") as stream:
    json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
os.chmod(path, 0o600)
PY
manifest_digest=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["manifestDigest"])' "$manifest")
container="pi-runtime-$(python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:16])' "$run_id")"
controller_build_id="build_$(printf '9%.0s' {1..32})"
docker create --name "$container" \
  --label "pi.control.managed=true" \
  --label "pi.control.run-id=$run_id" \
  --label "pi.control.manifest-digest=$manifest_digest" \
  --label "pi.control.project-id=$project_id" \
  --label "pi.control.policy-hash=$policy_hash" \
  --label "pi.control.runtime-spec-hash=$runtime_hash" \
  --label "pi.control.controller-build-id=$controller_build_id" \
  --label "pi.control.working-copy-id=$working_copy_id" \
  --label "pi.control.writer-epoch=1" \
  --user "$uid:$gid" --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs "/tmp/pi-home:rw,nosuid,nodev,mode=0700,uid=$uid,gid=$gid" \
  --mount "type=bind,src=$repo,dst=/workspace,bind-propagation=rprivate" \
  -e "PI_RUN_ID=$run_id" -e "PI_MANIFEST_DIGEST=$manifest_digest" -e "PI_ATTESTATION_NONCE=phase5c-attestation-nonce-abcdefghijklmnopqrstuvwxyz" \
  -e "PI_PROJECT_ID=$project_id" -e "PI_WORKING_COPY_ID=$working_copy_id" \
  "$image" sleep infinity >/dev/null
docker start "$container" >/dev/null

INSPECT_JSON=$(docker inspect "$container") INSPECT_CONTAINER="$container" INSPECT_IMAGE_ID="$image_id" INSPECT_RUN_ID="$run_id" INSPECT_MANIFEST="$manifest_digest" INSPECT_PROJECT="$project_id" INSPECT_POLICY="$policy_hash" INSPECT_RUNTIME="$runtime_hash" INSPECT_CONTROLLER="$controller_build_id" INSPECT_WORKING="$working_copy_id" python3 - <<'PY'
import json, os
item = json.loads(os.environ["INSPECT_JSON"])[0]
assert item["Name"].lstrip("/") == os.environ["INSPECT_CONTAINER"]
assert item["State"]["Running"] is True
assert item["Image"] == os.environ["INSPECT_IMAGE_ID"]
assert item["Config"]["User"] == f"{os.getuid()}:{os.getgid()}"
labels = item["Config"].get("Labels") or {}
expected_labels = {
    "pi.control.managed": "true",
    "pi.control.run-id": os.environ["INSPECT_RUN_ID"],
    "pi.control.manifest-digest": os.environ["INSPECT_MANIFEST"],
    "pi.control.project-id": os.environ["INSPECT_PROJECT"],
    "pi.control.policy-hash": os.environ["INSPECT_POLICY"],
    "pi.control.runtime-spec-hash": os.environ["INSPECT_RUNTIME"],
    "pi.control.controller-build-id": os.environ["INSPECT_CONTROLLER"],
    "pi.control.working-copy-id": os.environ["INSPECT_WORKING"],
    "pi.control.writer-epoch": "1",
}
control_labels = {key: value for key, value in labels.items() if key.startswith("pi.control.")}
assert control_labels == expected_labels, (control_labels, expected_labels)
mounts = item["Mounts"]
workspace = next(m for m in mounts if m["Destination"] == "/workspace")
assert workspace["Type"] == "bind" and workspace["RW"] is True and workspace["Propagation"] == "rprivate"
host = item["HostConfig"]
actual_tmpfs = set(host.get("Tmpfs", {}).get("/tmp/pi-home", "").split(","))
expected_tmpfs = {"rw", "nosuid", "nodev", "mode=0700", f"uid={os.getuid()}", f"gid={os.getgid()}"}
assert actual_tmpfs == expected_tmpfs, (actual_tmpfs, expected_tmpfs)
assert host["CapDrop"] == ["ALL"] and not host.get("Privileged")
assert host["SecurityOpt"] == ["no-new-privileges:true"]
assert not host.get("PortBindings")
PY

docker exec "$container" sh -c 'test "$(id -u)" = "'"$uid"'" && test "$(id -g)" = "'"$gid"'" && test -w /workspace && test ! -e /var/run/docker.sock'
docker exec "$container" sh -c 'printf tool-proof > /workspace/tool-proof.txt'
test "$(cat "$repo/tool-proof.txt")" = tool-proof

docker stop --time 10 "$container" >/dev/null
if docker inspect --format '{{.State.Running}}' "$container" | grep -qx true; then
  echo 'container remained running after graceful stop' >&2
  exit 1
fi
printf 'PASS disposable Docker create, manifest labels, identity, mount, tool, and graceful stop attestation\n'
