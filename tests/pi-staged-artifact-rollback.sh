#!/usr/bin/env bash
# Disposable Phase 5D staged-package/image/hash/rollback proof.
# It never activates a live path, tag, symlink, package tree, or controller DB.
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
package_root="$root/pi/packages/pi-sandbox-control"
image=${PI_CONTROL_PLANE_DOCKER_IMAGE:-pi-tool-sandbox:node22-bookworm-20260728}

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  printf 'NOT TESTED: Docker daemon is unavailable in this execution plane\n'
  exit 77
fi
if ! command -v npm >/dev/null 2>&1; then
  printf 'NOT TESTED: npm is unavailable in this execution plane\n'
  exit 77
fi
if ! docker image inspect "$image" >/dev/null 2>&1; then
  printf 'NOT TESTED: disposable image is unavailable: %s\n' "$image"
  exit 77
fi

staging=$(mktemp -d)
chmod 700 "$staging"
artifacts="$staging/artifacts"
mkdir -p "$artifacts"
staged_tag="pi-control-phase5d-staged-$$"
final_tag="pi-control-phase5d-final-$$"
rollback_tag="pi-control-phase5d-rollback-$$"
cleanup() {
  docker image rm "$final_tag" "$staged_tag" "$rollback_tag" >/dev/null 2>&1 || true
  rm -rf "$staging"
}
trap cleanup EXIT

# Package artifact: pack and install the exact first-party source without a
# relative file link, so the staged node_modules tree remains self-contained.
pack_json=$(cd "$package_root" && npm pack --ignore-scripts --pack-destination "$artifacts" --json)
tarball=$(python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["filename"])' <<<"$pack_json")
tarball="$artifacts/$tarball"
test -f "$tarball"
tarball_sha=$(sha256sum "$tarball" | awk '{print $1}')

