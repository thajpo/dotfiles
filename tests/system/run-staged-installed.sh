#!/usr/bin/env bash
set -u
root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
node "$root/tests/pi-subagents-control-provenance.mjs" || exit $?
node "$root/tests/pi-sandbox-control-manifest.test.mjs" || exit $?
NODE_PATH="$root/pi/npm/node_modules" node "$root/tests/pi-sandbox-control-broker.test.mjs" || exit $?
if ! command -v npm >/dev/null 2>&1; then
  printf '%s\n' 'STOP/77: npm is unavailable for staged installation' >&2
  exit 77
fi
if ! python3 -c 'import jsonschema' >/dev/null 2>&1; then
  printf '%s\n' 'STOP/77: Python jsonschema is unavailable for evidence validation' >&2
  exit 77
fi
node_bin=$(command -v node || true)
if [[ -z "$node_bin" || ! -x "$node_bin" ]]; then
  printf '%s\n' 'STOP/77: Node.js is unavailable for staged installation' >&2
  exit 77
fi
expected_version=$(<"$root/pi/PI_VERSION")
stage_cleanup=""
stages=()
build_ids=()
manifest_digests=()
if [[ -n "${PI_SYSTEM_STAGED_ROOT:-}" ]]; then
  stages+=("$PI_SYSTEM_STAGED_ROOT" "$PI_SYSTEM_STAGED_ROOT")
  PI_SYSTEM_LOADED_BUILD_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["buildId"])' "$PI_SYSTEM_STAGED_ROOT/build-manifest.json")
  build_ids+=("$PI_SYSTEM_LOADED_BUILD_ID" "$PI_SYSTEM_LOADED_BUILD_ID")
  manifest_digest=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["manifestDigest"])' "$PI_SYSTEM_STAGED_ROOT/build-manifest.json")
  manifest_digests+=("$manifest_digest" "$manifest_digest")
else
  stage_cleanup=$(mktemp -d)
  trap 'rm -rf "$stage_cleanup"' EXIT
  for attempt in 1 2; do
    stage_root="$stage_cleanup/stage-$attempt"
    info="$stage_cleanup/info-$attempt.json"
    PYTHONDONTWRITEBYTECODE=1 python3 -m tests.system.staged_install --output-root "$stage_root" >"$info" || exit $?
    stages+=("$stage_root")
    build_ids+=("$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["buildId"])' "$info")")
    manifest_digests+=("$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["manifestDigest"])' "$info")")
  done
  if [[ "${build_ids[0]}" != "${build_ids[1]}" || "${manifest_digests[0]}" != "${manifest_digests[1]}" ]]; then
    printf '%s\n' 'FAIL: two production generation builds are not deterministic' >&2
    exit 1
  fi
fi

journey_cleanup=$(mktemp -d)
cleanup_staged_journey() {
  if [[ -n "${journey_cleanup:-}" && -d "$journey_cleanup" ]]; then
    chmod -R u+w "$journey_cleanup" 2>/dev/null || true
  fi
  rm -rf "$journey_cleanup" "$stage_cleanup"
}
if [[ -n "${PI_SYSTEM_EVIDENCE_DIR:-}" ]]; then
  evidence_root=$PI_SYSTEM_EVIDENCE_DIR
  mkdir -p "$evidence_root"
else
  evidence_root=$(mktemp -d)
fi
evidence_root=$(realpath "$evidence_root")
case "$evidence_root/" in
  "$root/"*) printf '%s\n' 'FAIL: installed-process evidence directory must be outside the repository' >&2; exit 1 ;;
