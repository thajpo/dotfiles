"""Pisec-owned Git object isolation and approved promotion."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
from typing import Any, Mapping

from .fsutil import _atomic_write, _secure_tree
from .models import NeedsAttentionError, validate_id


class GitObjectManager:
    def __init__(self, *, state_root: Path | str):
        self.state_root = Path(state_root)

    def materialize(self, scope: Mapping[str, Any]) -> Mapping[str, Any]:
        project_id = validate_id(scope["projectId"], prefix="prj")
        workstream_id = validate_id(scope["workstreamId"], prefix="ws")
        object_dir = Path(scope["privateGitObjectDir"]).absolute()
        expected_root = self.state_root / "git-objects" / project_id / workstream_id / "objects"
        if object_dir != expected_root.absolute():
            raise NeedsAttentionError("private Git object path is not the deterministic state path")
        raw_common_objects = Path(scope["gitCommonObjectDir"])
        try:
            raw_info = raw_common_objects.lstat()
        except OSError as error:
            raise NeedsAttentionError("approved common Git object directory is invalid") from error
        if stat.S_ISLNK(raw_info.st_mode) or not stat.S_ISDIR(raw_info.st_mode) or raw_info.st_uid != os.geteuid():
            raise NeedsAttentionError("approved common Git object directory is unsafe")
        # A group/other-writable object store is a host umask condition, not an ownership
        # decision: tighten it so every secretary heals the same repository instead of
        # rejecting the spawn.
        if raw_info.st_mode & 0o022:
            os.chmod(raw_common_objects, raw_info.st_mode & ~0o022)
        try:
            common_objects = raw_common_objects.resolve(strict=True)
        except OSError as error:
            raise NeedsAttentionError("approved common Git object directory is invalid") from error
        common_info = common_objects / "info"
        if common_info.is_symlink():
            raise NeedsAttentionError("common Git object info directory is a symlink")
        if not common_info.exists():
            common_info.mkdir(mode=0o700)
        common_info_info = common_info.lstat()
        if not stat.S_ISDIR(common_info_info.st_mode) or common_info_info.st_uid != os.geteuid():
            raise NeedsAttentionError("common Git object info directory is unsafe")
        if common_info_info.st_mode & 0o022:
            os.chmod(common_info, common_info_info.st_mode & ~0o022)
        _secure_tree(self.state_root, object_dir / "info")
        _secure_tree(self.state_root, object_dir / "pack")
        _atomic_write(object_dir / "info" / "alternates", str(common_objects) + "\n", mode=0o600)

        common_alternates = common_info / "alternates"
        if common_alternates.exists() or common_alternates.is_symlink():
            alternate_info = common_alternates.lstat()
            if stat.S_ISLNK(alternate_info.st_mode) or not stat.S_ISREG(alternate_info.st_mode) or alternate_info.st_uid != os.geteuid() or alternate_info.st_mode & 0o022:
                raise NeedsAttentionError("common Git alternates file is unsafe")
            existing = common_alternates.read_text().splitlines()
            managed_root = (self.state_root / "git-objects").absolute()
            retained = [
                line
                for line in existing
                if line and not (Path(line).is_absolute() and Path(line).is_relative_to(managed_root))
            ]
            if retained != existing:
                _atomic_write(common_alternates, "".join(f"{line}\n" for line in retained), mode=0o600)
        return {"object_dir": str(object_dir), "common_object_dir": str(common_objects)}

    def promote(self, *, worktree: Path, private_object_dir: Path, common_object_dir: Path) -> None:
        """Copy approved worker objects into the real repository object store."""
        for path in (worktree, private_object_dir, common_object_dir):
            if path.is_symlink():
                raise NeedsAttentionError("Git object promotion path is a symlink")
        source_objects = private_object_dir.resolve(strict=True)
        target_objects = common_object_dir.resolve(strict=True)
        if source_objects == target_objects:
            raise NeedsAttentionError("Git object promotion source and destination are identical")
        source_info = source_objects.lstat()
        target_info = target_objects.lstat()
        if not stat.S_ISDIR(source_info.st_mode) or source_info.st_uid != os.geteuid():
            raise NeedsAttentionError("private Git object directory is unsafe")
        if source_info.st_mode & 0o022:
            os.chmod(source_objects, source_info.st_mode & ~0o022)
        if not stat.S_ISDIR(target_info.st_mode) or target_info.st_uid != os.geteuid():
            raise NeedsAttentionError("common Git object directory is unsafe")
        if target_info.st_mode & 0o022:
            os.chmod(target_objects, target_info.st_mode & ~0o022)
        environment = os.environ.copy()
        environment.update({"GIT_OBJECT_DIRECTORY": str(target_objects), "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(source_objects)})
        result = subprocess.run(
            ["git", "-C", str(worktree), "pack-objects", "--revs", "--stdout"],
            input=b"--all\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=environment,
        )
        if result.returncode != 0:
            raise NeedsAttentionError("approved Git object promotion failed", detail={"stderr": result.stderr.decode("utf-8", "replace")[:256]})
        if not result.stdout:
            return
        index = subprocess.run(
            ["git", "-C", str(worktree), "index-pack", "--stdin", "--fix-thin", "--keep=pisec-promotion"],
            input=result.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env={**environment, "GIT_OBJECT_DIRECTORY": str(target_objects)},
        )
        if index.returncode != 0:
            raise NeedsAttentionError("approved Git object promotion index failed", detail={"stderr": index.stderr.decode("utf-8", "replace")[:256]})