python3 - "$package_root" "$staging/source-tree.json" <<'PY'
import hashlib, json, os, pathlib, stat, sys
source = pathlib.Path(sys.argv[1]).resolve()
out = pathlib.Path(sys.argv[2])
entries = []
for path in sorted(source.rglob("*")):
    if path.is_dir():
        continue
    relative = path.relative_to(source).as_posix()
    if path.is_symlink():
        entries.append({"path": relative, "kind": "symlink", "target": os.readlink(path)})
    else:
        entries.append({
            "path": relative,
            "kind": "file",
            "mode": stat.S_IMODE(path.stat().st_mode),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
if {entry["path"] for entry in entries} != {"LICENSE", "UPSTREAM.md", "package.json", "src/index.ts", "src/manifest-adapter.ts"}:
    raise SystemExit(f"unexpected package file set: {entries}")
body = {"schemaVersion": 1, "files": entries}
body["treeSha256"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
out.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")), encoding="utf-8")
os.chmod(out, 0o600)
PY
source_tree_sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["treeSha256"])' "$staging/source-tree.json")
package_lock_sha=$(sha256sum "$root/pi/npm/package-lock.json" | awk '{print $1}')
dockerfile_sha=$(sha256sum "$root/pi/sandbox/Dockerfile" | awk '{print $1}')

# Image artifact: use disposable tags and bind both staged and final tags to
# the exact immutable local image ID. Repository digests are recorded when
# the local image has them; a mutable tag alone is never accepted as identity.
base_image_id=$(docker image inspect --format '{{.Id}}' "$image")
repo_digests=$(docker image inspect --format '{{json .RepoDigests}}' "$image")
docker tag "$image" "$staged_tag"
staged_image_id=$(docker image inspect --format '{{.Id}}' "$staged_tag")
test "$staged_image_id" = "$base_image_id"
docker tag "$staged_tag" "$final_tag"
final_image_id=$(docker image inspect --format '{{.Id}}' "$final_tag")
test "$final_image_id" = "$staged_image_id"
# Re-run the disposable runtime proof against the exact staged image tag, not
# the mutable source tag. The staged image ID must remain unchanged afterward.
PI_CONTROL_PLANE_DOCKER_IMAGE="$staged_tag" "$root/tests/pi-docker-control-plane-e2e.sh" >"$staging/runtime-proof.txt"
grep -Fq 'PASS disposable Docker create, manifest labels, identity, mount, tool, and graceful stop attestation' "$staging/runtime-proof.txt"
test "$(docker image inspect --format '{{.Id}}' "$staged_tag")" = "$staged_image_id"

install_root="$staging/npm"
mkdir -p "$install_root"
cat > "$install_root/package.json" <<'JSON'
{"name":"pi-phase5d-staged-fixture","private":true,"dependencies":{}}
JSON
npm install --prefix "$install_root" --offline --ignore-scripts --no-audit --no-fund --no-package-lock --legacy-peer-deps "$tarball" >/dev/null
installed="$install_root/node_modules/pi-sandbox-control"
test -d "$installed"
test ! -L "$installed"
node --experimental-strip-types --check "$installed/src/index.ts"
node --experimental-strip-types --check "$installed/src/manifest-adapter.ts"
python3 - "$package_root" "$installed" <<'PY'
import hashlib, pathlib, sys
source = pathlib.Path(sys.argv[1])
installed = pathlib.Path(sys.argv[2])
expected = {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in source.rglob("*") if path.is_file() and not path.is_symlink()}
actual = {path.relative_to(installed).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in installed.rglob("*") if path.is_file() and not path.is_symlink()}
if actual != expected:
    raise SystemExit(f"installed package differs: expected={expected} actual={actual}")
PY
installed_tree_sha=$(python3 - "$installed" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
entries = []
for path in sorted(root.rglob("*")):
    if path.is_file() and not path.is_symlink():
        entries.append({"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
print(hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
PY
)

# Canonical build manifest and build ID. The manifest is staged with strict
# permissions and is independently recomputed before any rollback exercise.
manifest="$staging/artifact-manifest.json"
python3 - "$manifest" "$root" "$tarball_sha" "$source_tree_sha" "$installed_tree_sha" "$package_lock_sha" "$dockerfile_sha" "$image" "$base_image_id" "$staged_image_id" "$final_image_id" "$repo_digests" <<'PY'
import hashlib, json, os, pathlib, sys
(path, repo, tarball, source_tree, installed_tree, lock_sha, dockerfile, image, base_id, staged_id, final_id, repo_digests) = sys.argv[1:]
body = {
    "schemaVersion": 1,
    "artifact": "pi-sandbox-control",
    "packageVersion": json.loads(pathlib.Path(repo, "pi/packages/pi-sandbox-control/package.json").read_text())["version"],
    "packageTarballSha256": tarball,
    "packageSourceTreeSha256": source_tree,
    "installedTreeSha256": installed_tree,
    "packageLockSha256": lock_sha,
    "dockerfileSha256": dockerfile,
    "imageRef": image,
    "imageRepoDigests": json.loads(repo_digests),
    "baseImageId": base_id,
    "stagedImageId": staged_id,
    "finalImageId": final_id,
    "activation": "disposable-staging-only",
    "rollback": "disposable-staging-only",
}
canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
manifest = dict(body, buildId="sha256:" + hashlib.sha256(canonical).hexdigest())
pathlib.Path(path).write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")
os.chmod(path, 0o600)
PY
python3 - "$manifest" <<'PY'
import hashlib, json, os, pathlib, stat, sys
path = pathlib.Path(sys.argv[1])
if stat.S_IMODE(path.stat().st_mode) != 0o600:
    raise SystemExit("artifact manifest is not exactly 0600")
data = json.loads(path.read_text())
build_id = data.pop("buildId")
expected = "sha256:" + hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
if build_id != expected:
    raise SystemExit(f"build ID mismatch: {build_id} != {expected}")
PY

# Filesystem rollback: replace a disposable target, then restore the exact
# pre-activation bytes/modes/symlink and compare a canonical snapshot.
target="$staging/active"
backup="$staging/rollback/active"
candidate="$staging/candidate"
mkdir -p "$target" "$candidate" "$(dirname "$backup")"
printf 'old-generation\n' > "$target/generation.txt"
chmod 640 "$target/generation.txt"
ln -s generation.txt "$target/current"
python3 - "$target" "$staging/before.json" <<'PY'
import hashlib, json, os, pathlib, stat, sys
root = pathlib.Path(sys.argv[1])
entries = []
for path in sorted(root.rglob("*")):
    relative = path.relative_to(root).as_posix()
    if path.is_symlink(): entries.append({"path": relative, "kind": "symlink", "target": os.readlink(path)})
    elif path.is_file(): entries.append({"path": relative, "kind": "file", "mode": stat.S_IMODE(path.stat().st_mode), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
pathlib.Path(sys.argv[2]).write_text(json.dumps(entries, sort_keys=True, separators=(",", ":")), encoding="utf-8")
PY
cp -a "$installed" "$candidate/pi-sandbox-control"
mv "$target" "$backup"
mv "$candidate" "$target"
test -f "$target/pi-sandbox-control/src/index.ts"
rm -rf "$target"
mv "$backup" "$target"
python3 - "$target" "$staging/before.json" <<'PY'
import hashlib, json, os, pathlib, stat, sys
root = pathlib.Path(sys.argv[1])
actual = []
for path in sorted(root.rglob("*")):
    relative = path.relative_to(root).as_posix()
    if path.is_symlink(): actual.append({"path": relative, "kind": "symlink", "target": os.readlink(path)})
    elif path.is_file(): actual.append({"path": relative, "kind": "file", "mode": stat.S_IMODE(path.stat().st_mode), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
expected = json.loads(pathlib.Path(sys.argv[2]).read_text())
if actual != expected:
    raise SystemExit(f"rollback mismatch: expected={expected} actual={actual}")
PY

# Image rollback: move the disposable final tag to the staged candidate, then
# restore the old image ID and verify the exact ID before cleanup.
docker tag "$image" "$rollback_tag"
docker tag "$staged_tag" "$final_tag"
test "$(docker image inspect --format '{{.Id}}' "$final_tag")" = "$staged_image_id"
docker tag "$rollback_tag" "$final_tag"
test "$(docker image inspect --format '{{.Id}}' "$final_tag")" = "$base_image_id"
printf 'PASS staged package/image hashes, immutable final identity, and disposable filesystem/image rollback\n'
