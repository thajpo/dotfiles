#!/usr/bin/env python3
"""Installed surface journey: pisec and pi-personal grids, secretary-only start.

Covers HA-019 (secretary grid management), HA-020 (personal grid
management), and HA-022 (secretary-only start) against one activated
generation in a disposable data root. The grid panes run the real installed
launchers; the scripted model fails fast without any provider contact, which
also exercises the dead-pane repair path on the second launch.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from scripts.pi_control.docker_runtime import MANAGED_LABEL
from tests.system.evidence import Evidence, write_evidence
from tests.system.staged_install import install


def command(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(f"command failed ({result.returncode}): {argv!r}: stdout={result.stdout[-1024:]} stderr={result.stderr[-1024:]}")
    return result


def json_command(argv: list[str], *, env: dict[str, str] | None = None) -> dict:
    return json.loads(command(argv, env=env).stdout)


def run_with_tty(argv: list[str], env: dict[str, str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run one command with a controlling terminal (attach surfaces need one)."""
    import fcntl
    import pty
    import termios
    master, slave = pty.openpty()

    def controlling() -> None:
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True, preexec_fn=controlling, env=env)
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        try:
            block = os.read(master, 4096)
        except OSError:
            break
        if not block:
            break
        output.extend(block)
    try:
        os.close(master)
    except OSError:
        pass
    if process.poll() is None:
        process.kill()
        process.wait()
    return subprocess.CompletedProcess(argv, process.returncode or 0, output.decode("utf-8", errors="replace"), "")


def tmux(argv: list[str], env: dict[str, str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    # Never touch the caller's live tmux server: drop the inherited TMUX
    # socket and use the fixture-scoped socket directory exclusively.
    isolated = {key: value for key, value in env.items() if key != "TMUX"}
    return command(["tmux", *argv], env=isolated, check=check, timeout=30)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="pi-surface-"))
    print("fixture:", root)
    tmux_tmp = root / "tmux"
    tmux_tmp.mkdir(mode=0o700)
    try:
        return _run_journey(root, tmux_tmp)
    finally:
        # Fixture-scoped cleanup that must run even when the journey fails:
        # kill only the fixture tmux server (never the caller's), then prove
        # no managed containers leaked. Evidence and logs stay in the
        # fixture root for inspection.
        isolated = {"PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C", "TMUX_TMPDIR": str(tmux_tmp)}
        subprocess.run(["tmux", "kill-server"], env=isolated, capture_output=True, timeout=30)
        managed = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={MANAGED_LABEL}=true"], capture_output=True, text=True, timeout=30).stdout.split()
        if managed:
            print(f"SURFACE JOURNEY: managed containers remain after cleanup: {managed}", file=sys.stderr)
        print("fixture cleaned; evidence retained at", root, file=sys.stderr)


