#!/usr/bin/env python3
"""Install committed Pisec bundles with bounded manual recovery."""

from __future__ import annotations

import argparse
import ast
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid


SCHEMA_VERSION = 1
DATABASE_NAME = "pisec-core-v1"
COLLIE_PATCH = "patches/collie-v0.28-unread-idle.patch"
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
    COLLIE_PATCH,
)
REQUIRED = ("bin/pisec", "scripts/pisec-update.py", "scripts/pisec", "pisec", COLLIE_PATCH)


class UnsupportedStateError(RuntimeError):
    pass


class LockedError(RuntimeError):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_value(value: object) -> str:
    return _sha256_bytes(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode())


def _document_digest(value: dict, field: str) -> str:
    unsigned = dict(value)
    unsigned.pop(field, None)
    return _digest_value(unsigned)


def _json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _read_json(path: Path) -> dict:
    info = path.lstat()
    if info.st_uid != os.geteuid() or stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError(f"unsafe updater metadata: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"updater metadata is not an object: {path.name}")
    return value


def _owner_dir(path: Path, *, create: bool = False) -> None:
    if not path.exists():
        if not create:
            raise RuntimeError(f"owner-only directory is missing: {path.name}")
        path.mkdir(mode=0o700, parents=True)
    info = path.lstat()
    if info.st_uid != os.geteuid() or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError(f"owner-only directory is unsafe: {path.name}")


def _ensure_install_root(install_root: Path) -> None:
    if not install_root.exists():
        install_root.mkdir(mode=0o700, parents=True)
    info = install_root.lstat()
    if info.st_uid != os.geteuid() or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022:
        raise RuntimeError("Pisec install root is not owner-controlled")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(install_root, 0o700)


def _run(args: list[str], *, cwd: Path | None = None) -> str:
    return subprocess.check_output(args, cwd=str(cwd) if cwd else None, stderr=subprocess.STDOUT, text=True).strip()


def _git(repo: Path, *args: str) -> str:
    return _run(["git", "-C", str(repo), *args])


def _assert_clean(repo: Path) -> None:
    if _git(repo, "status", "--porcelain"):
        raise RuntimeError("source checkout is dirty; commit changes before updating Pisec")


def _safe_tree(root: Path) -> None:
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        if info.st_uid != os.geteuid():
            raise RuntimeError(f"unsafe bundle ownership or mode: {path}")
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise RuntimeError(f"unsupported bundle entry: {path}")
        if stat.S_ISDIR(info.st_mode):
            os.chmod(path, 0o700)
        else:
            os.chmod(path, 0o700 if info.st_mode & stat.S_IXUSR else 0o600)


def _tree_digest(root: Path, *, exclude: frozenset[str] = frozenset()) -> str:
    digest = hashlib.sha256()
    for path in [root, *sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())]:
        relative = "." if path == root else path.relative_to(root).as_posix()
        if relative in exclude:
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"bundle contains an unsupported entry: {relative}")
        if stat.S_ISDIR(info.st_mode):
            digest.update(f"d\0{relative}\0".encode())
        elif stat.S_ISREG(info.st_mode):
            digest.update(f"f\0{relative}\0{stat.S_IMODE(info.st_mode) & 0o700:o}\0".encode())
            digest.update(path.read_bytes())
            digest.update(b"\0")
        else:
            raise RuntimeError(f"bundle contains an unsupported entry: {relative}")
    return digest.hexdigest()


def _opaque_archive_digest(root: Path) -> str:
    """Digest an archived pre-v1 tree without interpreting its contents."""
    digest = hashlib.sha256()
    for path in [root, *sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())]:
        relative = "." if path == root else path.relative_to(root).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode) & 0o700
        if stat.S_ISLNK(info.st_mode):
            digest.update(f"l\0{relative}\0{mode:o}\0".encode())
            digest.update(os.readlink(path).encode())
            digest.update(b"\0")
        elif stat.S_ISDIR(info.st_mode):
            digest.update(f"d\0{relative}\0{mode:o}\0".encode())
        elif stat.S_ISREG(info.st_mode):
            digest.update(f"f\0{relative}\0{mode:o}\0".encode())
            digest.update(path.read_bytes())
            digest.update(b"\0")
        else:
            digest.update(f"x\0{relative}\0{stat.S_IFMT(info.st_mode):o}\0{mode:o}\0{info.st_size}\0".encode())
    return digest.hexdigest()


def _allowed(name: str) -> bool:
    return any(
        name == prefix.rstrip("/")
        or name.startswith(prefix)
        or prefix.startswith(name.rstrip("/") + "/")
        for prefix in ALLOWLIST
    )


