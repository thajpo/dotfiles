"""Descriptor-rooted read and Git queries for host read-only roles."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import stat
import subprocess
import time
from typing import Any, Mapping

from .models import validate_id


MAX_FILE_BYTES = 256 * 1024
MAX_FILE_LINES = 10_000
MAX_LIST_ENTRIES = 1_000
MAX_GREP_MATCHES = 1_000
MAX_GREP_FILES = 1_000
MAX_TREE_ENTRIES = 4_096
MAX_TREE_DEPTH = 128
MAX_GREP_BYTES = 8 * 1024 * 1024
MAX_GIT_BYTES = 512 * 1024
MAX_PATH_BYTES = 4_096
_OPEN_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_OPEN_FILE = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC


class ScopedReadError(PermissionError):
    pass


def _parts(value: str, *, allow_root: bool = True, expose_git: bool = False) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value or len(value.encode("utf-8")) > MAX_PATH_BYTES:
        raise ScopedReadError("path is invalid")
    pure = PurePosixPath(value)
    if pure.is_absolute() or value != pure.as_posix():
        raise ScopedReadError("path must be canonical and relative to the assigned working copy")
    parts = tuple(part for part in pure.parts if part != ".")
    if (not allow_root and not parts) or any(part in {"", ".", ".."} for part in parts):
        raise ScopedReadError("path escapes the assigned working copy")
    if not expose_git and ".git" in parts:
        raise ScopedReadError("project Git metadata is not exposed as a normal file tree")
    return parts


def _open_absolute_directory(path: Path) -> int:
    value = path.absolute()
    if not value.is_absolute() or ".." in value.parts:
        raise ScopedReadError("assigned root is invalid")
    descriptor = os.open(value.anchor, _OPEN_DIRECTORY)
    try:
        for component in value.parts[1:]:
            following = os.open(component, _OPEN_DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


class ScopedProjectReader:
    """Open one assigned root once and perform every tree access relative to it."""

    def __init__(
        self,
        store: Any,
        *,
        project_id: str,
        working_copy_id: str | None = None,
        expected_root: str | None = None,
        expected_head_oid: str | None = None,
        expected_tree_oid: str | None = None,
    ):
        validate_id(project_id, prefix="prj")
        project = store.conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if project is None:
            raise ScopedReadError("project is not registered")
        if working_copy_id is None:
            row = store.conn.execute(
                "SELECT * FROM working_copies WHERE project_id=? AND kind='primary' ORDER BY created_at LIMIT 1",
                (project_id,),
            ).fetchone()
        else:
            validate_id(working_copy_id, prefix="wc")
            row = store.conn.execute(
                "SELECT * FROM working_copies WHERE project_id=? AND working_copy_id=?",
                (project_id, working_copy_id),
            ).fetchone()
        if row is None:
            raise ScopedReadError("working copy is not registered in project")
        root = Path(str(row["path"])).absolute()
        if expected_root is not None and str(root) != expected_root:
            raise ScopedReadError("assigned root differs from the authenticated manifest")
        try:
            root_fd = _open_absolute_directory(root)
        except OSError as error:
            raise ScopedReadError("working copy root is unavailable or symlinked") from error
        info = os.fstat(root_fd)
        if not stat.S_ISDIR(info.st_mode):
            os.close(root_fd)
            raise ScopedReadError("working copy root is not a directory")
        self.store = store
        self.project_id = project_id
        self.working_copy_id = str(row["working_copy_id"])
        self.root = root
        self._root_fd = root_fd
        self._root_identity = info
        self._project_git_identity = (int(project["git_common_device"]), int(project["git_common_inode"]))
        self._git_common_path = Path(str(project["git_common_dir"])).absolute()
        self._git_common_fd = -1
        self._git_dir_fd = -1
        self.expected_head_oid = expected_head_oid if expected_head_oid is not None else row["expected_head_oid"]
        self.expected_tree_oid = expected_tree_oid if expected_tree_oid is not None else row["expected_tree_oid"]
        try:
            self._git_common_fd = _open_absolute_directory(self._git_common_path)
            common_info = os.fstat(self._git_common_fd)
            if (common_info.st_dev, common_info.st_ino) != self._project_git_identity:
                raise ScopedReadError("registered Git common directory was replaced")
            self._git_dir_fd = self._open_assigned_git_directory(row)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for name in ("_git_dir_fd", "_git_common_fd", "_root_fd"):
            descriptor = getattr(self, name, -1)
            setattr(self, name, -1)
            if descriptor >= 0:
                os.close(descriptor)

    def __enter__(self) -> "ScopedProjectReader":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass

    def _assert_root_current(self) -> None:
        if self._root_fd < 0:
            raise ScopedReadError("scoped reader is closed")
        try:
            current = _open_absolute_directory(self.root)
        except OSError as error:
            raise ScopedReadError("assigned working copy root was replaced") from error
        try:
            if not _same_inode(self._root_identity, os.fstat(current)):
                raise ScopedReadError("assigned working copy root was replaced")
        finally:
            os.close(current)

    def _directory(self, parts: tuple[str, ...]) -> int:
        descriptor = os.dup(self._root_fd)
        try:
            for component in parts:
                following = os.open(component, _OPEN_DIRECTORY, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = following
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _file(self, parts: tuple[str, ...]) -> tuple[int, os.stat_result]:
        if not parts:
            raise ScopedReadError("path is not a regular file")
        parent = self._directory(parts[:-1])
        try:
            descriptor = os.open(parts[-1], _OPEN_FILE, dir_fd=parent)
        finally:
            os.close(parent)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            os.close(descriptor)
            raise ScopedReadError("path is not a regular file")
        return descriptor, info

    def _open_assigned_git_directory(self, working: Mapping[str, Any]) -> int:
        value = Path(str(working["git_dir"] or self._git_common_path)).absolute()
        if value == self.root / ".git":
            try:
                return self._directory((".git",))
            except OSError:
                descriptor, info = self._file((".git",))
                try:
                    if info.st_size > 4_096:
                        raise ScopedReadError("working-tree Git pointer exceeds its bound")
                    text = self._read_bytes(descriptor, 4_097).decode("utf-8", errors="strict").strip()
                finally:
                    os.close(descriptor)
                if not text.startswith("gitdir: "):
                    raise ScopedReadError("working-tree Git pointer is invalid")
                value = Path(text[8:])
        if not value.is_absolute() or str(value) != os.path.normpath(str(value)):
            raise ScopedReadError("assigned Git directory is not canonical")
        if value != self._git_common_path and not value.is_relative_to(self._git_common_path / "worktrees"):
            raise ScopedReadError("assigned Git directory crosses repository identity")
        try:
            return _open_absolute_directory(value)
        except OSError as error:
            raise ScopedReadError("assigned Git directory is unavailable or symlinked") from error

    def _assert_git_storage_current(self) -> None:
        try:
            descriptor = _open_absolute_directory(self._git_common_path)
        except OSError as error:
            raise ScopedReadError("registered Git common directory was replaced") from error
        try:
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) != self._project_git_identity or not _same_inode(info, os.fstat(self._git_common_fd)):
                raise ScopedReadError("registered Git common directory was replaced")
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_bytes(descriptor: int, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            block = os.read(descriptor, min(64 * 1024, size - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        return bytes(chunks)

    def read(self, relative_path: str, *, max_bytes: int = MAX_FILE_BYTES, start_line: int = 1, max_lines: int = 2_000) -> dict[str, Any]:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= MAX_FILE_BYTES:
            raise ScopedReadError("file byte bound is invalid")
        if not isinstance(start_line, int) or isinstance(start_line, bool) or start_line < 1 or not isinstance(max_lines, int) or isinstance(max_lines, bool) or not 1 <= max_lines <= MAX_FILE_LINES:
            raise ScopedReadError("line bounds are invalid")
        parts = _parts(relative_path, allow_root=False)
        self._assert_root_current()
        try:
            descriptor, info = self._file(parts)
        except OSError as error:
            raise ScopedReadError("file is unavailable or crosses a symlink") from error
        try:
            if info.st_size > max_bytes:
                raise ScopedReadError("file exceeds the byte bound")
            body = self._read_bytes(descriptor, max_bytes + 1)
            if len(body) > max_bytes:
                raise ScopedReadError("file exceeds the byte bound")
            text = body.decode("utf-8", errors="replace")
        finally:
            os.close(descriptor)
        self._assert_root_current()
        lines = text.splitlines()
        selected = lines[start_line - 1 : start_line - 1 + max_lines]
        return {
            "projectId": self.project_id,
            "workingCopyId": self.working_copy_id,
            "path": "/".join(parts),
            "startLine": start_line,
            "lines": selected,
            "truncated": start_line - 1 + len(selected) < len(lines),
        }

    def list(self, relative_path: str = ".", *, pattern: str = "*", max_entries: int = 256) -> list[dict[str, Any]]:
        if not isinstance(pattern, str) or not pattern or len(pattern.encode("utf-8")) > 256 or "\x00" in pattern:
            raise ScopedReadError("directory pattern is invalid")
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or not 1 <= max_entries <= MAX_LIST_ENTRIES:
            raise ScopedReadError("directory listing bound is invalid")
        parts = _parts(relative_path)
        self._assert_root_current()
        try:
            descriptor = self._directory(parts)
        except OSError as error:
            raise ScopedReadError("directory is unavailable or crosses a symlink") from error
        entries: list[dict[str, Any]] = []
        try:
            with os.scandir(descriptor) as iterator:
                for item in iterator:
                    if item.name == ".git" or not fnmatch.fnmatch(item.name, pattern):
                        continue
                    try:
                        info = item.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "symlink" if stat.S_ISLNK(info.st_mode) else "other"
                    entries.append({"name": item.name, "kind": kind})
                    if len(entries) >= max_entries:
                        break
        finally:
            os.close(descriptor)
        self._assert_root_current()
        return sorted(entries, key=lambda item: item["name"])

    def _grep_file(self, descriptor: int, relative: str, compiled: re.Pattern[str], matches: list[dict[str, Any]], budget: dict[str, int], max_matches: int) -> None:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES or budget["bytes"] + info.st_size > MAX_GREP_BYTES:
            return
        budget["files"] += 1
        budget["bytes"] += info.st_size
        body = self._read_bytes(descriptor, MAX_FILE_BYTES + 1)
        if len(body) > MAX_FILE_BYTES:
            return
        text = body.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), 1):
            if compiled.search(line[:4096]):
                matches.append({"path": relative, "line": line_number, "text": line[:4096]})
                if len(matches) >= max_matches:
                    return

    def _grep_directory(self, descriptor: int, prefix: tuple[str, ...], compiled: re.Pattern[str], matches: list[dict[str, Any]], budget: dict[str, int], max_matches: int, depth: int) -> None:
        if depth > MAX_TREE_DEPTH:
            raise ScopedReadError("grep tree exceeds the depth bound")
        with os.scandir(descriptor) as iterator:
            for item in iterator:
                budget["entries"] += 1
                if budget["entries"] > MAX_TREE_ENTRIES:
                    raise ScopedReadError("grep tree exceeds the entry bound")
                if item.name == ".git":
                    continue
                try:
                    info = item.stat(follow_symlinks=False)
                except OSError:
                    continue
                path = (*prefix, item.name)
                if stat.S_ISLNK(info.st_mode):
                    continue
                if stat.S_ISREG(info.st_mode):
                    if budget["files"] >= MAX_GREP_FILES or len(matches) >= max_matches:
                        return
                    try:
                        child = os.open(item.name, _OPEN_FILE, dir_fd=descriptor)
                    except OSError:
                        continue
                    try:
                        self._grep_file(child, "/".join(path), compiled, matches, budget, max_matches)
                    finally:
                        os.close(child)
                elif stat.S_ISDIR(info.st_mode):
                    try:
                        child = os.open(item.name, _OPEN_DIRECTORY, dir_fd=descriptor)
                    except OSError:
                        continue
                    try:
                        self._grep_directory(child, path, compiled, matches, budget, max_matches, depth + 1)
                    finally:
                        os.close(child)
                if budget["files"] >= MAX_GREP_FILES or budget["bytes"] >= MAX_GREP_BYTES or len(matches) >= max_matches:
                    return

    def grep(self, pattern: str, relative_path: str = ".", *, max_matches: int = 200) -> list[dict[str, Any]]:
        if not isinstance(pattern, str) or not pattern or len(pattern.encode("utf-8")) > 512 or "\x00" in pattern:
            raise ScopedReadError("grep pattern is invalid")
        if not isinstance(max_matches, int) or isinstance(max_matches, bool) or not 1 <= max_matches <= MAX_GREP_MATCHES:
            raise ScopedReadError("grep result bound is invalid")
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            raise ScopedReadError("grep pattern is invalid") from error
        parts = _parts(relative_path)
        self._assert_root_current()
        matches: list[dict[str, Any]] = []
        budget = {"entries": 0, "files": 0, "bytes": 0}
        try:
            try:
                descriptor, _ = self._file(parts)
                is_file = True
            except (OSError, ScopedReadError):
                descriptor = self._directory(parts)
                is_file = False
            try:
                if is_file:
                    self._grep_file(descriptor, "/".join(parts), compiled, matches, budget, max_matches)
                else:
                    self._grep_directory(descriptor, parts, compiled, matches, budget, max_matches, 0)
            finally:
                os.close(descriptor)
        except ScopedReadError:
            raise
        except OSError as error:
            raise ScopedReadError("grep path is unavailable or crosses a symlink") from error
        self._assert_root_current()
        return matches

    def _git_environment(self) -> dict[str, str]:
        return {
            "PATH": os.defpath,
            "HOME": "/nonexistent",
            "XDG_CONFIG_HOME": "/nonexistent",
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_EDITOR": "false",
            "GIT_ASKPASS": "false",
            "SSH_ASKPASS": "false",
            "GIT_DIR": f"/proc/self/fd/{self._git_dir_fd}",
            "GIT_COMMON_DIR": f"/proc/self/fd/{self._git_common_fd}",
            "GIT_WORK_TREE": f"/proc/self/fd/{self._root_fd}",
        }

    def _run_git(self, args: list[str], *, max_bytes: int = MAX_GIT_BYTES, timeout: float = 30.0) -> str:
        git = shutil.which("git", path=os.defpath)
        if git is None:
            raise ScopedReadError("Git is unavailable")
        command = [
            git,
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false",
            "-c", "core.sshCommand=false",
            "-c", "credential.helper=",
            "-c", "protocol.allow=never",
            "-c", "diff.external=",
            *args,
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=f"/proc/self/fd/{self._root_fd}",
                env=self._git_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                pass_fds=(self._root_fd, self._git_common_fd, self._git_dir_fd),
            )
        except OSError as error:
            raise ScopedReadError("Git query is unavailable") from error
        selector = selectors.DefaultSelector()
        output = bytearray()
        error_output = bytearray()
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, output)
        selector.register(process.stderr, selectors.EVENT_READ, error_output)
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    raise ScopedReadError("Git query timed out")
                for key, _ in selector.select(remaining):
                    block = os.read(key.fileobj.fileno(), 64 * 1024)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    key.data.extend(block)
                    if len(output) + len(error_output) > max_bytes:
                        process.kill()
                        raise ScopedReadError("Git query output exceeds its bound")
            return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()
        if return_code != 0:
            detail = error_output.decode("utf-8", errors="replace").strip()[:512]
            raise ScopedReadError(detail or "Git query failed")
        return output.decode("utf-8", errors="replace")

    def _assert_git_identity(self) -> tuple[str, str]:
        if not self.expected_head_oid or not self.expected_tree_oid:
            raise ScopedReadError("assigned scope has no exact Git revision")
        self._assert_git_storage_current()
        identity = self._run_git(["rev-parse", "HEAD", "HEAD^{tree}"], max_bytes=8 * 1024)
        values = identity.splitlines()
        if values != [self.expected_head_oid, self.expected_tree_oid]:
            raise ScopedReadError("working copy is not at the authenticated revision")
        self._assert_git_storage_current()
        return self.expected_head_oid, self.expected_tree_oid

    def assert_revision(self, *, clean: bool = False) -> tuple[str, str]:
        self._assert_root_current()
        identity = self._assert_git_identity()
        if clean and self._run_git(["status", "--porcelain=v2", "--untracked-files=all"], max_bytes=64 * 1024):
            raise ScopedReadError("exact-revision scope is not clean")
        self._assert_root_current()
        return identity

    def git(self, query: str, *, path: str | None = None, mode: str = "revision", limit: int = 20) -> dict[str, Any]:
        if query not in {"status", "diff", "log", "show", "rev-parse"}:
            raise ScopedReadError("Git query operation is not allowed")
        if path is not None:
            path_value = "/".join(_parts(path, allow_root=False))
        else:
            path_value = None
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ScopedReadError("Git query limit is invalid")
        if mode not in {"revision", "working"}:
            raise ScopedReadError("Git diff mode is invalid")
        head, tree = self.assert_revision()
        if query == "status":
            args = ["status", "--porcelain=v2", "--branch", "--untracked-files=normal"]
        elif query == "diff":
            args = ["diff", "--no-ext-diff", "--no-textconv", "--patch", "--stat", head] if mode == "working" else ["show", "--format=", "--no-ext-diff", "--no-textconv", "--patch", "--stat", head]
            if path_value is not None:
                args.extend(["--", path_value])
        elif query == "log":
            args = ["log", "--no-decorate", f"--max-count={limit}", "--format=%H%x09%P%x09%aI%x09%s", head]
            if path_value is not None:
                args.extend(["--", path_value])
        elif query == "show":
            args = ["show", "--no-ext-diff", "--no-textconv", "--format=fuller", "--stat", "--patch", head] if path_value is None else ["show", f"{head}:{path_value}"]
        else:
            args = ["rev-parse", "HEAD", "HEAD^{tree}"]
        output = self._run_git(args)
        if self._assert_git_identity() != (head, tree):
            raise ScopedReadError("Git revision changed during the bounded query")
        self._assert_root_current()
        return {
            "projectId": self.project_id,
            "workingCopyId": self.working_copy_id,
            "query": query,
            "revision": head,
            "tree": tree,
            "output": output,
            "truncated": False,
        }


__all__ = [
    "MAX_FILE_BYTES", "MAX_GIT_BYTES", "MAX_TREE_ENTRIES",
    "ScopedProjectReader", "ScopedReadError",
]