esac
chmod 700 "$evidence_root"
trap cleanup_staged_journey EXIT
for attempt in 1 2; do
  index=$((attempt - 1))
  staged_root=${stages[$index]}
  loaded_build_id=${build_ids[$index]}
  PI_SYSTEM_LOADED_BUILD_ID=$loaded_build_id PYTHONDONTWRITEBYTECODE=1 python3 -m tests.system.staged_proof --root "$staged_root" || exit $?
  pi_cli="$staged_root/runtime/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"
  controller="$staged_root/bin/pi-control"
  launcher="$staged_root/bin/pi-system-secretary"
  driver="$root/tests/system/fixtures/installed-pi.py"
  provider="$root/tests/system/fixtures/scripted-provider.ts"
  probe="$root/tests/system/loaded_resource_probe.ts"
  scoped_extension="$staged_root/pi/extensions/scoped-project-read/index.ts"
  if [[ ! -x "$pi_cli" || ! -x "$controller" || ! -x "$launcher" || ! -f "$driver" || ! -f "$provider" || ! -f "$probe" || ! -f "$scoped_extension" ]]; then
    printf '%s\n' 'FAIL: staged Pi product or external test harness resources are incomplete' >&2
    exit 1
  fi
  if [[ "$($node_bin "$pi_cli" --version 2>/dev/null || true)" != "$expected_version" ]]; then
    printf '%s\n' 'FAIL: staged Pi version does not match the reviewed version' >&2
    exit 1
  fi
  journey="$journey_cleanup/run-$attempt"
  repository="$journey/repository"
  state="$journey/state"
  mkdir -p "$repository" "$state" "$journey/evidence"
  chmod 700 "$journey" "$repository" "$state" "$journey/evidence"
  git init -q -b main "$repository" || exit $?
  git -C "$repository" config user.name 'Pi System Fixture'
  git -C "$repository" config user.email 'fixture@example.invalid'
  printf '%s\n' 'installed process' >"$repository/README"
  git -C "$repository" add README
  GIT_AUTHOR_DATE='2024-01-01T00:00:00Z' GIT_COMMITTER_DATE='2024-01-01T00:00:00Z' git -C "$repository" commit -qm fixture
  "$controller" --state-root "$state" build register --staged-root "$staged_root" >"$journey/build.json" || exit $?
  registered_build_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["build_id"])' "$journey/build.json")
  if [[ "$registered_build_id" != "$loaded_build_id" ]]; then
    printf '%s\n' 'FAIL: registered staged build identity differs from loaded generation' >&2
    exit 1
  fi
  "$controller" --state-root "$state" project register --repository "$repository" >"$journey/project.json" || exit $?
  project_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["project_id"])' "$journey/project.json")
  "$controller" --state-root "$state" project status "$project_id" >"$journey/status.json" || exit $?
  working_copy_id=$(python3 -c 'import json,sys; v=json.load(open(sys.argv[1])); print(next(x["working_copy_id"] for x in v["workingCopies"] if x["kind"]=="primary"))' "$journey/status.json")
  conversation_id=$(python3 -c 'import json,sys; v=json.load(open(sys.argv[1])); print(next(x["conversation_id"] for x in v["conversations"] if x["role"]=="secretary"))' "$journey/status.json")
  pi_session_id=$(python3 -c 'import json,sys; v=json.load(open(sys.argv[1])); print(next(x["pi_session_id"] for x in v["conversations"] if x["role"]=="secretary"))' "$journey/status.json")
  session_file=$(python3 -c 'import json,sys; v=json.load(open(sys.argv[1])); print(next(x["session_file"] for x in v["conversations"] if x["role"]=="secretary"))' "$journey/status.json")
  python3 "$driver" \
    --launcher "$launcher" \
    --state-root "$state" --project-id "$project_id" --working-copy-id "$working_copy_id" \
    --conversation-id "$conversation_id" --pi-session-id "$pi_session_id" --session-file "$session_file" \
    --provider "$provider" --probe "$probe" \
    --repository "$repository" --build-id "$loaded_build_id" \
    --staged-root "$staged_root" \
    --evidence "$evidence_root/run-$attempt.json" >"$journey/result.json" || exit $?
  PYTHONDONTWRITEBYTECODE=1 python3 -c 'import json,sys; from jsonschema import Draft202012Validator; from tests.system.evidence import validate_evidence; value=json.load(open(sys.argv[1])); schema=json.load(open("tests/system/evidence.schema.json")); Draft202012Validator.check_schema(schema); Draft202012Validator(schema).validate(value); validate_evidence(value)' "$evidence_root/run-$attempt.json" || exit $?
done
printf '%s\n' "PASS: two identical production builds completed the staged installed Pi journey; evidence: $evidence_root"
exit 0