def _archive(repo: Path, ref: str, destination: Path) -> tuple[str, str, str]:
    commit = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    command = ["git", "-C", str(repo), "archive", "--format=tar", commit, *ALLOWLIST]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert process.stdout is not None
    try:
        with process.stdout as stream, tarfile.open(fileobj=stream, mode="r|") as archive:
            for member in archive:
                name = member.name.rstrip("/")
                if not name or not _allowed(name) or name.startswith("/") or ".." in Path(name).parts:
                    process.kill()
                    raise RuntimeError(f"bundle member is outside the allowlist: {name}")
                if member.issym() or member.islnk() or not (member.isdir() or member.isreg()):
                    raise RuntimeError(f"bundle member is unsafe: {name}")
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
                os.chmod(target, member.mode & 0o777)
    finally:
        error = process.wait()
    if error:
        raise RuntimeError(f"git archive failed with status {error}")
    for required in REQUIRED:
        if not (destination / required).exists():
            raise RuntimeError(f"bundle is missing required path: {required}")
    _safe_tree(destination)
    return commit, tree, _tree_digest(destination)


def _schema_identity(repo: Path, commit: str) -> dict[str, object]:
    source = _run(["git", "-C", str(repo), "show", f"{commit}:scripts/pisec/pi_schema.py"])
    tree = ast.parse(source)
    values: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in {"SCHEMA_NAME", "SCHEMA_VERSION", "SCHEMA_SQL"}:
                value = ast.literal_eval(node.value)
                values[target.id] = value
    if not isinstance(values.get("SCHEMA_NAME"), str) or not isinstance(values.get("SCHEMA_VERSION"), int) or not isinstance(values.get("SCHEMA_SQL"), str):
        raise RuntimeError("staged schema identity is unavailable")
    return {
        "name": values["SCHEMA_NAME"],
        "version": values["SCHEMA_VERSION"],
        "sha256": _sha256_bytes(str(values["SCHEMA_SQL"]).encode("utf-8")),
    }


def _schema_digest(repo: Path, commit: str) -> str:
    return str(_schema_identity(repo, commit)["sha256"])


def _patch_digest(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("the exact Collie patch is missing")
    return _sha256_bytes(path.read_bytes())


def _validate_bundle(bundle: Path, expected: dict | None = None) -> None:
    _safe_tree(bundle)
    patch_files = sorted(path.relative_to(bundle).as_posix() for path in (bundle / "patches").rglob("*") if path.is_file()) if (bundle / "patches").is_dir() else []
    if patch_files != [COLLIE_PATCH]:
        raise RuntimeError("bundle must contain exactly the pinned Collie patch")
    patch_sha = _patch_digest(bundle / COLLIE_PATCH)
    for path in bundle.rglob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec", dont_inherit=True)
    bun = shutil.which("bun")
    extension = bundle / "omp" / "extensions" / "pisec.ts"
    if bun and extension.is_file():
        with tempfile.TemporaryDirectory(prefix="pisec-bun-") as output:
            result = subprocess.run([bun, "build", str(extension), "--target", "bun", "--outdir", output], text=True, capture_output=True)
            if result.returncode:
                raise RuntimeError(f"OMP extension build failed: {result.stdout[-256:]}{result.stderr[-256:]}")
    if expected is not None:
        actual_digest = _tree_digest(bundle, exclude=frozenset({"bundle.json"}))
        if expected.get("bundleSha256") != actual_digest or expected.get("colliePatchSha256") != patch_sha or expected.get("manifestSha256") != _document_digest(expected, "manifestSha256"):
            raise RuntimeError("bundle digest or Collie patch provenance is invalid")
        database = expected.get("database")
        if not isinstance(database, dict) or database.get("name") != DATABASE_NAME or database.get("version") != 1 or not isinstance(database.get("sha256"), str):
            raise RuntimeError("bundle database identity is invalid")


def _bundle_manifest(bundle: Path, *, commit: str, tree: str, digest: str, database: dict[str, object]) -> dict:
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "sourceCommit": commit,
        "sourceTree": tree,
        "bundleSha256": digest,
        "database": database,
        "colliePatch": {"path": COLLIE_PATCH, "sha256": _patch_digest(bundle / COLLIE_PATCH)},
        "colliePatchSha256": _patch_digest(bundle / COLLIE_PATCH),
        "createdAt": now(),
    }
    manifest["manifestSha256"] = _document_digest(manifest, "manifestSha256")
    return manifest


