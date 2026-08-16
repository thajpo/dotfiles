#!/usr/bin/env bash
set -u
root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"

# User scenario journeys: resume, conflict integration, multi-project,
# review-exact-revision loop, investigation-complete. The real-TTY approval
# scenarios are emitted by run-p6-installed.sh (tty-approve-execute-replay-
# refuse, command-request-without-approval envelopes).
bash tests/system/run-u-resume.sh || exit $?
bash tests/system/run-u-conflict.sh || exit $?
bash tests/system/run-u-multiproject.sh || exit $?
bash tests/system/run-u-review.sh || exit $?
bash tests/system/run-u-investigate.sh || exit $?
printf '%s\n' "PASS: all user scenario journeys completed"
exit 0
