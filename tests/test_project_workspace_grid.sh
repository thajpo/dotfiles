#!/usr/bin/env bash
# Project workspace grid mechanics for the controller-backed pidev.
#
# Uses an isolated tmux server and a stubbed pi-surface.py. Verifies: pidev
# never creates a workstream, opens an editor-only home window when the
# project has no headful workstream, reconciles ws- windows when it does,
# and never duplicates the project session.
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
temporary=$(mktemp -d /tmp/project-workspace-test.XXXXXX)
cleanup() {
  TMUX_TMPDIR="$temporary/tmux" env -u TMUX tmux kill-server >/dev/null 2>&1 || true
  rm -rf "$temporary"
}
trap cleanup EXIT

mkdir -p "$temporary/bin" "$temporary/scripts" "$temporary/state" "$temporary/tmux" "$temporary/repo"
chmod 700 "$temporary/state"
cp "$root/bin/pidev" "$temporary/bin/pidev"
chmod +x "$temporary/bin/pidev"

export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid
git -C "$temporary/repo" init -q
echo hello > "$temporary/repo/README"
git -C "$temporary/repo" add README
git -C "$temporary/repo" commit -qm initial

cat > "$temporary/scripts/pi-surface.py" <<'PY'
#!/usr/bin/env python3
"""Stub surface helper: canned controller answers, never touches the host."""
import json
import os
import subprocess as sp
import sys

STATE_ROOT = os.environ["PROJECT_WORKSPACE_STATE"]
PROJECT_ID = "prj_" + "a" * 32


def tmux(*args):
    return sp.run(["tmux", *args], capture_output=True, text=True)


def workstreams_for(project_id):
    try:
        value = json.load(open(os.path.join(STATE_ROOT, "workstreams.json"), encoding="utf-8"))
        return value.get(project_id, [])
    except OSError:
        return []


def main(argv):
    if argv[0] == "env":
        print(json.dumps({"dataRoot": "/tmp/project-workspace-data", "stateRoot": STATE_ROOT, "buildId": "build_ws"}))
    elif argv[0] == "project-register":
        try:
            repo = argv[2]
        except IndexError:
            repo = "."
        open(os.path.join(STATE_ROOT, "repo-path"), "w").write(repo)
        print(json.dumps({"project_id": PROJECT_ID}))
    elif argv[0] == "present-project":
        project_id = argv[1]
        layout = "desktop"
        if argv[1] == "--layout":
            layout = argv[2]
            project_id = argv[3]
        repo = open(os.path.join(STATE_ROOT, "repo-path")).read().strip()
        common = sp.run(["git", "-C", repo, "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True).stdout.strip()
        import hashlib
        session = "pi-project-" + hashlib.sha256(common.encode()).hexdigest()[:12]
        workstreams = workstreams_for(project_id)
        if not workstreams:
            print(json.dumps({"surface": "project", "session": session, "present": [], "excluded": []}))
            return 0
        if tmux("has-session", "-t", "=%s" % session).returncode != 0:
            tmux("new-session", "-d", "-s", session, "-n", "shell")
        for index, ws in enumerate(workstreams, start=1):
            window = "ws-%s-%s" % (ws["title"], ws["id"][-8:])
            if window in tmux("list-windows", "-t", "=%s" % session, "-F", "#{window_name}").stdout.splitlines():
                continue
            tmux("new-window", "-d", "-t", "=%s" % session, "-n", window, "-c", ws["path"])
            tmux("send-keys", "-t", "=%s:%s" % (session, window), "nvim", "Enter")
            if layout == "desktop":
                tmux("split-window", "-h", "-t", "=%s:%s" % (session, window), "-c", ws["path"])
            tmux("send-keys", "-t", "=%s:%s" % (session, window), "exec bash -c 'sleep 300' -- --conversation-id %s --interactive" % ws["conversation"], "Enter")
            tmux("select-pane", "-t", "=%s:%s" % (session, window), "-T", "pi-workstream %s" % ws["conversation"])
        print(json.dumps({"surface": "project", "session": session, "present": [ws["conversation"] for ws in workstreams], "excluded": []}))
        return 0
    else:
        raise SystemExit(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
PY
chmod +x "$temporary/scripts/pi-surface.py"

export TMUX_TMPDIR="$temporary/tmux"
unset TMUX
export PROJECT_WORKSPACE_STATE="$temporary/state"
tmux new-session -d -s scratch >/dev/null 2>&1

pidev() { PIPEV_NO_ATTACH=1 "$temporary/bin/pidev" "$@"; }

# No workstream: pidev opens an editor-only home window and never creates
# a worktree or workstream.
pidev "$temporary/repo" >/dev/null 2>&1
session=$(tmux list-sessions -F '#{session_name}' | grep '^pi-project-' | head -1)
[[ -n "$session" ]] || { echo "FAIL: project session missing"; exit 1; }
tmux list-windows -t "=$session" -F '#{window_name}' | grep -qx home || { echo "FAIL: home window missing"; exit 1; }

# Reopen must not duplicate the session or the home window.
pidev "$temporary/repo" >/dev/null 2>&1
count=$(tmux list-sessions -F '#{session_name}' | grep -c '^pi-project-')
(( count == 1 )) || { echo "FAIL: duplicate project sessions ($count)"; exit 1; }
home_count=$(tmux list-windows -t "=$session" -F '#{window_name}' | grep -c '^home$')
(( home_count == 1 )) || { echo "FAIL: duplicate home windows ($home_count)"; exit 1; }

# With a headful workstream, pidev reconciles the ws- window instead and
# must not add a home window.
echo '{"prj_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": [{"title": "feature work", "id": "ws_deadbeefdeadbeef", "conversation": "conv_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb1", "path": "'"$temporary"'/repo"}]}' > "$temporary/state/workstreams.json"
# Close the editor window as a user would; reopening with a workstream must
# reconcile the ws- window and must not recreate the home window.
tmux kill-window -t "=$session:home"
pidev "$temporary/repo" >/dev/null 2>&1
tmux list-windows -t "=$session" -F '#{window_name}' | grep -q '^ws-' || { echo "FAIL: workstream window missing"; exit 1; }
tmux list-windows -t "=$session" -F '#{window_name}' | grep -qx home && { echo "FAIL: home window recreated despite workstream"; exit 1; }

echo "project workspace grid mechanics: ok"
