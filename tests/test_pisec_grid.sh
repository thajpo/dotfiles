#!/usr/bin/env bash
# Grid mechanics test for the controller-backed pisec launcher.
#
# Uses an isolated tmux server (TMUX_TMPDIR) and a stubbed pi-surface.py so no
# real Pi process or model provider is contacted. Verifies: active-set
# ordering, desktop window/pane grouping, mobile layout, dead-pane repair, and
# single-owner behavior (a live pane is never respawned).
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
temporary=$(mktemp -d /tmp/pisec-grid-test.XXXXXX)
cleanup() {
  TMUX_TMPDIR="$temporary/tmux" env -u TMUX tmux kill-server >/dev/null 2>&1 || true
  rm -rf "$temporary"
}
trap cleanup EXIT

mkdir -p "$temporary/bin" "$temporary/scripts" "$temporary/state" "$temporary/tmux"
chmod 700 "$temporary/state"
cp "$root/bin/pisec" "$temporary/bin/pisec"
chmod +x "$temporary/bin/pisec"

cat > "$temporary/scripts/pi-surface.py" <<'PY'
#!/usr/bin/env python3
"""Stub surface helper: canned controller answers, never touches the host."""
import json
import os
import sys

PROJECTS = [
    {"project_id": "prj_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1", "display_name": "alpha"},
    {"project_id": "prj_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa2", "display_name": "beta"},
    {"project_id": "prj_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa3", "display_name": "gamma"},
]
CONVERSATIONS = {
    "prj_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1": "conv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb1",
    "prj_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa2": "conv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb2",
    "prj_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa3": "conv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb3",
}
STATE_ROOT = os.environ["PISEC_GRID_STATE"]


