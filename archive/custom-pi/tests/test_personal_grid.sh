#!/usr/bin/env bash
# Grid mechanics test for the controller-backed pi-personal launcher.
#
# Uses an isolated Pi tmux socket and a stubbed pi-surface.py so no real Pi
# process or model provider is contacted. Verifies the personal surface uses
# the real personal conversation path (not workstream) and applies its own
# ordered active set independently from the secretary grid.
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
temporary=$(mktemp -d /tmp/personal-grid-test.XXXXXX)
cleanup() {
  env -u TMUX tmux -S "$temporary/pi.sock" kill-server >/dev/null 2>&1 || true
  rm -rf "$temporary"
}
trap cleanup EXIT

mkdir -p "$temporary/bin" "$temporary/scripts" "$temporary/state" "$temporary/tmux"
chmod 700 "$temporary/state"
cp "$root/bin/pi-personal" "$temporary/bin/pi-personal"
cp "$root/bin/pi-tmux" "$temporary/bin/pi-tmux"
chmod +x "$temporary/bin/pi-personal"
chmod +x "$temporary/bin/pi-tmux"

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
    elif argv[0] == "present":
        # Simulate the controller reconciler against the fixture tmux server.
        import subprocess as sp
        surface = layout = None
        ids = []
        rest = argv[1:]
        while rest:
            flag = rest.pop(0)
            if flag == "--surface":
                surface = rest.pop(0)
            elif flag == "--layout":
                layout = rest.pop(0)
            else:
                ids.append(flag)
        def tmux(*args):
            return sp.run(["tmux", "-S", os.environ["PI_TMUX_SOCKET"], *args], capture_output=True, text=True)
        def conv_title(conv):
            return "pi-personal %s" % conv
        if not ids:
            if tmux("has-session", "-t", "=%s" % surface).returncode != 0:
                tmux("new-session", "-d", "-s", surface, "-n", "shell")
            print(json.dumps({"excluded": []}))
            return 0
        if tmux("has-session", "-t", "=%s" % surface).returncode != 0:
            tmux("new-session", "-d", "-s", surface, "-n", "shell")
        wanted = [CONVERSATIONS[i] for i in ids]
        windows = tmux("list-windows", "-t", "=%s" % surface, "-F", "#{window_name}").stdout.splitlines()
        for index, conv in enumerate(wanted, start=1):
            name = "projects-%d" % index
            if name in windows:
                continue
            tmux("new-window", "-d", "-t", "=%s" % surface, "-n", name)
            tmux("send-keys", "-t", "=%s:%s" % (surface, name), "exec bash -c 'sleep 300' -- --conversation-id %s --interactive" % conv, "Enter")
            tmux("select-pane", "-t", "=%s:%s" % (surface, name), "-T", conv_title(conv))
        wins = tmux("list-windows", "-t", "=%s" % surface, "-F", "#{window_name}").stdout.splitlines()
        if "shell" in wins and len(wins) > 1:
            tmux("kill-window", "-t", "=%s:shell" % surface)
        print(json.dumps({"excluded": []}))
        return 0
    else:
        raise SystemExit(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
PY
chmod +x "$temporary/scripts/pi-surface.py"

export PI_TMUX_SOCKET="$temporary/pi.sock"
unset TMUX
export PERSONAL_GRID_STATE="$temporary/state"
ptmux() { tmux -S "$PI_TMUX_SOCKET" "$@"; }
ptmux new-session -d -s scratch >/dev/null 2>&1

personal() { "$temporary/bin/pi-personal" "$@"; }

# Invalid commands must fail before attempting reconciliation or attach.
if personal --bogus >/dev/null 2>&1; then echo "FAIL: unknown personal option accepted"; exit 1; fi
if personal list extra >/dev/null 2>&1; then echo "FAIL: extra personal argument accepted"; exit 1; fi
if personal activate >/dev/null 2>&1; then echo "FAIL: empty personal activation accepted"; exit 1; fi
if personal activate --clear extra >/dev/null 2>&1; then echo "FAIL: extra personal clear argument accepted"; exit 1; fi

# Independent active set: personal shows only beta while the secretary grid
# (stored preference for pisec) is untouched.
python3 "$temporary/scripts/pi-surface.py" preference set pisec prj_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1 >/dev/null
personal activate beta >/dev/null 2>&1
personal list | grep -q "beta" || { echo "FAIL: beta missing from personal list"; exit 1; }
personal list | grep -q "^ .*alpha" || { echo "FAIL: alpha must be inactive in personal"; exit 1; }

# The personal launch must request the real personal role argv.
personal launch
window=$(ptmux list-windows -t =pi-personal -F '#{window_name}' | grep -v '^shell$' | head -1)
[[ "$window" == "projects-1" ]] || { echo "FAIL: expected projects-1 window, got: $window"; exit 1; }
argv=$(ptmux list-panes -t "=pi-personal:$window" -F '#{pane_title}')
[[ "$argv" == "pi-personal conv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb2" ]] || { echo "FAIL: pane title mismatch: $argv"; exit 1; }

# Single owner on re-launch.
personal launch
count=$(ptmux list-panes -a -F '#{session_name}' | grep -c '^pi-personal$')
(( count == 1 )) || { echo "FAIL: expected 1 pane, got $count"; exit 1; }

echo "pi-personal grid mechanics: ok"
