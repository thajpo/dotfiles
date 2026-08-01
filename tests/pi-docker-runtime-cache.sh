#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  printf 'NOT TESTED: Docker daemon is unavailable in this execution plane\n'
  exit 77
fi
base_image=${PI_TEST_BASE_IMAGE:-pi-tool-sandbox:node22-bookworm-20260728}
docker image inspect "$base_image" >/dev/null 2>&1 || {
  printf 'NOT TESTED: base image is not built: %s\n' "$base_image"
  exit 77
}

root=$(mktemp -d)
images=()
cleanup() {
  for image in "${images[@]}"; do docker image rm "$image" >/dev/null 2>&1 || true; done
  rm -rf "$root"
}
trap cleanup EXIT

project="$root/project"
mkdir -p "$project"
cat > "$project/pyproject.toml" <<'TOML'
[project]
name = "pi-runtime-fixture"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[tool.uv]
package = false
TOML
(cd "$project" && uv lock >/dev/null)
mkdir -p "$project/.venv"
printf 'native-host-sentinel\n' > "$project/.venv/sentinel"
route="$root/route.json"
printf '{"worktree":"%s","image":"%s"}\n' "$project" "$base_image" > "$route"

first=$(python3 scripts/pi-runtime.py prepare --route "$route")
second=$(python3 scripts/pi-runtime.py prepare --route "$route")
first_image=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["image"])' "$first")
second_image=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["image"])' "$second")
first_key=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["environmentKey"])' "$first")
second_key=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["environmentKey"])' "$second")
test "$first_image" = "$second_image"
test "$first_key" = "$second_key"
images+=("$first_image")
docker run --rm --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$project,dst=/workspace,readonly=true" \
  --workdir /workspace "$first_image" \
  python -c 'import sys; assert sys.executable == "/opt/pi/env/bin/python"; print(sys.executable)'
test "$(cat "$project/.venv/sentinel")" = native-host-sentinel

sed -i.bak 's/version = "0.1.0"/version = "0.1.1"/' "$project/pyproject.toml"
(cd "$project" && UV_PROJECT_ENVIRONMENT="$root/lock-env" uv lock >/dev/null)
third=$(python3 scripts/pi-runtime.py prepare --route "$route")
third_image=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["image"])' "$third")
third_key=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["environmentKey"])' "$third")
test "$third_key" != "$first_key"
test "$third_image" != "$first_image"
images+=("$third_image")
test "$(cat "$project/.venv/sentinel")" = native-host-sentinel
printf 'PASS uv lock fingerprint reuse, changed-lock invalidation, Linux env, and host venv isolation\n'