def load_preference():
    try:
        value = json.load(open(os.path.join(STATE_ROOT, "preferences.json"), encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except OSError:
        return {}


def save_preference(value):
    with open(os.path.join(STATE_ROOT, "preferences.json"), "w", encoding="utf-8") as handle:
        handle.write(json.dumps(value))


def main(argv):
    if argv[0] == "env":
        print(json.dumps({"dataRoot": "/tmp/pisec-grid-data", "stateRoot": STATE_ROOT, "buildId": "build_grid"}))
    elif argv[0] == "project-list":
        print(json.dumps(PROJECTS))
    elif argv[0] == "preference":
        preference = load_preference()
        if argv[1] == "get":
            configured = argv[2] in preference
            print(json.dumps({"surface": argv[2], "activeProjectIds": preference.get(argv[2], []), "configured": configured}))
        else:
            preference[argv[2]] = argv[3:]
            save_preference(preference)
            print(json.dumps({"surface": argv[2], "activeProjectIds": preference[argv[2]]}))
    elif argv[0] == "secretary-conversation":
        print(json.dumps({"conversation_id": CONVERSATIONS[argv[1]]}))
    elif argv[0] == "launch-argv-shell":
        # Fake launcher argv containing "--" tokens (like the real launcher):
        # long-running sleep keeps the pane "live" without Pi.
        print("bash -c 'sleep 300' -- --conversation-id %s --interactive" % argv[2])
    else:
        raise SystemExit(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
PY
chmod +x "$temporary/scripts/pi-surface.py"

export TMUX_TMPDIR="$temporary/tmux"
unset TMUX
export PISEC_GRID_STATE="$temporary/state"
tmux new-session -d -s scratch >/dev/null 2>&1

pisec() { "$temporary/bin/pisec" "$@"; }

# Active set: alpha + gamma in that order (beta inactive).
pisec activate alpha gamma >/dev/null 2>&1

# Desktop launch: the active pair shares one window (alpha) with two
# side-by-side panes; the shell placeholder is removed.
pisec launch
tmux has-session -t =pisec || { echo "FAIL: pisec session missing"; exit 1; }
windows=$(tmux list-windows -t =pisec -F '#{window_name}' | sort | tr '\n' ' ')
[[ "$windows" == "alpha " ]] || { echo "FAIL: expected only alpha window, got: $windows"; exit 1; }

# Desktop grouping: the first two active projects share one window with two
# side-by-side panes.
pane_count=$(tmux list-panes -a -F '#{session_name}' | grep -c '^pisec$')
(( pane_count == 2 )) || { echo "FAIL: expected 2 panes for 2 active projects, got $pane_count"; exit 1; }

# Single-owner: a second launch must not duplicate live panes.
pisec launch
second_count=$(tmux list-panes -a -F '#{session_name}' | grep -c '^pisec$')
(( second_count == pane_count )) || { echo "FAIL: duplicate panes after re-launch ($second_count != $pane_count)"; exit 1; }

# Dead-pane repair: kill one pane process; launch must respawn it.
dead_pid=$(tmux list-panes -a -F '#{session_name}\t#{pane_pid}' | awk -F'\t' '$1=="pisec" {print $2; exit}')
kill "$dead_pid" 2>/dev/null || true
sleep 1
pisec launch
repaired=$(tmux list-panes -a -F '#{session_name}\t#{pane_dead}' | awk -F'\t' '$1=="pisec" && $2==1' | wc -l)
(( repaired == 0 )) || { echo "FAIL: dead pane not repaired"; exit 1; }

# Mobile layout: separate session per invocation context; kill the desktop
# grid, then launch mobile: one window per project, one pane each.
tmux kill-session -t =pisec
pisec --mobile launch
mobile_windows=$(tmux list-windows -t =pisec -F '#{window_name}' | sort | tr '\n' ' ')
[[ "$mobile_windows" == "alpha gamma " ]] || { echo "FAIL: mobile windows $mobile_windows"; exit 1; }
for window in alpha gamma; do
  count=$(tmux list-panes -t "=pisec:$window" | wc -l)
  (( count == 1 )) || { echo "FAIL: mobile window $window has $count panes"; exit 1; }
done

# Explicitly empty active set: activate --clear must mean an empty grid, not
# the all-projects fallback. Only the placeholder shell window remains.
tmux kill-session -t =pisec
pisec activate --clear >/dev/null 2>&1
pisec launch
empty_windows=$(tmux list-windows -t =pisec -F '#{window_name}' | sort | tr '\n' ' ')
[[ "$empty_windows" == "shell " ]] || { echo "FAIL: empty grid windows $empty_windows"; exit 1; }

# Re-activate by exact project id: resolution must accept ids and aliases.
projects_json=$(python3 "$temporary/scripts/pi-surface.py" project-list)
alpha_id=$(python3 -c "import json,sys; print(json.load(sys.stdin)[0]['project_id'])" <<<"$projects_json")
pisec activate "$alpha_id" gamma >/dev/null 2>&1
pisec launch
window=$(tmux list-windows -t =pisec -F '#{window_name}' | grep -v '^shell$' | head -1)
[[ "$window" == "alpha" ]] || { echo "FAIL: id activation window $window"; exit 1; }

# Stale preference ids must be dropped with a warning, not brick the surface.
python3 - "$temporary/state" <<'PY'
import json, os, sys
path = os.path.join(sys.argv[1], "preferences.json")
value = json.load(open(path, encoding="utf-8"))
value["pisec"] = ["prj_deadbeefdeadbeefdeadbeefdeadbeef", value["pisec"][1]]
with open(path, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(value))
PY
pisec list >/dev/null 2>"$temporary/stale.err"
grep -q "dropping stale active project" "$temporary/stale.err" || { echo "FAIL: stale id not reported"; exit 1; }
pisec launch >/dev/null 2>"$temporary/stale2.err"
grep -q "dropping stale active project" "$temporary/stale2.err" || { echo "FAIL: stale id not reported on launch"; exit 1; }

echo "pisec grid mechanics: ok"
