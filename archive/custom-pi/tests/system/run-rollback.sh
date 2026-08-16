#!/usr/bin/env bash
set -u
root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
status=0
PYTHONDONTWRITEBYTECODE=1 python3 -m tests.system.rollback_matrix >/dev/null
matrix=$?
if [[ $matrix -ne 0 ]]; then exit "$matrix"; fi
bash tests/pi-installer-transaction.sh
first=$?
if [[ $first -ne 0 ]]; then status=$first; fi
bash tests/pi-staged-artifact-rollback.sh
second=$?
if [[ $second -eq 1 || $second -eq 2 ]]; then status=$second
elif [[ $status -eq 0 && $second -eq 77 ]]; then status=77
fi
exit "$status"