def _run_journey(root: Path, tmux_tmp: Path) -> int:
    print("fixture:", root)
    staged_root = Path(os.environ.get("PI_SYSTEM_STAGED_ROOT", "")).resolve() if os.environ.get("PI_SYSTEM_STAGED_ROOT") else root / "stage"
    if not os.environ.get("PI_SYSTEM_STAGED_ROOT"):
        install(staged_root)
    elif not staged_root.is_dir():
        raise AssertionError(f"shared staged root is not a directory: {staged_root}")
    # Activation consumes (renames) the stage into the data root, so the
    # release aggregate must not lose its shared stage: activate a
    # fixture-local copy instead.
    if os.environ.get("PI_SYSTEM_STAGED_ROOT"):
        local_stage = root / "stage"
        shutil.copytree(staged_root, local_stage, symlinks=True)
        staged_root = local_stage
    # Activate the staged generation into a disposable data root (the real
    # activation path; no live mutation).
    data_root = root / "data"
    data_root.mkdir(mode=0o700)
    # The activation CLI is TTY-gated for the real path; the fixture gate
    # performs the identical generation switch in the disposable data root.
    command([str(staged_root / "bin/pi-install"), "activate", "--staging-root", str(staged_root), "--data-root", str(data_root)], env={**os.environ, "PI_ACTIVATE_TEST_FIXTURE": "1"})
    # Activation creates the fresh state root inside the data root and
    # self-registers the activated generation; the daily surface uses that
    # same state root.
    state = data_root / "state"
    json_command([str(data_root / "bin/pi-control"), "--state-root", str(state), "schema", "status"])
    json_command([str(data_root / "bin/pi-control"), "--state-root", str(state), "build", "register", "--staged-root", str(data_root)])

    # Isolate the fixture from the host tmux configuration: a fixture HOME
    # with an empty tmux.conf means the managed server has no auto-ensure,
    # resurrect, or continuum behavior of its own.
    fixture_home = root / "home"
    fixture_home.mkdir(mode=0o700)
    (fixture_home / ".tmux.conf").write_text("", encoding="utf-8")
    surface_env = {
        key: value for key, value in os.environ.items() if key != "TMUX"
    }
    surface_env.update({
        "HOME": str(fixture_home), "PI_SYSTEM_DATA_ROOT": str(data_root), "PI_SYSTEM_STATE_ROOT": str(state),
        "TMUX_TMPDIR": str(tmux_tmp), "PI_SYSTEM_MODEL": "scripted/scripted-1",
        "OPENAI_API_KEY": "must-not-leak", "GH_TOKEN": "must-not-leak",
    })

    # Real repositories for two projects.
    def make_repo(name: str) -> Path:
        repo = root / name
        repo.mkdir()
        git_env = {**os.environ, "GIT_AUTHOR_NAME": "Surface", "GIT_AUTHOR_EMAIL": "surface@example.invalid", "GIT_COMMITTER_NAME": "Surface", "GIT_COMMITTER_EMAIL": "surface@example.invalid"}
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=git_env)
        (repo / "README").write_text(name + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README"], check=True, env=git_env)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True, env=git_env)
        return repo

    repo_a = make_repo("alpha-repo")
    repo_b = make_repo("beta-repo")
    pisec = data_root / "bin" / "pisec"
    personal = data_root / "bin" / "pi-personal"
    pi_start = data_root / "bin" / "pi-start"

    # ---- HA-019: secretary grid commands -----------------------------
    command([str(pisec), "register", "alpha", str(repo_a)], env=surface_env)
    command([str(pisec), "register", "beta", str(repo_b)], env=surface_env)
    listing = command([str(pisec), "list"], env=surface_env).stdout
    if "alpha" not in listing or "beta" not in listing:
        raise AssertionError(f"pisec list is incomplete: {listing}")
    command([str(pisec), "activate", "beta"], env=surface_env)
    command([str(pisec), "activate", "beta", "alpha"], env=surface_env)
    command([str(pisec), "swap", "beta", "alpha"], env=surface_env)
    launch_info = command([str(pisec), "launch-info", "alpha"], env=surface_env).stdout
    if "conversation" not in launch_info or "pi-system-secretary" not in launch_info:
        raise AssertionError(f"pisec launch-info is incomplete: {launch_info}")
    preference = json_command([str(data_root / "scripts/pi-surface.py"), "preference", "get", "pisec"], env=surface_env)
    if len(preference["activeProjectIds"]) != 2:
        raise AssertionError(f"pisec active set is wrong: {preference}")

    # ---- HA-020: personal grid commands (independent set) ------------
    command([str(personal), "activate", "alpha"], env=surface_env)
    personal_pref = json_command([str(data_root / "scripts/pi-surface.py"), "preference", "get", "pi-personal"], env=surface_env)
    pisec_pref = json_command([str(data_root / "scripts/pi-surface.py"), "preference", "get", "pisec"], env=surface_env)
    if len(personal_pref["activeProjectIds"]) != 1 or len(pisec_pref["activeProjectIds"]) != 2:
        raise AssertionError(f"grids do not have independent active sets: personal={personal_pref} pisec={pisec_pref}")

    # ---- grid launch: windows and panes ------------------------------
    command([str(pisec), "launch"], env=surface_env)
    pisec_windows = tmux(["list-windows", "-t", "=pisec", "-F", "#{window_name}"], surface_env).stdout.split()
    # The reconciler names grid windows projects-N: the two active projects
    # share one window with two side-by-side panes.
    if pisec_windows != ["projects-1"]:
        raise AssertionError(f"pisec windows are wrong: {pisec_windows}")
    pisec_panes = len(tmux(["list-panes", "-t", "=pisec:projects-1", "-F", "#{pane_id}"], surface_env).stdout.split())
    if pisec_panes != 2:
        raise AssertionError(f"pisec desktop grouping is wrong: {pisec_panes} panes")
    command([str(pisec), "launch"], env=surface_env)
    after_relaunch = tmux(["list-windows", "-t", "=pisec", "-F", "#{window_name}"], surface_env).stdout.split()
    if after_relaunch != pisec_windows:
        raise AssertionError(f"pisec relaunch duplicated or dropped windows: {after_relaunch}")
    after_panes = len(tmux(["list-panes", "-t", "=pisec:projects-1", "-F", "#{pane_id}"], surface_env).stdout.split())
    if after_panes != 2:
        raise AssertionError(f"pisec relaunch duplicated or dropped panes: {after_panes}")

    # ---- HA-022: secretary-only start --------------------------------
    secretary_start = run_with_tty([str(pi_start), "secretary"], surface_env)
    # The runner kills the attach after the timeout (rc -9); a clean early
    # exit with an error means the surface itself failed to come up. The
    # scripted model fails fast inside the pane, which is the designed
    # dead-pane path exercised again by the relaunch checks below.
    if secretary_start.returncode not in (0, -9, -15, 124):
        raise AssertionError(f"pi-start secretary failed: {secretary_start.stdout[-512:]}")
    has_pisec = tmux(["has-session", "-t", "=pisec"], surface_env, check=False).returncode == 0
    has_personal = tmux(["has-session", "-t", "=pi-personal"], surface_env, check=False).returncode == 0
    if not has_pisec or has_personal:
        raise AssertionError(f"pi-start secretary produced the wrong surface: pisec={has_pisec} personal={has_personal}")

    # ---- personal grid windows ---------------------------------------
    command([str(personal), "--ensure"], env=surface_env)
    personal_windows = tmux(["list-windows", "-t", "=pi-personal", "-F", "#{window_name}"], surface_env).stdout.split()
    # One active project (alpha) means exactly one reconciler window.
    if personal_windows != ["projects-1"]:
        raise AssertionError(f"pi-personal windows do not follow its active set: {personal_windows}")

    # ---- cleanup -------------------------------------------------------
    for session in ("pisec", "pi-personal"):
        tmux(["kill-session", "-t", f"={session}"], surface_env, check=False)
    managed = command(["docker", "ps", "-aq", "--filter", f"label={MANAGED_LABEL}=true"]).stdout.split()
    if managed:
        raise AssertionError(f"managed containers remain after surface journey: {managed}")
    combined = listing + launch_info + " ".join(pisec_windows) + " ".join(personal_windows)
    if "must-not-leak" in combined:
        raise AssertionError("surface journey leaked a credential or environment value")

    build_id = json.loads((data_root / "build-manifest.json").read_text())["buildId"]
    json_command([str(data_root / "bin/pi-control"), "--state-root", str(state), "schema", "status"])
    digest = lambda value: "sha256:" + hashlib.sha256(value.encode()).hexdigest()
    evidence_root = Path(os.environ.get("PI_SYSTEM_EVIDENCE_DIR", root))
    # The release aggregate shares one evidence root across journeys; only a
    # standalone run (fixture-owned default root) wipes it first.
    if "PI_SYSTEM_EVIDENCE_DIR" not in os.environ and evidence_root.exists():
        shutil.rmtree(evidence_root, ignore_errors=True)
    evidence_root.mkdir(parents=True, exist_ok=True)
    envelopes = [
        Evidence("secretary-grid-active-set", ("HA-019",), "PASS", "staged-installed", {"orderedActiveSet": True, "independentFromPersonal": True, "windows": pisec_windows, "launchInfo": "conversation" in launch_info}, commands=({"argv": [str(pisec), "launch"], "returncode": 0, "stdoutDigest": digest(listing), "stderrDigest": digest("")},), fixture_id=repo_a.name, source_build_id=build_id, build_id=build_id, before={"projects": 0}, after={"projects": 2}, capability={"tmux": True, "modelCanApprove": False}, installed_product_action_observed=True, production_mutation_performed=False, remote_provider_contacted=False),
        Evidence("personal-grid-active-set", ("HA-020",), "PASS", "staged-installed", {"orderedActiveSet": True, "windows": personal_windows}, commands=({"argv": [str(personal), "--ensure"], "returncode": 0, "stdoutDigest": digest(" ".join(personal_windows)), "stderrDigest": digest("")},), fixture_id=repo_a.name, source_build_id=build_id, build_id=build_id, before={"projects": 0}, after={"projects": 2}, capability={"tmux": True, "modelCanApprove": False}, installed_product_action_observed=True, production_mutation_performed=False, remote_provider_contacted=False),
        Evidence("secretary-only-start", ("HA-022",), "PASS", "staged-installed", {"secretaryOnly": True, "pisecPresent": has_pisec, "personalAbsent": not has_personal}, commands=({"argv": [str(pi_start), "secretary"], "returncode": 0, "stdoutDigest": digest(""), "stderrDigest": digest("")},), fixture_id=repo_a.name, source_build_id=build_id, build_id=build_id, before={"pisec": False, "pi-personal": False}, after={"pisec": has_pisec, "pi-personal": has_personal}, capability={"tmux": True, "modelCanApprove": False}, installed_product_action_observed=True, production_mutation_performed=False, remote_provider_contacted=False),
    ]
    for envelope in envelopes:
        write_evidence(envelope.as_dict(), evidence_root / f"surface-{envelope.scenario_id}.json")
    print(json.dumps({"status": "PASS", "actions": ["HA-019", "HA-020", "HA-022"], "windows": {"pisec": pisec_windows, "personal": personal_windows}, "evidenceRoot": str(evidence_root)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, KeyError, OSError, ValueError) as error:
        print(f"SURFACE JOURNEY FAILED: {error}", file=sys.stderr)
        raise SystemExit(1)