def _write_bundle_manifest(bundle: Path, manifest: dict) -> None:
    path = bundle / "bundle.json"
    path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _stage_candidate(repo: Path, commit: str, install_root: Path) -> dict:
    staging = install_root / f".staging-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    commit = _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    database = _schema_identity(repo, commit)
    try:
        resolved, archived_tree, digest = _archive(repo, commit, staging)
        if resolved != commit or archived_tree != tree:
            raise RuntimeError("committed bundle identity changed during archive")
        manifest = _bundle_manifest(staging, commit=commit, tree=tree, digest=digest, database=database)
        _write_bundle_manifest(staging, manifest)
        _validate_bundle(staging, manifest)
        return {"staging": staging, "commit": commit, "tree": tree, "digest": digest, "database": database, "manifest": manifest}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _deployment_path(install_root: Path, name: str) -> Path:
    if Path(name).name != name or not name.startswith("deploy-"):
        raise RuntimeError("deployment identity is invalid")
    path = install_root / name
    if path.is_symlink() or not path.is_dir() or path.resolve().parent != install_root.resolve():
        raise RuntimeError("deployment is outside the install root")
    return path


def _deployment_identity(deployment: Path) -> dict:
    manifest = _read_json(deployment / "bundle.json")
    if manifest.get("schemaVersion") != SCHEMA_VERSION or not isinstance(manifest.get("sourceCommit"), str) or not isinstance(manifest.get("sourceTree"), str) or manifest.get("manifestSha256") != _document_digest(manifest, "manifestSha256"):
        raise RuntimeError("deployment bundle manifest is invalid")
    if manifest.get("bundleSha256") != _tree_digest(deployment, exclude=frozenset({"bundle.json"})):
        raise RuntimeError("deployment bundle digest is invalid")
    patch_sha = _patch_digest(deployment / COLLIE_PATCH)
    if manifest.get("colliePatchSha256") != patch_sha or manifest.get("colliePatch") != {"path": COLLIE_PATCH, "sha256": patch_sha}:
        raise RuntimeError("deployment Collie patch digest is invalid")
    if manifest.get("database", {}).get("name") != DATABASE_NAME or manifest.get("database", {}).get("version") != 1:
        raise RuntimeError("deployment database identity is unsupported")
    return {**manifest, "deployment": deployment.name}


def _same_identity(left: dict, right: dict) -> bool:
    return all(left.get(key) == right.get(key) for key in ("deployment", "sourceCommit", "sourceTree", "bundleSha256", "database"))


def _current_target(install_root: Path) -> Path | None:
    current = install_root / "current"
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise RuntimeError("current deployment link is not a symlink")
    target = (install_root / os.readlink(current)).resolve()
    return _deployment_path(install_root, target.name)


def _verified_record(install_root: Path, deployment: Path, *, expected_database: dict | None = None) -> dict:
    record = _read_json(install_root / "verified" / f"{deployment.name}.json")
    identity = _deployment_identity(deployment)
    if not _same_identity(record, identity) or not isinstance(record.get("verifiedAt"), str) or not isinstance(record.get("verificationDigests"), dict) or set(record["verificationDigests"]) != {"doctor", "refresh", "reconcile"} or record.get("verificationSha256") != _document_digest(record, "verificationSha256"):
        raise RuntimeError("deployment verification record does not match its bundle")
    if expected_database is not None and record.get("database") != expected_database:
        raise RuntimeError("deployment verification schema identity is incompatible")
    return record


def _marker(install_root: Path, *, expected_database: dict | None = None, current: Path | None = None) -> dict | None:
    path = install_root / "last-known-good.json"
    if not path.exists() and not path.is_symlink():
        return None
    marker = _read_json(path)
    deployment = _deployment_path(install_root, str(marker.get("deployment", "")))
    record = _verified_record(install_root, deployment, expected_database=expected_database)
    if marker.get("schemaVersion") != SCHEMA_VERSION or marker.get("marker") != "last-known-good" or marker.get("verifiedAt") != record.get("verifiedAt") or marker.get("markerSha256") != _document_digest(marker, "markerSha256") or not _same_identity(marker, record):
        raise RuntimeError("last-known-good marker does not match its verification record")
    return marker


def _stable_updater(install_root: Path) -> dict | None:
    stable = install_root / "bin" / "pisec-update"
    manifest_path = install_root / "stable-updater.json"
    if not stable.exists() and not manifest_path.exists():
        return None
    if not stable.is_file() or stable.is_symlink() or not manifest_path.is_file():
        raise RuntimeError("stable updater or manifest is incomplete")
    manifest = _read_json(manifest_path)
    if manifest.get("schemaVersion") != SCHEMA_VERSION or not isinstance(manifest.get("sourceCommit"), str) or not isinstance(manifest.get("sourceTree"), str) or manifest.get("fileSha256") != _sha256_bytes(stable.read_bytes()) or manifest.get("manifestSha256") != _document_digest(manifest, "manifestSha256"):
        raise RuntimeError("stable updater manifest is tampered")
    return manifest


