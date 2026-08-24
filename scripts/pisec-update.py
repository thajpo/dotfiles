#!/usr/bin/env python3
"""Install one committed Pisec bundle without importing the active bundle."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

SCHEMA_VERSION = 1
EXIT_FAILED = 1
EXIT_NEEDS_ATTENTION = 2
EXIT_UNSUPPORTED_STATE = 3
EXIT_LOCKED = 75
ALLOWLIST = (
    "bin/pisec",
    "scripts/pisec/",
    "scripts/pisec-update.py",
    "pisec/",
    "omp/extensions/",
    "omp/agents/",
    "agent/AGENTS.md",
    "skills/",
    "herdr/plugins/pisec/",
    "systemd/user/",
)
REQUIRED = ("bin/pisec", "scripts/pisec-update.py", "scripts/pisec", "pisec")
EPOCH15_DIGEST = "sha256:912a55b54f861a9715676baf4d0d86c8762b0236232204e175ea3f62ee976dd2"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _run(args: list[str], *, cwd: Path | None = None) -> str:
    return subprocess.check_output(args, cwd=str(cwd) if cwd else None, stderr=subprocess.STDOUT, text=True).strip()


def _git(repo: Path, *args: str) -> str:
    return _run(["git", "-C", str(repo), *args])


def _safe_tree(root: Path) -> None:
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError(f"unsafe bundle ownership or mode: {path}")
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise RuntimeError(f"unsupported bundle entry: {path}")
        if stat.S_ISDIR(info.st_mode):
            os.chmod(path, 0o700)
        else:
            os.chmod(path, 0o700 if info.st_mode & stat.S_IXUSR else 0o600)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"bundle contains an unsupported entry: {relative}")
        digest.update(relative.encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _allowed(name: str) -> bool:
    return any(name == prefix.rstrip("/") or name.startswith(prefix) for prefix in ALLOWLIST)


def _archive(repo: Path, ref: str, destination: Path) -> tuple[str, str, str]:
    commit = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    command = ["git", "-C", str(repo), "archive", "--format=tar", commit, *ALLOWLIST]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
        seen: set[str] = set()
        for member in archive:
            name = member.name.rstrip("/")
            if not name or not _allowed(name) or name.startswith("/") or ".." in Path(name).parts:
                process.kill()
                raise RuntimeError(f"bundle member is outside the allowlist: {name}")
            if member.issym() or member.islnk() or not (member.isdir() or member.isreg()):
                raise RuntimeError(f"bundle member is unsafe: {name}")
            seen.add(name)
            target = destination / name
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"bundle member is unreadable: {name}")
            with target.open("wb") as output:
                shutil.copyfileobj(source, output)
    error = process.wait()
    if error:
        raise RuntimeError(f"git archive failed with status {error}")
    for required in REQUIRED:
        if not (destination / required).exists():
            raise RuntimeError(f"bundle is missing required path: {required}")
    _safe_tree(destination)
    return commit, tree, _tree_digest(destination)


def _preflight_state(state_root: Path) -> None:
    db_path = state_root / "control.db"
    if not db_path.exists():
        return
    import sqlite3
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(control_meta)")}
        if columns == {"singleton", "schema_name", "schema_version", "schema_sha256", "created_at"}:
            row = connection.execute("SELECT schema_name,schema_version,schema_sha256 FROM control_meta WHERE singleton=1").fetchone()
            if row is None or row[0] != "pisec-core" or row[1] != 16:
                raise ValueError("unsupported Pisec state schema")
            return
        if columns != {"singleton", "schema_name", "schema_version", "schema_sha256", "migration_name", "created_at"}:
            raise ValueError("unsupported Pisec state schema")
        row = connection.execute("SELECT schema_name,schema_version,schema_sha256,migration_name FROM control_meta WHERE singleton=1").fetchone()
        if row != ("pisec-core", 15, EPOCH15_DIGEST, "pisec-core-epoch-15"):
            raise ValueError("unsupported Pisec state schema")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "access_grants" not in tables:
            return
        workstream = connection.execute("SELECT grant_id FROM access_grants WHERE subject_kind='workstream' AND state <> 'revoked' LIMIT 1").fetchone()
        if workstream:
            raise PermissionError(f"non-revoked workstream grant blocks migration: {workstream[0]}")
        ambiguous = connection.execute("SELECT grant_id FROM access_grants WHERE subject_kind='project_workers' AND state IN ('proposed','revoking') LIMIT 1").fetchone()
        if ambiguous:
            raise PermissionError(f"in-flight project grant blocks migration: {ambiguous[0]}")
    finally:
        connection.close()


def _systemctl(action: str) -> None:
    subprocess.run(["systemctl", "--user", action, "pisec-broker.service"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _post_switch(current: Path, wait_seconds: float) -> dict:
    _systemctl("start")
    doctor = subprocess.run([str(current / "bin" / "pisec"), "doctor", "--json"], text=True, capture_output=True)
    if doctor.returncode:
        raise RuntimeError(f"doctor failed: {doctor.stdout[-512:]}{doctor.stderr[-512:]}")
    refresh = subprocess.run([str(current / "bin" / "pisec"), "project", "refresh", "--all", "--wait-seconds", str(wait_seconds), "--json"], text=True, capture_output=True)
    refresh_value = json.loads(refresh.stdout) if refresh.stdout.strip() else {"ok": False}
    if refresh.returncode or refresh_value.get("failed"):
        raise RuntimeError(f"runtime refresh failed: {refresh.stdout[-512:]}{refresh.stderr[-512:]}")
    reconcile = subprocess.run([str(current / "bin" / "pisec"), "reconcile", "--json"], text=True, capture_output=True)
    if reconcile.returncode:
        raise RuntimeError(f"reconcile failed: {reconcile.stdout[-512:]}{reconcile.stderr[-512:]}")
    return {"doctor": "ok", "refresh": refresh_value, "reconcile": "ok"}


def update(repo: Path, ref: str, wait_seconds: float, state_root: Path, install_root: Path) -> tuple[int, dict]:
    status_path = state_root / "update-status.json"
    base = {"schemaVersion": 1, "state": "running", "sourceCommit": None, "sourceTree": None, "bundleSha256": None, "currentStep": "lock", "refresh": None, "error": None, "startedAt": now(), "finishedAt": None}
    _json_write(status_path, base)
    install_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = install_root / ".update.lock"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            base.update(state="failed", currentStep="lock", error="another updater is running", finishedAt=now())
            _json_write(status_path, base)
            return EXIT_LOCKED, base
        current = install_root / "current"
        staging = install_root / f".staging-{uuid.uuid4().hex}"
        deployment = install_root / f"deploy-{uuid.uuid4().hex}"
        switched = False
        try:
            base["currentStep"] = "resolve"
            _json_write(status_path, base)
            if ref == "HEAD" and _git(repo, "status", "--porcelain"):
                raise RuntimeError("source checkout is dirty; commit changes before updating Pisec")
            _preflight_state(state_root)
            staging.mkdir(mode=0o700)
            base["currentStep"] = "archive"
            commit, tree, digest = _archive(repo, ref, staging)
            base.update(sourceCommit=commit, sourceTree=tree, bundleSha256=digest, currentStep="verify")
            manifest = {"schemaVersion": 1, "sourceCommit": commit, "sourceTree": tree, "treeSha256": digest, "createdAt": now()}
            (staging / "bundle.json").write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            os.chmod(staging / "bundle.json", 0o600)
            _safe_tree(staging)
            base["currentStep"] = "switch"
            _json_write(status_path, base)
            os.replace(staging, deployment)
            _systemctl("stop")
            temporary = install_root / f".current-{uuid.uuid4().hex}"
            temporary.symlink_to(deployment.name)
            os.replace(temporary, current)
            switched = True
            for old in install_root.glob("deploy-*"):
                if old != deployment:
                    shutil.rmtree(old, ignore_errors=True)
            base["currentStep"] = "health"
            _json_write(status_path, base)
            base["refresh"] = _post_switch(current, wait_seconds)
            base.update(state="applied", currentStep="complete", finishedAt=now())
            _json_write(status_path, base)
            return 0, base
        except PermissionError as error:
            base.update(state="failed" if not switched else "needs_attention", error=str(error), finishedAt=now())
            _json_write(status_path, base)
            return EXIT_UNSUPPORTED_STATE if not switched else EXIT_NEEDS_ATTENTION, base
        except Exception as error:
            base.update(state="failed" if not switched else "needs_attention", error=str(error)[:2048], finishedAt=now())
            _json_write(status_path, base)
            return EXIT_FAILED if not switched else EXIT_NEEDS_ATTENTION, base
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pisec update")
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--repo", type=Path, default=None)
    parser.add_argument("--wait-seconds", type=float, default=300.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if os.name != "posix" or sys.platform != "linux":
        print("Pisec updater supports Linux only", file=sys.stderr)
        return EXIT_FAILED
    repo = (args.repo or Path(os.environ.get("PISEC_SOURCE_ROOT", Path.cwd()))).expanduser().resolve()
    state_root = Path(os.environ.get("PISEC_STATE_ROOT", Path.home() / ".local" / "state" / "pisec")).expanduser()
    install_root = Path(os.environ.get("PISEC_INSTALL_ROOT", Path.home() / ".local" / "lib" / "pisec")).expanduser()
    code, result = update(repo, args.commit, args.wait_seconds, state_root, install_root)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"pisec update: {result['state']} ({result.get('currentStep')})")
        if result.get("error"):
            print(result["error"], file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
