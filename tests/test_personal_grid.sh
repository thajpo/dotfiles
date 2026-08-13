#!/usr/bin/env bash
# Grid mechanics test for the controller-backed pi-personal launcher.
#
# Uses an isolated tmux server and a stubbed pi-surface.py so no real Pi
# process or model provider is contacted. Verifies the personal surface uses
# the real personal conversation path (not workstream) and applies its own
# ordered active set independently from the secretary grid.
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
temporary=$(mktemp -d /tmp/personal-grid-test.XXXXXX)
cleanup() {
  TMUX_TMPDIR="$temporary/tmux" env -u TMUX tmux kill-server >/dev/null 2>&1 || true
  rm -rf "$temporary"
}
trap cleanup EXIT

mkdir -p "$temporary/bin" "$temporary/scripts" "$temporary/state" "$temporary/tmux"
chmod 700 "$temporary/state"
cp "$root/bin/pi-personal" "$temporary/bin/pi-personal"
chmod +x "$temporary/bin/pi-personal"

cat > "$temporary/scripts/pi-surface.py" <<'PY'
#!/usr/bin/env python3
"""Stub surface helper: canned controller answers, never touches the host."""
import json
import os
import sys

PROJECTS = [
    {"project_id": "prj_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1", "display_name": "alpha"},
    {"project_id": "prj_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa2", "display_name": "beta"},
]
CONVERSATIONS = {
    "prj_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1": "conv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb1",
    "prj_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa2": "conv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb2",
}
STATE_ROOT = os.environ["PERSONAL_GRID_STATE"]


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
        print(json.dumps({"dataRoot": "/tmp/personal-grid-data", "stateRoot": STATE_ROOT, "buildId": "build_grid"}))
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
    elif argv[0] == "personal-conversation":
        print(json.dumps({"conversation_id": CONVERSATIONS[argv[1]]}))
    elif argv[0] == "launch-argv-shell":
        if argv[1] == "personal":
            # The personal surface must launch the real personal launcher.
            print("bash -c 'sleep 300' -- --conversation-id %s --interactive" % argv[2])
        else:
            raise SystemExit(2)
    else:
        raise SystemExit(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
PY
chmod +x "$temporary/scripts/pi-surface.py"

export TMUX_TMPDIR="$temporary/tmux"
unset TMUX
export PERSONAL_GRID_STATE="$temporary/state"
tmux new-session -d -s scratch >/dev/null 2>&1

personal() { "$temporary/bin/pi-personal" "$@"; }

# Independent active set: personal shows only beta while the secretary grid
# (stored preference for pisec) is untouched.
python3 "$temporary/scripts/pi-surface.py" preference set pisec prj_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1 >/dev/null
personal activate beta >/dev/null 2>&1
personal list | grep -q "beta" || { echo "FAIL: beta missing from personal list"; exit 1; }
personal list | grep -q "^ .*alpha" || { echo "FAIL: alpha must be inactive in personal"; exit 1; }

# The personal launch must request the real personal role argv.
personal launch
window=$(tmux list-windows -t =pi-personal -F '#{window_name}' | grep -v '^shell$' | head -1)
[[ "$window" == "beta" ]] || { echo "FAIL: expected beta window, got: $window"; exit 1; }
argv=$(tmux list-panes -t "=pi-personal:$window" -F '#{pane_title}')
[[ "$argv" == "pi-personal conv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb2" ]] || { echo "FAIL: pane title mismatch: $argv"; exit 1; }

# Single owner on re-launch.
personal launch
count=$(tmux list-panes -a -F '#{session_name}' | grep -c '^pi-personal$')
(( count == 1 )) || { echo "FAIL: expected 1 pane, got $count"; exit 1; }

echo "pi-personal grid mechanics: ok"