def _preflight_state(state_root: Path, expected_database: dict) -> None:
    if not state_root.exists() and not state_root.is_symlink():
        return
    try:
        info = state_root.lstat()
        if info.st_uid != os.geteuid() or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            raise UnsupportedStateError("unsupported Pisec state root; review it and select explicit archive/reset")
        database = state_root / "control.db"
        if not database.exists():
            return
        db_info = database.lstat()
        if db_info.st_uid != os.geteuid() or stat.S_ISLNK(db_info.st_mode) or not stat.S_ISREG(db_info.st_mode) or stat.S_IMODE(db_info.st_mode) != 0o600:
            raise UnsupportedStateError("unsupported Pisec state database; review it and select explicit archive/reset")
        import sqlite3
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(control_meta)")}
            if columns != {"singleton", "schema_name", "schema_version", "schema_sha256", "created_at"}:
                raise UnsupportedStateError("unsupported Pisec state schema; review it and select explicit archive/reset")
            row = connection.execute("SELECT schema_name,schema_version,schema_sha256 FROM control_meta WHERE singleton=1").fetchone()
            actual = None if row is None else {"name": row[0], "version": row[1], "sha256": row[2]}
            if actual != expected_database:
                raise UnsupportedStateError("unsupported Pisec state schema; review it and select explicit archive/reset")
        finally:
            connection.close()
    except UnsupportedStateError:
        raise
    except Exception as error:
        raise UnsupportedStateError("unsupported Pisec state; review it and select explicit archive/reset") from error


def _status_identity(identity: dict | None) -> dict | None:
    if identity is None:
        return None
    return {key: identity.get(key) for key in ("deployment", "sourceCommit", "sourceTree", "bundleSha256", "database")}


def _status(*, state: str, step: str, current: dict | None = None, candidate: dict | None = None, stable: dict | None = None, marker: dict | None = None, error: str | None = None, refresh: object = None) -> dict:
    current_status = _status_identity(current)
    marker_status = _status_identity(marker)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "state": state,
        "currentStep": step,
        "sourceCommit": None if candidate is None else candidate.get("sourceCommit"),
        "sourceTree": None if candidate is None else candidate.get("sourceTree"),
        "bundleSha256": None if candidate is None else candidate.get("bundleSha256"),
        "current": current_status,
        "candidate": _status_identity(candidate),
        "stableUpdater": None if stable is None else {key: stable.get(key) for key in ("sourceCommit", "sourceTree", "fileSha256")},
        "lastKnownGood": marker_status,
        "recoveryAvailable": bool(marker_status is not None and (current_status is None or marker_status.get("deployment") != current_status.get("deployment"))),
        "recoveryReason": "verified last-known-good deployment is available" if marker_status is not None and (current_status is None or marker_status.get("deployment") != current_status.get("deployment")) else "no compatible last-known-good deployment",
        "refresh": refresh,
        "error": error,
        "startedAt": now(),
        "finishedAt": None if state == "running" else now(),
    }


