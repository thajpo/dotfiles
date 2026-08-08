"""Capability-checked read tools for secretary, investigator, and reviewer roles."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
import re
from typing import Any

from .greenfield_store import GreenfieldStore
from .models import validate_id


class ScopedReadError(PermissionError):
    pass


def _no_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor or os.sep)
    for component in absolute.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ScopedReadError("symlink path components are not allowed")


class ScopedProjectReader:
    def __init__(self, store: Any, *, project_id: str, working_copy_id: str | None = None):
        validate_id(project_id, prefix="prj")
        self.store = store
        project = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if project is None:
            raise ScopedReadError("project is not registered")
        self.project_id = project_id
        if working_copy_id is None:
            row = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND kind='primary' ORDER BY created_at LIMIT 1", (project_id,)).fetchone()
        else:
            validate_id(working_copy_id, prefix="wc")
            row = store.conn.execute("SELECT * FROM working_copies WHERE project_id=? AND working_copy_id=?", (project_id, working_copy_id)).fetchone()
        if row is None:
            raise ScopedReadError("working copy is not registered in project")
        self.working_copy_id = str(row["working_copy_id"])
        self.root = Path(row["path"]).absolute()
        _no_symlink_components(self.root)
        if not self.root.is_dir():
            raise ScopedReadError("working copy is unavailable")

    def resolve(self, relative_path: str = ".") -> Path:
        if not isinstance(relative_path, str) or "\x00" in relative_path or Path(relative_path).is_absolute():
            raise ScopedReadError("path must be relative to the assigned working copy")
        candidate = (self.root / relative_path).absolute()
        _no_symlink_components(candidate.parent if not candidate.exists() else candidate)
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(self.root.resolve(strict=True))
        except ValueError as error:
            raise ScopedReadError("path escapes the assigned working copy") from error
        if resolved == self.root / ".git" or self.root / ".git" in resolved.parents:
            raise ScopedReadError("project Git metadata is not exposed as a normal file tree")
        return resolved

    def read(self, relative_path: str, *, max_bytes: int = 256 * 1024, start_line: int = 1, max_lines: int = 2000) -> dict[str, Any]:
        path = self.resolve(relative_path)
        if not path.is_file() or path.stat().st_size > max_bytes:
            raise ScopedReadError("file is not readable within the byte bound")
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if start_line < 1 or max_lines < 1 or max_lines > 10000:
            raise ScopedReadError("line bounds are invalid")
        selected = lines[start_line - 1 : start_line - 1 + max_lines]
        return {"projectId": self.project_id, "workingCopyId": self.working_copy_id, "path": relative_path, "startLine": start_line, "lines": selected, "truncated": len(selected) < max(0, len(lines) - start_line + 1)}

    def list(self, relative_path: str = ".", *, pattern: str = "*", max_entries: int = 256) -> list[dict[str, Any]]:
        path = self.resolve(relative_path)
        if not path.is_dir():
            raise ScopedReadError("path is not a directory")
        if len(pattern) > 256 or max_entries < 1 or max_entries > 1000:
            raise ScopedReadError("directory listing bounds are invalid")
        entries = []
        for item in sorted(path.iterdir(), key=lambda p: p.name):
            if item.name == ".git" or not fnmatch.fnmatch(item.name, pattern):
                continue
            entries.append({"name": item.name, "kind": "directory" if item.is_dir() else "file"})
            if len(entries) >= max_entries:
                break
        return entries

    def grep(self, pattern: str, relative_path: str = ".", *, max_matches: int = 200) -> list[dict[str, Any]]:
        if not isinstance(pattern, str) or not pattern or len(pattern) > 512:
            raise ScopedReadError("grep pattern is invalid")
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            raise ScopedReadError("grep pattern is invalid") from error
        root = self.resolve(relative_path)
        files = [root] if root.is_file() else [item for item in root.rglob("*") if item.is_file() and ".git" not in item.parts]
        matches: list[dict[str, Any]] = []
        for path in files[:1000]:
            try:
                for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if compiled.search(line):
                        matches.append({"path": str(path.relative_to(self.root)), "line": line_number, "text": line[:4096]})
                        if len(matches) >= max_matches:
                            return matches
            except OSError:
                continue
        return matches


__all__ = ["ScopedProjectReader", "ScopedReadError"]
