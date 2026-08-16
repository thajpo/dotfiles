"""Executable disposable rollback-boundary matrix for C10c."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

BOUNDARIES = ("prepare", "package-swap", "image-swap", "post-verify", "rollback")
RECOVERY = ("controller.db", "refs/new.ref", "worktrees/new/README", "evidence/new.json")


def snapshot(root: Path) -> dict[str, dict[str, str | int]]:
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = {"kind": "symlink", "target": os.readlink(path), "mode": path.lstat().st_mode & 0o7777}
        elif path.is_file():
            result[relative] = {"kind": "file", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mode": path.stat().st_mode & 0o7777}
    return result


def _restore_active(active: Path, backup: Path) -> None:
    if backup.exists() or backup.is_symlink():
        if active.exists() or active.is_symlink():
            shutil.rmtree(active)
        shutil.move(str(backup), str(active))


def _inject_boundary(boundary: str, active: Path, backup: Path, candidate: Path, image: Path) -> None:
    """Execute one named swap boundary and raise at the injected point."""
    if boundary == "prepare":
        candidate.mkdir(mode=0o700)
        (candidate / "config.json").write_text("new\n")
        (candidate / "current").symlink_to("config.json")
        raise RuntimeError("injected failure: prepare")
    if boundary == "package-swap":
        shutil.move(str(active), str(backup))
        raise RuntimeError("injected failure: package-swap")
    if boundary == "image-swap":
        image.write_text("new-image\n")
        raise RuntimeError("injected failure: image-swap")
    if boundary == "post-verify":
        shutil.move(str(active), str(backup))
        shutil.move(str(candidate), str(active))
        raise RuntimeError("injected failure: post-verify")
    if boundary == "rollback":
        shutil.move(str(active), str(backup))
        shutil.move(str(candidate), str(active))
        shutil.rmtree(active)
        raise RuntimeError("injected failure: rollback")
    raise AssertionError(boundary)


def run_matrix() -> list[dict[str, object]]:
    results = []
    for boundary in BOUNDARIES:
        with tempfile.TemporaryDirectory(prefix=f"pi-c10c-{boundary}-") as raw:
            root = Path(raw); active = root / "active"; backup = root / "backup"; candidate = root / "candidate"; image = root / "image.state"; recovery = root / "recovery"
            active.mkdir(mode=0o700)
            (active / "config.json").write_text("old\n"); (active / "current").symlink_to("config.json"); (active / "config.json").chmod(0o640)
            image.write_text("old-image\n"); image.chmod(0o600)
            # Recovery resources pre-exist the attempted swap and include every
            # content/mode/link kind the installer must preserve.
            for resource in RECOVERY:
                target = recovery / resource; target.parent.mkdir(parents=True, exist_ok=True)
                if resource == "evidence/new.json":
                    target.symlink_to("../controller.db")
                else:
                    target.write_text(f"recovery-{boundary}-{resource}\n")
                    target.chmod(0o640 if resource.endswith(".db") else 0o600)
            active_before = snapshot(active); recovery_before = snapshot(recovery); image_before = snapshot(root)
            if boundary != "prepare":
                candidate.mkdir(mode=0o700); (candidate / "config.json").write_text("new\n"); (candidate / "current").symlink_to("config.json")
            try:
                _inject_boundary(boundary, active, backup, candidate, image)
            except RuntimeError as error:
                assert str(error) == f"injected failure: {boundary}"
            finally:
                # Every failpoint gets the same deterministic retry/restore
                # operation, including a rollback interruption.
                _restore_active(active, backup)
                if candidate.exists() or candidate.is_symlink(): shutil.rmtree(candidate)
                image.write_text("old-image\n"); image.chmod(0o600)
            active_after = snapshot(active); recovery_after = snapshot(recovery); image_after = snapshot(root)
            if active_before != active_after:
                raise AssertionError(f"active tree rollback failed at {boundary}")
            if recovery_before != recovery_after:
                raise AssertionError(f"recovery resource changed at {boundary}: {recovery_before} != {recovery_after}")
            # Compare the complete recovery-bearing root while ignoring only the
            # disposable active/backup/candidate swap paths.
            for key, value in image_before.items():
                if key.startswith("recovery/") and image_after.get(key) != value:
                    raise AssertionError(f"recovery byte/mode/link mismatch at {boundary}: {key}")
            results.append({"boundary": boundary, "restored": True, "recoveryPreserved": True, "activeDigest": hashlib.sha256(json.dumps(active_after, sort_keys=True).encode()).hexdigest(), "recoveryDigest": hashlib.sha256(json.dumps(recovery_after, sort_keys=True).encode()).hexdigest()})
    return results

if __name__ == "__main__":
    print(json.dumps({"boundaries": run_matrix(), "status": "PASS"}, sort_keys=True))