def _systemctl(action: str) -> None:
    subprocess.run(["systemctl", "--user", action, "pisec-broker.service"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _broker_runtime_root() -> Path:
    configured = os.environ.get("PISEC_RUNTIME_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.geteuid()}")) / "pisec"


def _broker_ready() -> bool:
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", "pisec-broker.service"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
    if not active:
        return False
    socket_path = _broker_runtime_root() / "admin" / "control.sock"
    try:
        info = socket_path.lstat()
    except OSError:
        return False
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            client.connect(str(socket_path))
    except OSError:
        return False
    return True


def _wait_for_broker(wait_seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        if _broker_ready():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Pisec broker did not become ready before the health deadline")
        time.sleep(min(0.1, remaining))


def _post_switch(current: Path, wait_seconds: float) -> dict:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    _systemctl("start")
    _wait_for_broker(wait_seconds)
    doctor = subprocess.run([str(current / "bin" / "pisec"), "doctor", "--json"], env=environment, text=True, capture_output=True)
    if doctor.returncode:
        raise RuntimeError(f"doctor failed: {doctor.stdout[-512:]}{doctor.stderr[-512:]}")
    refresh = subprocess.run([str(current / "bin" / "pisec"), "project", "refresh", "--all", "--wait-seconds", str(wait_seconds), "--json"], env=environment, text=True, capture_output=True)
    refresh_value = json.loads(refresh.stdout) if refresh.stdout.strip() else {"ok": False}
    if refresh.returncode or refresh_value.get("failed"):
        raise RuntimeError(f"runtime refresh failed: {refresh.stdout[-512:]}{refresh.stderr[-512:]}")
    reconcile = subprocess.run([str(current / "bin" / "pisec"), "reconcile", "--json"], env=environment, text=True, capture_output=True)
    if reconcile.returncode:
        raise RuntimeError(f"reconcile failed: {reconcile.stdout[-512:]}{reconcile.stderr[-512:]}")
    return {"doctor": "ok", "refresh": refresh_value, "reconcile": "ok"}


def _switch_current(install_root: Path, deployment: Path) -> None:
    temporary = install_root / f".current-{uuid.uuid4().hex}"
    temporary.symlink_to(deployment.name)
    os.replace(temporary, install_root / "current")


def _clear_current(install_root: Path) -> None:
    current = install_root / "current"
    if current.exists() or current.is_symlink():
        if not current.is_symlink():
            raise RuntimeError("current deployment link is not a symlink")
        current.unlink()


def _write_verification(install_root: Path, deployment: Path, health: dict) -> dict:
    identity = _deployment_identity(deployment)
    record = {
        **identity,
        "verifiedAt": now(),
        "verificationDigests": {
            "doctor": _sha256_bytes(b"ok"),
            "refresh": _digest_value(health.get("refresh")),
            "reconcile": _sha256_bytes(b"ok"),
        },
    }
    record["verificationSha256"] = _document_digest(record, "verificationSha256")
    _json_write(install_root / "verified" / f"{deployment.name}.json", record)
    return record


def _write_marker(install_root: Path, record: dict) -> dict:
    marker = {key: record[key] for key in ("sourceCommit", "sourceTree", "bundleSha256", "database", "deployment", "verifiedAt")}
    marker["schemaVersion"] = SCHEMA_VERSION
    marker["marker"] = "last-known-good"
    marker["markerSha256"] = _document_digest(marker, "markerSha256")
    _json_write(install_root / "last-known-good.json", marker)
    return marker


def _prune(install_root: Path, *, current: Path, marker: dict | None) -> None:
    retained = {current.name}
    if marker is not None:
        retained.add(str(marker["deployment"]))
    for deployment in install_root.glob("deploy-*"):
        if deployment.name not in retained:
            if deployment.is_symlink() or not deployment.is_dir():
                raise RuntimeError("unsafe unreferenced deployment")
            shutil.rmtree(deployment)
    verified = install_root / "verified"
    if verified.exists():
        _owner_dir(verified)
        for record in verified.glob("deploy-*.json"):
            if record.stem not in retained:
                record.unlink()


def _preflight_metadata(install_root: Path, *, expected_database: dict, current: Path | None) -> tuple[dict | None, dict | None, dict | None]:
    current_identity = None if current is None else _deployment_identity(current)
    stable = _stable_updater(install_root)
    verified = install_root / "verified"
    if verified.exists():
        _owner_dir(verified)
        for path in verified.glob("deploy-*.json"):
            deployment = _deployment_path(install_root, path.stem)
            _verified_record(install_root, deployment, expected_database=expected_database)
    marker = _marker(install_root, expected_database=expected_database, current=current)
    return current_identity, stable, marker


@contextlib.contextmanager
def _update_lock(install_root: Path):
    lock_path = install_root / ".update.lock"
    lock_path.parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(lock_path.parent, 0o700)
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LockedError("another updater is running") from error
        yield


def _failure(code: int, status_path: Path, status: dict, error: Exception, *, switched: bool = False, current: dict | None = None, candidate: dict | None = None, stable: dict | None = None, marker: dict | None = None, refresh: object = None) -> tuple[int, dict]:
    status.update(_status(state="needs_attention" if switched else "failed", step=str(status.get("currentStep", "unknown")), current=current, candidate=candidate, stable=stable, marker=marker, error=str(error)[:2048], refresh=refresh))
    _json_write(status_path, status)
    return (EXIT_NEEDS_ATTENTION if switched else code), status


def update(repo: Path, ref: str, wait_seconds: float, state_root: Path, install_root: Path, *, reject_dirty: bool = False) -> tuple[int, dict]:
    _ensure_install_root(install_root)
    status_path = install_root / "update-status.json"
    status = _status(state="running", step="preflight")
    _json_write(status_path, status)
    staging: Path | None = None
    deployment: Path | None = None
    switched = False
    candidate_identity: dict | None = None
    current_identity: dict | None = None
    stable: dict | None = None
    marker: dict | None = None
    try:
        with _update_lock(install_root):
            if reject_dirty:
                _assert_clean(repo)
            commit = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
            expected_database = _schema_identity(repo, commit)
            current = _current_target(install_root)
            current_identity, stable, marker = _preflight_metadata(install_root, expected_database=expected_database, current=current)
            _preflight_state(state_root, expected_database)
            candidate = _stage_candidate(repo, commit, install_root)
            staging = candidate["staging"]
            candidate_manifest = candidate["manifest"]
            status.update(sourceCommit=commit, sourceTree=candidate["tree"], bundleSha256=candidate["digest"], currentStep="switch")
            _json_write(status_path, status)
            deployment = install_root / f"deploy-{uuid.uuid4().hex}"
            os.replace(staging, deployment)
            staging = None
            candidate_identity = _deployment_identity(deployment)
            _systemctl("stop")
            _switch_current(install_root, deployment)
            switched = True
            current = _current_target(install_root)
            status.update(currentStep="health")
            _json_write(status_path, status)
            try:
                health = _post_switch(current, wait_seconds)
            except Exception as error:
                return _failure(EXIT_FAILED, status_path, status, error, switched=True, current=candidate_identity, candidate=candidate_identity, stable=stable, marker=marker)
            status["refresh"] = health
            status["currentStep"] = "verification"
            _json_write(status_path, status)
            verified = _write_verification(install_root, deployment, health)
            status["currentStep"] = "stable-updater"
            _json_write(status_path, status)
            stable = _install_stable_from_deployment(install_root, deployment, candidate_identity)
            status["currentStep"] = "last-known-good"
            _json_write(status_path, status)
            if current_identity is not None:
                previous_verified = _verified_record(install_root, _deployment_path(install_root, current_identity["deployment"]), expected_database=expected_database)
                marker = _write_marker(install_root, previous_verified)
            status["currentStep"] = "prune"
            _json_write(status_path, status)
            _prune(install_root, current=deployment, marker=marker)
            current_identity = candidate_identity
            status = _status(state="applied", step="complete", current=current_identity, candidate=candidate_identity, stable=stable, marker=marker, refresh=health)
            _json_write(status_path, status)
            return 0, status
    except LockedError as error:
        status = _status(state="failed", step="lock", error=str(error))
        _json_write(status_path, status)
        return EXIT_LOCKED, status
    except UnsupportedStateError as error:
        status = _status(state="failed", step="preflight", error=str(error))
        _json_write(status_path, status)
        return EXIT_UNSUPPORTED_STATE, status
    except Exception as error:
        result, status = _failure(EXIT_FAILED, status_path, status, error, switched=switched, current=current_identity, candidate=candidate_identity, stable=stable, marker=marker)
        return result, status
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if deployment is not None and not switched and deployment.exists():
            shutil.rmtree(deployment, ignore_errors=True)


def _install_stable_from_deployment(install_root: Path, deployment: Path, identity: dict) -> dict:
    source = deployment / "scripts" / "pisec-update.py"
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("candidate stable updater is missing")
    compile(source.read_text(encoding="utf-8"), str(source), "exec", dont_inherit=True)
    return _atomic_install_stable(
        install_root,
        source,
        {"sourceCommit": identity["sourceCommit"], "sourceTree": identity["sourceTree"], "bundleSha256": identity["bundleSha256"]},
    )


def _atomic_install_stable(install_root: Path, source: Path, fields: dict) -> dict:
    stable = install_root / "bin" / "pisec-update"
    stable.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(stable.parent, 0o700)
    manifest_path = install_root / "stable-updater.json"
    old_stable = stable.read_bytes() if stable.exists() else None
    old_manifest = manifest_path.read_bytes() if manifest_path.exists() else None
    temporary = stable.with_name(f".pisec-update.{uuid.uuid4().hex}.tmp")
    temporary_manifest = manifest_path.with_name(f".stable-updater.{uuid.uuid4().hex}.tmp")
    manifest = {"schemaVersion": SCHEMA_VERSION, **fields, "fileSha256": None, "installedAt": now()}
    try:
        shutil.copy2(source, temporary)
        os.chmod(temporary, 0o700)
        manifest["fileSha256"] = _sha256_bytes(temporary.read_bytes())
        manifest["manifestSha256"] = _document_digest(manifest, "manifestSha256")
        temporary_manifest.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        os.chmod(temporary_manifest, 0o600)
        os.replace(temporary, stable)
        os.replace(temporary_manifest, manifest_path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        if temporary_manifest.exists():
            temporary_manifest.unlink()
        if old_stable is None:
            if stable.exists() or stable.is_symlink():
                stable.unlink()
        else:
            restore = stable.with_name(f".pisec-update-restore.{uuid.uuid4().hex}.tmp")
            restore.write_bytes(old_stable)
            os.chmod(restore, 0o700)
            os.replace(restore, stable)
        if old_manifest is None:
            if manifest_path.exists() or manifest_path.is_symlink():
                manifest_path.unlink()
        else:
            restore_manifest = manifest_path.with_name(f".stable-updater-restore.{uuid.uuid4().hex}.tmp")
            restore_manifest.write_bytes(old_manifest)
            os.chmod(restore_manifest, 0o600)
            os.replace(restore_manifest, manifest_path)
        raise
    return manifest


def install_updater_only(repo: Path, ref: str, install_root: Path) -> tuple[int, dict]:
    _ensure_install_root(install_root)
    status_path = install_root / "stable-updater.json"
    try:
        with _update_lock(install_root):
            _assert_clean(repo)
            _stable_updater(install_root)
            with tempfile.TemporaryDirectory(prefix="pisec-updater-") as tmp:
                bundle = Path(tmp) / "bundle"
                commit, tree, digest = _archive(repo, ref, bundle)
                _validate_bundle(bundle)
                source = bundle / "scripts" / "pisec-update.py"
                compile(source.read_text(encoding="utf-8"), str(source), "exec", dont_inherit=True)
                manifest = _atomic_install_stable(install_root, source, {"sourceCommit": commit, "sourceTree": tree, "bundleSha256": digest})
                return 0, {"state": "applied", **manifest}
    except LockedError as error:
        return EXIT_LOCKED, {"state": "failed", "error": str(error)}
    except Exception as error:
        return EXIT_FAILED, {"state": "failed", "error": str(error)[:2048]}


def _broker_quiescent() -> bool:
    if os.environ.get("PISEC_BROKER_QUIESCENT") == "1":
        return True
    result = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "pisec-broker.service"])
    return result.returncode != 0


def _checkpoint_wal(state_root: Path) -> None:
    database = state_root / "control.db"
    if not database.exists():
        return
    import sqlite3
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def _archive_root(state_root: Path) -> Path | None:
    if not state_root.exists() and not state_root.is_symlink():
        return None
    info = state_root.lstat()
    if info.st_uid != os.geteuid() or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("Pisec state root is unsafe; archive/reset stopped")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = state_root.with_name(f"{state_root.name}.archive-{stamp}")
    suffix = 0
    while archive.exists() or archive.is_symlink():
        suffix += 1
        archive = state_root.with_name(f"{state_root.name}.archive-{stamp}-{suffix}")
    os.replace(state_root, archive)
    os.chmod(archive, 0o700)
    return archive


def _quarantine_pre_v1_metadata(install_root: Path, archive_name: str) -> Path:
    root = install_root / "pre-v1-control-archive" / archive_name
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    for name in ("last-known-good.json", "verified", "update-status.json"):
        source = install_root / name
        if source.exists() or source.is_symlink():
            os.replace(source, root / name)
    return root


def _write_archive_manifest(install_root: Path, state_root: Path, archive: Path) -> dict:
    filesystem_sha = _opaque_archive_digest(archive)
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "archive": archive.name,
        "sourceRoot": state_root.name,
        "sourceFilesystemSha256": filesystem_sha,
        "filesystemSha256": filesystem_sha,
        "archivedAt": now(),
    }
    _json_write(install_root / "archive-manifests" / f"{archive.name}.json", manifest)
    return manifest


