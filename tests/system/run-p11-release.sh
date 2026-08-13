#!/usr/bin/env bash
set -u
root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"

if [[ -z "${PI_SYSTEM_STAGED_ROOT:-}" ]]; then
  printf '%s\n' 'STOP/77: P11 requires an existing staged build via PI_SYSTEM_STAGED_ROOT' >&2
  exit 77
fi

evidence_root=${PI_SYSTEM_EVIDENCE_DIR:-$(mktemp -d)}
mkdir -p "$evidence_root"
evidence_root=$(realpath "$evidence_root")
chmod 700 "$evidence_root"

opencode_before="$evidence_root/opencode/opencode-before.json"
opencode_after="$evidence_root/opencode/opencode-after.json"
opencode_result="$evidence_root/opencode/opencode-result.json"
mkdir -p "$evidence_root/opencode"

# Guard before: record OpenCode state, verify launchable.
python3 tests/system/fixtures/opencode_guard.py --before "$opencode_before" --output "$opencode_result" || exit $?

# Every installed journey against the SAME staged build, evidence into one root.
export PI_SYSTEM_STAGED_ROOT PI_SYSTEM_EVIDENCE_DIR="$evidence_root"
bash tests/system/run-staged-installed.sh || exit $?
bash tests/system/run-docker.sh || exit $?
bash tests/system/run-p6-installed.sh || exit $?
bash tests/system/run-p7-installed.sh || exit $?
bash tests/system/run-p9-installed.sh || exit $?
bash tests/system/run-p10-installed.sh || exit $?
bash tests/system/run-u-scenarios.sh || exit $?
bash tests/system/run-p12-activation.sh || exit $?
# Repair and surface journeys on the same staged build. The surface journey
# activates (consuming) the stage, so it runs after every other journey.
bash tests/system/run-repair-installed.sh || exit $?
bash tests/system/run-repair-surface.sh || exit $?
unset PI_SYSTEM_EVIDENCE_DIR

# Guard after: OpenCode must be unchanged and launchable.
python3 tests/system/fixtures/opencode_guard.py --before "$opencode_before" --after "$opencode_after" --output "$opencode_result" || exit $?

# Aggregate and require every action covered.
python3 tests/system/p11_release_verify.py --evidence-dir "$evidence_root" || exit $?

printf '%s\n' "PASS: full installed journey completed twice on one build; evidence: $evidence_root"
exit 0