def _initialize_candidate_state(bundle: Path, state_root: Path) -> None:
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_root, 0o700)
    source = "from pathlib import Path; import sys; from scripts.pisec.pi_store import PiStore; store=PiStore(Path(sys.argv[1])); store.close()"
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(bundle) + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
    result = subprocess.run([sys.executable, "-c", source, str(state_root)], env=environment, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"candidate v1 schema initialization failed: {result.stdout[-256:]}{result.stderr[-256:]}")


def archive_reset_state(repo: Path, ref: str, wait_seconds: float, state_root: Path, install_root: Path) -> tuple[int, dict]:
    _ensure_install_root(install_root)
    status_path = install_root / "update-status.json"
    status = _status(state="running", step="preflight")
    _json_write(status_path, status)
    staging: Path | None = None
    deployment: Path | None = None
    switched = False
    archive: Path | None = None
    try:
        with _update_lock(install_root):
            _assert_clean(repo)
            if not _broker_quiescent():
                raise RuntimeError("Pisec broker is not quiescent; stop it before archive/reset")
            stable = _stable_updater(install_root)
            commit = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
            expected_database = _schema_identity(repo, commit)
            if expected_database.get("name") != DATABASE_NAME or expected_database.get("version") != 1:
                raise RuntimeError("candidate does not declare the exact Pisec v1 database")
            candidate = _stage_candidate(repo, commit, install_root)
            staging = candidate["staging"]
            deployment = install_root / f"deploy-{uuid.uuid4().hex}"
            os.replace(staging, deployment)
            staging = None
            identity = _deployment_identity(deployment)
            _systemctl("stop")
            _checkpoint_wal(state_root)
            archive = _archive_root(state_root)
            if archive is not None:
                _write_archive_manifest(install_root, state_root, archive)
                _quarantine_pre_v1_metadata(install_root, archive.name)
            _clear_current(install_root)
            _initialize_candidate_state(deployment, state_root)
            _switch_current(install_root, deployment)
            switched = True
            status = _status(state="running", step="health", current=identity, candidate=identity, stable=stable, marker=None)
            _json_write(status_path, status)
            try:
                health = _post_switch(_current_target(install_root), wait_seconds)
            except Exception as error:
                if archive is not None:
                    error = RuntimeError(f"{error}; archive={archive}; partial_state={state_root}")
                return _failure(EXIT_FAILED, status_path, status, error, switched=True, current=identity, candidate=identity, stable=stable, marker=None)
            _write_verification(install_root, deployment, health)
            status = _status(state="applied", step="complete", current=identity, candidate=identity, stable=stable, marker=None, refresh=health)
            status["recoveryAvailable"] = False
            status["recoveryReason"] = "no compatible predecessor after archive/reset"
            _json_write(status_path, status)
            return 0, status
    except LockedError as error:
        status = _status(state="failed", step="lock", error=str(error))
        _json_write(status_path, status)
        return EXIT_LOCKED, status
    except Exception as error:
        if archive is not None:
            try:
                _systemctl("stop")
            except Exception:
                pass
            error = RuntimeError(f"{error}; archive={archive}; partial_state={state_root}")
        result, failure = _failure(EXIT_FAILED, status_path, status, error, switched=switched)
        return result, failure
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if deployment is not None and not switched and deployment.exists():
            shutil.rmtree(deployment, ignore_errors=True)


def recover_previous(state_root: Path, install_root: Path, wait_seconds: float) -> tuple[int, dict]:
    _ensure_install_root(install_root)
    status_path = install_root / "update-status.json"
    status = _status(state="running", step="preflight")
    _json_write(status_path, status)
    switched = False
    try:
        with _update_lock(install_root):
            current = _current_target(install_root)
            current_identity = None if current is None else _deployment_identity(current)
            marker = _marker(install_root, current=current)
            if marker is None:
                raise RuntimeError("no verified last-known-good deployment is available")
            selected = _deployment_path(install_root, marker["deployment"])
            if current is not None and selected.name == current.name:
                raise RuntimeError("last-known-good deployment is already current")
            _preflight_state(state_root, marker["database"])
            stable = _stable_updater(install_root)
            _systemctl("stop")
            _switch_current(install_root, selected)
            switched = True
            selected_identity = _deployment_identity(selected)
            status = _status(state="running", step="health", current=selected_identity, candidate=selected_identity, stable=stable, marker=marker)
            _json_write(status_path, status)
            try:
                health = _post_switch(selected, wait_seconds)
            except Exception as error:
                return _failure(EXIT_FAILED, status_path, status, error, switched=True, current=selected_identity, candidate=selected_identity, stable=stable, marker=marker)
            status = _status(state="applied", step="complete", current=selected_identity, candidate=None, stable=stable, marker=marker, refresh=health)
            status["recoveryAvailable"] = False
            status["recoveryReason"] = "last-known-good deployment is current"
            _json_write(status_path, status)
            return 0, status
    except Exception as error:
        return _failure(EXIT_FAILED, status_path, status, error, switched=switched)


def _paths_from_environment() -> tuple[Path, Path]:
    state_root = Path(os.environ.get("PISEC_STATE_ROOT", Path.home() / ".local" / "state" / "pisec")).expanduser()
    install_root = Path(os.environ.get("PISEC_INSTALL_ROOT", Path.home() / ".local" / "lib" / "pisec")).expanduser()
    return state_root, install_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pisec update")
    parser.add_argument("--commit", "--ref", dest="ref", default=None, metavar="REF")
    parser.add_argument("--repo", type=Path, default=None)
    parser.add_argument("--wait-seconds", type=float, default=300.0)
    parser.add_argument("--recover-previous", action="store_true")
    parser.add_argument("--install-updater-only", action="store_true")
    parser.add_argument("--archive-reset-state", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if os.name != "posix" or sys.platform != "linux":
        print("Pisec updater supports Linux only", file=sys.stderr)
        return EXIT_FAILED
    modes = sum(bool(value) for value in (args.recover_previous, args.install_updater_only, args.archive_reset_state))
    if modes > 1:
        parser.error("updater modes are mutually exclusive")
    configured_source = Path.home() / ".config" / "pisec" / "source-root"
    configured = configured_source.read_text(encoding="utf-8").strip() if configured_source.is_file() else ""
    repo = (args.repo or Path(os.environ.get("PISEC_SOURCE_ROOT", configured or Path.cwd()))).expanduser().resolve()
    state_root, install_root = _paths_from_environment()
    if args.recover_previous:
        code, result = recover_previous(state_root, install_root, args.wait_seconds)
    elif args.install_updater_only:
        if args.repo is None or args.ref is None:
            parser.error("--install-updater-only requires --repo and --ref")
        code, result = install_updater_only(repo, args.ref, install_root)
    elif args.archive_reset_state:
        if args.repo is None or args.ref is None:
            parser.error("--archive-reset-state requires --repo and --ref")
        code, result = archive_reset_state(repo, args.ref, args.wait_seconds, state_root, install_root)
    else:
        ref = args.ref or "HEAD"
        code, result = update(repo, ref, args.wait_seconds, state_root, install_root, reject_dirty=args.ref is None)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"pisec update: {result.get('state', 'failed')} ({result.get('currentStep', 'unknown')})")
        if result.get("error"):
            print(result["error"], file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
