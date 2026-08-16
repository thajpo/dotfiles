"""Deterministic, disposable control-plane test infrastructure.

Nothing in this module is imported by installed/runtime code.  The helpers are
intended to make a controller operation observable without allowing a test to
accidentally use the caller's HOME, Git repository, or host processes.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import ctypes
import hashlib
import importlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence, runtime_checkable


# These are the external side-effect boundaries named by the Phase 1 contract.
# The suffixes are generated so callers cannot accidentally use a boundary name
# without an explicit before/after phase.
REGISTERED_FAILPOINT_BASE_NAMES: tuple[str, ...] = (
    "operation.intent",
    "lock.acquire",
    "git.ref_update",
    "worktree.create",
    "manifest.write",
    "runtime.create",
    "runtime.stop",
    "snapshot.ref",
    "change.revision",
    "integration.target_ref",
    "integration.index",
    "event.commit",
)
REGISTERED_FAILPOINT_NAMES: tuple[str, ...] = tuple(
    f"{base}.{phase}"
    for base in REGISTERED_FAILPOINT_BASE_NAMES
    for phase in ("before", "after")
)
FAILPOINT_NAMES = REGISTERED_FAILPOINT_NAMES
_FAILPOINT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.(?:before|after)$")


class InvalidFailpointName(ValueError):
    """Raised when a failpoint does not have the versioned boundary shape."""


class FailpointRaised(RuntimeError):
    """Default exception used by the test failpoint controller."""

    def __init__(self, name: str, context: Mapping[str, str]):
        self.name = name
        self.context = MappingProxyType(dict(context))
        super().__init__(f"failpoint fired: {name}")


@runtime_checkable
class FailpointController(Protocol):
    """The constructor-injected failpoint interface used by later phases."""

    def hit(self, name: str, context: Mapping[str, str]) -> None: ...


class NoOpFailpointController(FailpointController):
    """Production/default failpoint implementation; it never has side effects."""

    def hit(self, name: str, context: Mapping[str, str]) -> None:
        _validate_failpoint_name(name)
        # Deliberately do not retain or inspect context.  The default path must
        # not turn failpoints into an implicit event/telemetry channel.
        return None


DEFAULT_FAILPOINT_CONTROLLER = NoOpFailpointController()


def default_failpoint_controller() -> FailpointController:
    """Return the safe default controller used when tests inject nothing."""

    return DEFAULT_FAILPOINT_CONTROLLER


def _validate_failpoint_name(name: str) -> None:
    if not isinstance(name, str) or _FAILPOINT_NAME_RE.fullmatch(name) is None:
        raise InvalidFailpointName(
            f"failpoint name must match <operation>.<step>.before|after: {name!r}"
        )


def sanitize_failpoint_context(
    context: Mapping[str, Any] | None,
    *,
    max_items: int = 16,
    max_value_length: int = 192,
    max_total_length: int = 2048,
) -> dict[str, str]:
    """Copy, bound, and redact a failpoint context.

    Context is diagnostic only.  Keys are sorted and values are converted to
    bounded strings so callbacks cannot retain a caller-owned mutable mapping
    or accidentally receive an unbounded prompt/path/blob.  Secret-shaped keys
    are replaced before truncation.
    """

    if context is None:
        return {}
    if not isinstance(context, Mapping):
        raise TypeError("failpoint context must be a mapping")

    secret_key = re.compile(
        r"(?:pass(?:word)?|secret|token|credential|authorization|cookie|private.?key|capability)",
        re.IGNORECASE,
    )
    cleaned: dict[str, str] = {}
    total = 0
    for raw_key in sorted(context, key=lambda item: str(item)):
        if len(cleaned) >= max_items or total >= max_total_length:
            break
        key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(raw_key))[:80] or "_"
        if secret_key.search(key):
            value = "[redacted]"
        else:
            raw_value = context[raw_key]
            if isinstance(raw_value, (bytes, bytearray, memoryview)):
                value = base64.b64encode(bytes(raw_value)).decode("ascii")
            else:
                value = str(raw_value)
            value = value.replace("\x00", "\\0")
        remaining = max_total_length - total - len(key)
        if remaining <= 0:
            break
        value = value[: min(max_value_length, remaining)]
        cleaned[key] = value
        total += len(key) + len(value)
    return cleaned


class ConfiguredFailpointController(FailpointController):
    """Inject exactly one selected test action at one named boundary.

    Exactly one of ``raise_exception``, ``exit_code``, or ``callback`` must be
    supplied.  The selected boundary fires at most once; later hits are
    recorded but are no-ops.  The exit action is intentionally ``os._exit`` so
    it is suitable only in a child process launched by
    :func:`run_failpoint_child`.
    """

    def __init__(
        self,
        selected_name: str,
        *,
        raise_exception: BaseException | type[BaseException] | Callable[[str, Mapping[str, str]], BaseException] | None = None,
        exit_code: int | None = None,
        callback: Callable[[str, Mapping[str, str]], Any] | Callable[[Mapping[str, str]], Any] | None = None,
    ) -> None:
        _validate_failpoint_name(selected_name)
        actions = sum(
            value is not None
            for value in (raise_exception, exit_code, callback)
        )
        if actions != 1:
            raise ValueError(
                "exactly one failpoint action is required: raise_exception, exit_code, or callback"
            )
        if exit_code is not None and (not isinstance(exit_code, int) or not 0 <= exit_code <= 255):
            raise ValueError("exit_code must be an integer in the range 0..255")
        self.selected_name = selected_name
        self._raise_exception = raise_exception
        self._exit_code = exit_code
        self._callback = callback
        self._fired = False
        self._hits: list[tuple[str, dict[str, str]]] = []

    @property
    def fired(self) -> bool:
        return self._fired

    @property
    def hit_count(self) -> int:
        return sum(name == self.selected_name for name, _ in self._hits)

    @property
    def hits(self) -> tuple[tuple[str, Mapping[str, str]], ...]:
        return tuple((name, MappingProxyType(dict(context))) for name, context in self._hits)

    def hit(self, name: str, context: Mapping[str, str]) -> None:
        _validate_failpoint_name(name)
        safe_context = sanitize_failpoint_context(context)
        self._hits.append((name, safe_context))
        if name != self.selected_name or self._fired:
            return None
        self._fired = True
        if self._exit_code is not None:
            # This is reached only by an explicitly injected controller.  It
            # is never selected from environment state.
            os._exit(self._exit_code)
        if self._callback is not None:
            callback = self._callback
            # The documented callback shape is (name, context).  Supporting a
            # one-argument callback keeps tiny tests readable without catching
            # a TypeError raised *inside* the callback.
            try:
                signature = inspect.signature(callback)
                positional = [
                    parameter
                    for parameter in signature.parameters.values()
                    if parameter.kind
                    in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
                ]
                accepts_varargs = any(
                    parameter.kind == parameter.VAR_POSITIONAL
                    for parameter in signature.parameters.values()
                )
            except (TypeError, ValueError):
                positional = []
                accepts_varargs = True
            if accepts_varargs or len(positional) >= 2:
                callback(name, MappingProxyType(dict(safe_context)))  # type: ignore[misc]
            else:
                callback(MappingProxyType(dict(safe_context)))  # type: ignore[misc]
            return None
        assert self._raise_exception is not None
        action = self._raise_exception
        if isinstance(action, BaseException):
            raise action
        if isinstance(action, type) and issubclass(action, BaseException):
            raise action(name, safe_context) if action is FailpointRaised else action()
        if callable(action):
            exception = action(name, MappingProxyType(dict(safe_context)))
            if not isinstance(exception, BaseException):
                raise TypeError("raise_exception factory must return a BaseException")
            raise exception
        raise TypeError("unsupported raise_exception action")


# Friendly aliases for callers that prefer an explicit test-only name.
TestFailpointController = ConfiguredFailpointController
InjectedFailpointController = ConfiguredFailpointController


# Git environment controls that can select another repository, index, object
# database, config, hook, pager, editor, diff helper, transport, or prompt.
_GIT_PATH_CONTROLS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_EXEC_PATH",
        "GIT_TEMPLATE_DIR",
    }
)
_GIT_INJECTION_CONTROLS = frozenset(
    {
        "GIT_PAGER",
        "PAGER",
        "GIT_EDITOR",
        "GIT_SEQUENCE_EDITOR",
        "EDITOR",
        "VISUAL",
        "GIT_EXTERNAL_DIFF",
        "GIT_DIFF_OPTS",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_PROXY_COMMAND",
        "GIT_TERMINAL_PROMPT",
        "GIT_CONFIG",
        "GIT_ATTR_NOSYSTEM",
        "GIT_ATTR_SOURCE",
    }
)
_GIT_INJECTION_PREFIXES = (
    "GIT_CONFIG_",
    "GIT_TRACE",
    "GIT_CURL_",
    "GIT_HTTP_",
    "GIT_TEST_",
    "GIT_ATTR_",
)
_SAFE_ENV_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PYTHONPATH",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "TZ",
        "USER",
        "LOGNAME",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
        "GIT_AUTHOR_DATE",
        "GIT_COMMITTER_DATE",
    }
)
_SAFE_ENV_PREFIXES = ("PI_CONTROL_",)


def _trusted_temp_parent() -> Path:
    """Return a fixed, non-ambient parent for disposable fixture roots."""

    parent = Path("/tmp")
    if not parent.is_dir() or parent.is_symlink():
        raise RuntimeError("/tmp must be a real directory for isolated fixtures")
    return parent


_DISPOSABLE_ROOTS: dict[Path, tuple[int, int]] = {}


def safe_temporary_directory(*, prefix: str) -> tempfile.TemporaryDirectory[str]:
    """Create a temporary directory without consulting ambient TMPDIR."""

    if (
        not isinstance(prefix, str)
        or Path(prefix).name != prefix
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", prefix) is None
    ):
        raise ValueError("temporary prefix must be a bounded basename")
    temporary = tempfile.TemporaryDirectory(prefix=prefix, dir=str(_trusted_temp_parent()))
    root = Path(temporary.name).resolve()
    metadata = root.stat()
    _DISPOSABLE_ROOTS[root] = (metadata.st_dev, metadata.st_ino)
    return temporary


def _validated_disposable_root(
    fixture_root: os.PathLike[str] | str | None,
) -> Path:
    if fixture_root is None:
        raise GitSafetyError("an explicit disposable fixture root is required")
    raw_root = Path(fixture_root)
    if raw_root.is_symlink():
        raise GitSafetyError(f"fixture root must not be a symlink: {fixture_root}")
    root = raw_root.resolve()
    trusted_parent = _trusted_temp_parent().resolve()
    if root not in _DISPOSABLE_ROOTS and os.environ.get("PI_CONTROL_FIXTURE_ROOT"):
        _register_child_fixture_root_from_environment()
    if (
        root == trusted_parent
        or not root.is_relative_to(trusted_parent)
        or not root.is_dir()
        or root not in _DISPOSABLE_ROOTS
    ):
        raise GitSafetyError(f"fixture root is not a registered disposable /tmp directory: {fixture_root}")
    metadata = root.stat()
    if _DISPOSABLE_ROOTS[root] != (metadata.st_dev, metadata.st_ino):
        raise GitSafetyError(f"fixture root identity changed: {fixture_root}")
    return root


def _require_fixture_cwd(
    cwd: os.PathLike[str] | str,
    fixture_root: os.PathLike[str] | str | None,
) -> Path:
    root = _validated_disposable_root(fixture_root)
    cwd_path = Path(cwd).resolve()
    if not cwd_path.is_dir() or not cwd_path.is_relative_to(root):
        raise GitSafetyError(f"Git cwd is outside fixture root: {cwd}")
    return cwd_path


_CHILD_FIXTURE_ROOT_KEYS = (
    "PI_CONTROL_FIXTURE_ROOT",
    "PI_CONTROL_FIXTURE_ROOT_DEV",
    "PI_CONTROL_FIXTURE_ROOT_INO",
)


def _register_child_fixture_root_from_environment() -> None:
    raw_root = os.environ.get("PI_CONTROL_FIXTURE_ROOT")
    raw_device = os.environ.get("PI_CONTROL_FIXTURE_ROOT_DEV")
    raw_inode = os.environ.get("PI_CONTROL_FIXTURE_ROOT_INO")
    if raw_root is None and raw_device is None and raw_inode is None:
        return
    if not raw_root or raw_device is None or raw_inode is None:
        raise GitSafetyError("incomplete child fixture-root authorization")
    path = Path(raw_root)
    if path.is_symlink():
        raise GitSafetyError("child fixture root authorization names a symlink")
    root = path.resolve()
    trusted_parent = _trusted_temp_parent().resolve()
    if root == trusted_parent or not root.is_relative_to(trusted_parent) or not root.is_dir():
        raise GitSafetyError("child fixture root authorization is outside disposable /tmp")
    metadata = root.stat()
    try:
        expected_identity = (int(raw_device), int(raw_inode))
    except ValueError as error:
        raise GitSafetyError("invalid child fixture-root identity") from error
    if expected_identity != (metadata.st_dev, metadata.st_ino):
        raise GitSafetyError("child fixture-root identity changed")
    _DISPOSABLE_ROOTS[root] = expected_identity


def _trusted_git_executable() -> str:
    # Resolve Git using Python's fixed default search path, not a caller's
    # potentially repository-controlled PATH.  The returned absolute path is
    # used for every fixture subprocess.
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise FileNotFoundError("Git is required for the control-plane fixtures")
    return executable


_TRUSTED_NOOP_EXECUTABLE = shutil.which("true", path=os.defpath) or "true"
_CHILD_SUBREAPER_READY = False
_MAX_SUBPROCESS_OUTPUT = 256 * 1024
_OUTPUT_LIMIT_EXIT_CODE = 125
_MAX_CHILD_PAYLOAD = 64 * 1024
_ALLOWED_GIT_COMMANDS = frozenset(
    {
        "add",
        "commit",
        "config",
        "diff",
        "for-each-ref",
        "init",
        "rev-parse",
        "status",
        "symbolic-ref",
        "var",
        "worktree",
    }
)
_FORBIDDEN_GIT_COMMANDS = frozenset(
    {
        "archive",
        "clone",
        "fetch",
        "ls-remote",
        "pull",
        "push",
        "receive-pack",
        "send-pack",
        "submodule",
        "upload-pack",
    }
)

_SAFE_LOCAL_CONFIG_ARGS: tuple[str, ...] = (
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "core.fsmonitor=false",
    "-c",
    f"diff.external={_TRUSTED_NOOP_EXECUTABLE}",
    "-c",
    "core.sshCommand=",
    "-c",
    "credential.helper=",
    "-c",
    f"core.attributesFile={os.devnull}",
    "-c",
    f"core.excludesFile={os.devnull}",
)


def sanitize_git_environment(
    environ: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Return a copied, deterministic environment for Git subprocesses.

    Repository/index/object/config paths and executable hooks/helpers are not
    inherited.  System and global config are explicitly disabled, PATH is
    reduced to the platform default search path, the pager and editor are
    non-interactive, and locale/terminal behavior is stable. Local repository
    config remains readable for fixture identity/remote metadata, while
    ``git_run`` adds explicit command-line overrides for hook, fsmonitor,
    external-diff (to a trusted no-op), SSH, credential, attribute, and exclude
    execution surfaces.
    """

    source: Mapping[str, Any] = os.environ if environ is None else environ
    clean: dict[str, str] = {}
    for raw_key, raw_value in source.items():
        key = str(raw_key)
        if key in _GIT_PATH_CONTROLS or key in _GIT_INJECTION_CONTROLS:
            continue
        if key.startswith(_GIT_INJECTION_PREFIXES):
            continue
        if key not in _SAFE_ENV_KEYS and not key.startswith(_SAFE_ENV_PREFIXES):
            continue
        clean[key] = str(raw_value)

    # These values are safe neutralizers, not caller-selected config paths.
    clean.update(
        {
            # The Git executable is passed by absolute path; this PATH is only
            # for standard subprocess helpers and cannot select a repository
            # or user-provided executable from the caller environment.
            "PATH": os.defpath,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_EDITOR": "true",
            "GIT_SEQUENCE_EDITOR": "true",
            "EDITOR": "true",
            "VISUAL": "true",
            "GIT_TERMINAL_PROMPT": "0",
            # Prevent read-only status snapshots from refreshing/writing a
            # caller's index. Required locks used by fixture commits still work.
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
            "LANG": "C",
            "TZ": "UTC",
        }
    )
    return clean


def _ensure_child_subreaper() -> None:
    """Make this helper process able to reap orphaned group descendants on Linux."""

    global _CHILD_SUBREAPER_READY
    if _CHILD_SUBREAPER_READY or sys.platform != "linux":
        return
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    prctl.restype = ctypes.c_int
    if prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    _CHILD_SUBREAPER_READY = True


def _reap_adopted_processes(process_ids: Iterable[int], deadline: float) -> set[int]:
    pending = set(process_ids)
    while pending and time.monotonic() < deadline:
        for process_id in tuple(pending):
            try:
                waited, _status = os.waitpid(process_id, os.WNOHANG)
            except ChildProcessError:
                pending.discard(process_id)
                continue
            except OSError:
                continue
            if waited == process_id:
                pending.discard(process_id)
        if pending:
            time.sleep(0.01)
    return pending


def _process_table() -> dict[int, tuple[int, int, str, int]]:
    table: dict[int, tuple[int, int, str, int]] = {}
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return table
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat_text = (entry / "stat").read_text(encoding="utf-8")
            closing = stat_text.rfind(")")
            fields = stat_text[closing + 2 :].split()
            if len(fields) >= 20:
                table[int(entry.name)] = (
                    int(fields[1]),
                    int(fields[2]),
                    fields[0],
                    int(fields[19]),
                )
        except (OSError, ValueError):
            continue
    return table


def _process_group_members(process_group: int) -> list[int]:
    """Observe members of an isolated Linux process group without guessing."""

    return sorted(
        process_id
        for process_id, (_parent, group, state, _start) in _process_table().items()
        if group == process_group and state not in {"Z", "X"}
    )


def _processes_holding_pipes(pipe_inodes: Iterable[int]) -> dict[int, int]:
    wanted = {f"pipe:[{inode}]" for inode in pipe_inodes}
    if not wanted:
        return {}
    table = _process_table()
    result: dict[int, int] = {}
    for process_id, (_parent, _group, state, start_time) in table.items():
        if process_id == os.getpid() or state in {"Z", "X"}:
            continue
        fd_root = Path("/proc") / str(process_id) / "fd"
        try:
            for descriptor in fd_root.iterdir():
                try:
                    if os.readlink(descriptor) in wanted:
                        result[process_id] = start_time
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return result


def _process_descendants(root_pid: int) -> dict[int, int]:
    table = _process_table()
    children: dict[int, list[int]] = {}
    for process_id, (parent, _group, _state, _start) in table.items():
        children.setdefault(parent, []).append(process_id)
    result: dict[int, int] = {}
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        for process_id in children.get(parent, []):
            if process_id in result:
                continue
            state = table[process_id][2]
            if state not in {"Z", "X"}:
                result[process_id] = table[process_id][3]
                pending.append(process_id)
    return result


def _new_adopted_processes(
    baseline: Mapping[int, int],
    *,
    excluded: Iterable[int] = (),
) -> dict[int, int]:
    excluded_ids = set(excluded)
    table = _process_table()
    return {
        process_id: start_time
        for process_id, (parent, _group, state, start_time) in table.items()
        if parent == os.getpid()
        and process_id not in excluded_ids
        and state not in {"Z", "X"}
        and (process_id not in baseline or baseline[process_id] != start_time)
    }


def _live_known_descendants(known: Mapping[int, int]) -> dict[int, int]:
    table = _process_table()
    return {
        process_id: start_time
        for process_id, start_time in known.items()
        if process_id in table
        and table[process_id][3] == start_time
        and table[process_id][2] not in {"Z", "X"}
    }


def _terminate_known_descendants(known: Mapping[int, int]) -> None:
    table = _process_table()
    for process_id, start_time in known.items():
        info = table.get(process_id)
        if info is None or info[3] != start_time or info[2] in {"Z", "X"}:
            continue
        try:
            os.kill(process_id, signal.SIGTERM)
        except ProcessLookupError:
            continue


def _terminate_process_group(
    process: subprocess.Popen[bytes],
    process_group: int,
) -> None:
    """Gracefully terminate only the helper-owned process group."""

    if hasattr(os, "killpg"):
        if process.poll() is None:
            current_group = os.getpgid(process.pid)
            if current_group != process_group or process_group != process.pid:
                raise RuntimeError("helper process group identity changed")
        try:
            os.killpg(process_group, signal.SIGTERM)
            return
        except ProcessLookupError:
            return
    if process.poll() is None:
        process.terminate()


def _bounded_process(
    command: Sequence[str],
    *,
    cwd: os.PathLike[str] | str,
    environ: Mapping[str, Any],
    timeout: float,
    max_output: int = _MAX_SUBPROCESS_OUTPUT,
) -> tuple[int, bytes, bytes]:
    """Run a helper-owned process with bounded pipes and graceful timeout.

    stdout and stderr are drained concurrently with ``selectors`` and never
    retain more than ``max_output`` bytes each. Timeout/output overflow uses
    SIGTERM for the isolated process group and verified descendant identities.
    Uncooperative survivors cause a bounded ``RuntimeError`` rather than a
    successful observation; this fixture substrate never sends SIGKILL and
    never targets a caller-supplied PID.
    """

    if (
        not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or timeout <= 0
    ):
        raise ValueError("process timeout must be a positive finite number")
    if not isinstance(max_output, int) or not 1 <= max_output <= _MAX_SUBPROCESS_OUTPUT:
        raise ValueError(f"max_output must be in the range 1..{_MAX_SUBPROCESS_OUTPUT}")
    _ensure_child_subreaper()
    baseline_children = {
        process_id: start_time
        for process_id, (parent, _group, state, start_time) in _process_table().items()
        if parent == os.getpid() and state not in {"Z", "X"}
    }
    process = subprocess.Popen(
        list(command),
        cwd=os.fspath(cwd),
        env=dict(environ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
    )
    process_group = process.pid
    if hasattr(os, "getpgid") and os.getpgid(process.pid) != process_group:
        process.terminate()
        process.wait()
        raise RuntimeError("helper process was not created in its own process group")
    selector = selectors.DefaultSelector()
    streams: dict[int, str] = {}
    pipe_inodes: set[int] = set()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = False
    timed_out = False
    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        if stream is None:
            continue
        os.set_blocking(stream.fileno(), False)
        pipe_inodes.add(os.fstat(stream.fileno()).st_ino)
        selector.register(stream, selectors.EVENT_READ, name)
        streams[stream.fileno()] = name

    deadline = time.monotonic() + float(timeout)
    termination_deadline: float | None = None
    drain_deadline: float | None = None
    known_descendants: dict[int, int] = {}

    def request_termination(reason: str) -> None:
        nonlocal termination_deadline, timed_out
        running = process.poll() is None
        if reason == "timeout" and running:
            timed_out = True
        if running:
            _terminate_process_group(process, process_group)
        if termination_deadline is None:
            termination_deadline = time.monotonic() + 1.0

    try:
        while selector.get_map():
            known_descendants.update(_process_descendants(process.pid))
            known_descendants.update(
                _new_adopted_processes(baseline_children, excluded=(process.pid,))
            )
            known_descendants.update(_processes_holding_pipes(pipe_inodes))
            known_descendants.pop(process.pid, None)
            now = time.monotonic()
            if termination_deadline is not None:
                remaining = termination_deadline - now
            elif process.poll() is not None:
                # The direct child has exited. Drain inherited pipe handles
                # briefly, but do not let an unrelated grandchild keep a
                # successful observation open until the full command timeout.
                if drain_deadline is None:
                    drain_deadline = now + 0.25
                remaining = drain_deadline - now
            else:
                remaining = deadline - now
                if remaining <= 0:
                    request_termination("timeout")
                    continue
            if remaining <= 0:
                break
            events = selector.select(max(0.0, min(remaining, 0.25)))
            if not events:
                continue
            for key, _mask in events:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    try:
                        selector.unregister(stream)
                    except Exception:
                        pass
                    stream.close()
                    continue
                name = str(key.data)
                available = max_output - len(buffers[name])
                if available > 0:
                    buffers[name].extend(chunk[:available])
                if len(chunk) > available:
                    truncated = True
                    request_termination("output")
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    if process.poll() is None:
        request_termination("timeout")
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired as error:
            remaining_members = _process_group_members(process_group)
            known_live = _live_known_descendants(known_descendants)
            if remaining_members:
                _terminate_process_group(process, process_group)
            _terminate_known_descendants(known_live)
            raise RuntimeError(
                "helper process or descendant ignored bounded graceful termination: "
                f"groups={remaining_members!r}, known={sorted(known_live)!r}"
            ) from error
    returncode = process.wait()
    known_descendants.update(
        _new_adopted_processes(baseline_children, excluded=(process.pid,))
    )
    known_descendants.update(_processes_holding_pipes(pipe_inodes))
    remaining_members = _process_group_members(process_group)
    known_live = _live_known_descendants(known_descendants)
    if remaining_members or known_live:
        if remaining_members:
            _terminate_process_group(process, process_group)
        _terminate_known_descendants(known_live)
        cleanup_deadline = time.monotonic() + 1.0
        _reap_adopted_processes((*remaining_members, *known_live), cleanup_deadline)
        while (remaining_members or known_live) and time.monotonic() < cleanup_deadline:
            time.sleep(0.01)
            remaining_members = _process_group_members(process_group)
            known_live = _live_known_descendants(known_descendants)
        if remaining_members or known_live:
            raise RuntimeError(
                f"helper descendants did not terminate: groups={remaining_members!r}, "
                f"known={sorted(known_live)!r}"
            )
    if truncated and returncode == 0:
        # A successful process with incomplete transport data is not a
        # successful observation.
        returncode = _OUTPUT_LIMIT_EXIT_CODE
    if timed_out and returncode != 0:
        notice = b"\\n[pi-control: process timed out after graceful termination]\\n"
        buffers["stderr"] = bytearray((bytes(buffers["stderr"]) + notice)[-max_output:])
    if truncated:
        notice = b"\\n[pi-control: process output truncated at transport bound]\\n"
        buffers["stderr"] = bytearray((bytes(buffers["stderr"]) + notice)[-max_output:])
    return returncode, bytes(buffers["stdout"]), bytes(buffers["stderr"])


def _git_subcommand(args: Sequence[str]) -> str:
    if not args or args[0].startswith("-"):
        raise ValueError("git_run requires an approved Git subcommand as its first argument")
    command = args[0]
    if command in _FORBIDDEN_GIT_COMMANDS:
        raise ValueError(f"remote/destructive Git command is forbidden in Phase 1: {command}")
    if command not in _ALLOWED_GIT_COMMANDS:
        raise ValueError(f"Git subcommand is not allowlisted for Phase 1: {command}")
    if any(argument == "-c" or argument.startswith("-c") for argument in args[1:]):
        raise ValueError("callers cannot override Git safety configuration")
    return command


def git_run(
    args: Sequence[os.PathLike[str] | str],
    *,
    cwd: os.PathLike[str] | str,
    environ: Mapping[str, Any] | None = None,
    check: bool = True,
    timeout: float = 30.0,
    fixture_root: os.PathLike[str] | str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one allowlisted local Git command with bounded output.

    The wrapper intentionally has no arbitrary ``subprocess`` kwargs: shell,
    pre-exec hooks, alternate executables, remote commands, and safety-config
    overrides are not part of the Phase 1 API.
    """

    converted_args = [os.fspath(argument) for argument in args]
    _validate_git_arguments(converted_args, cwd, fixture_root)
    _assert_safe_local_config(cwd, fixture_root=fixture_root)
    command = [
        _trusted_git_executable(),
        *_SAFE_LOCAL_CONFIG_ARGS,
        *converted_args,
    ]
    returncode, raw_stdout, raw_stderr = _bounded_process(
        command,
        cwd=cwd,
        environ=sanitize_git_environment(environ),
        timeout=timeout,
    )
    stdout = raw_stdout.decode("utf-8", errors="replace")
    stderr = raw_stderr.decode("utf-8", errors="replace")
    result = subprocess.CompletedProcess(command, returncode, stdout, stderr)
    if check and returncode:
        raise subprocess.CalledProcessError(
            returncode,
            command,
            output=stdout,
            stderr=stderr,
        )
    return result


run_git = git_run


class GitSafetyError(RuntimeError):
    """Raised before Git when local configuration or arguments are unsafe."""


def _reject_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor or os.sep)
    for component in absolute.parts[1:]:
        current /= component
        if current.is_symlink():
            raise GitSafetyError(f"Git metadata path contains a symlink: {current}")


def _assert_no_symlink_tree(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise GitSafetyError(f"Git metadata root is not a regular directory: {root}")
    try:
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in (*directories, *files):
                candidate = current_path / name
                if candidate.name in {"alternates", "http-alternates"}:
                    raise GitSafetyError(
                        f"Git metadata tree contains an unbound object alternate: {candidate}"
                    )
                if candidate.is_symlink():
                    raise GitSafetyError(
                        f"Git metadata tree contains a symlink: {candidate}"
                    )
    except OSError as error:
        raise GitSafetyError(f"cannot inspect Git metadata tree {root}: {error}") from error


def _resolve_linked_metadata_path(
    raw_path: str,
    *,
    base: Path,
    fixture_root: Path,
    label: str,
) -> Path:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = base / candidate
    _reject_symlink_components(candidate)
    resolved = candidate.resolve()
    if not resolved.is_dir() or not resolved.is_relative_to(fixture_root):
        raise GitSafetyError(f"linked-worktree {label} escapes fixture root: {resolved}")
    return resolved


def _is_bare_repository_root(root: Path) -> bool:
    dotgit = root / ".git"
    return (
        not dotgit.exists()
        and not dotgit.is_symlink()
        and (root / "config").exists()
        and (root / "objects").is_dir()
    )


def _find_repository_root(start: Path, fixture_root: Path | None) -> Path | None:
    current = start
    while True:
        dotgit = current / ".git"
        if dotgit.is_symlink() or dotgit.exists() or _is_bare_repository_root(current):
            return current
        if fixture_root is not None and current == fixture_root:
            return None
        parent = current.parent
        if parent == current:
            return None
        if fixture_root is not None and not parent.is_relative_to(fixture_root):
            return None
        current = parent


def _git_config_files(
    cwd: os.PathLike[str] | str,
    fixture_root: os.PathLike[str] | str | None,
) -> list[Path]:
    start = (
        _require_fixture_cwd(cwd, fixture_root)
        if fixture_root is not None
        else Path(cwd).resolve()
    )
    fixture = (
        _validated_disposable_root(fixture_root)
        if fixture_root is not None
        else None
    )
    root = _find_repository_root(start, fixture)
    if root is None:
        return []
    dotgit = root / ".git"
    if dotgit.is_symlink():
        raise GitSafetyError(f".git must not be a symlink: {dotgit}")
    bare_config = root / "config"
    bare_repository = _is_bare_repository_root(root)
    if bare_repository:
        if fixture is None:
            raise GitSafetyError("bare Git repositories require an explicit disposable fixture root")
        _assert_no_symlink_tree(root)
        if (root / "commondir").exists():
            raise GitSafetyError("bare Git commondir metadata is not supported in Phase 1")
        return [bare_config]
    if dotgit.is_dir():
        if fixture is not None:
            _assert_no_symlink_tree(dotgit)
        candidates = [dotgit / "config", dotgit / "config.worktree"]
    elif dotgit.is_file():
        if fixture is None:
            raise GitSafetyError("linked-worktree Git metadata requires an explicit fixture root")
        contents = dotgit.read_text(encoding="utf-8", errors="strict")
        gitdir_values = [
            line.split(":", 1)[1].strip()
            for line in contents.splitlines()
            if line.lower().startswith("gitdir:")
        ]
        if len(gitdir_values) != 1 or not gitdir_values[0]:
            raise GitSafetyError(f"invalid linked-worktree .git file: {dotgit}")
        gitdir = _resolve_linked_metadata_path(
            gitdir_values[0],
            base=root,
            fixture_root=fixture,
            label="gitdir",
        )
        commondir_file = gitdir / "commondir"
        if commondir_file.exists():
            if commondir_file.is_symlink() or not commondir_file.is_file():
                raise GitSafetyError(f"linked-worktree commondir is unsafe: {commondir_file}")
            common_values = [
                line.strip()
                for line in commondir_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if len(common_values) != 1:
                raise GitSafetyError(f"invalid linked-worktree commondir: {commondir_file}")
            common_dir = _resolve_linked_metadata_path(
                common_values[0],
                base=gitdir,
                fixture_root=fixture,
                label="commondir",
            )
        else:
            try:
                common_dir = gitdir.parents[1].resolve()
            except IndexError as error:
                raise GitSafetyError(f"linked-worktree gitdir has no common directory: {gitdir}") from error
            if not common_dir.is_relative_to(fixture) or not common_dir.is_dir():
                raise GitSafetyError(f"linked-worktree common directory escapes fixture root: {common_dir}")
            _reject_symlink_components(common_dir)
        _assert_no_symlink_tree(gitdir)
        if common_dir != gitdir:
            _assert_no_symlink_tree(common_dir)
        candidates = [common_dir / "config", gitdir / "config.worktree"]
    else:
        return []
    return candidates


def _config_key_is_dangerous(section: str, key: str) -> bool:
    section = section.lower()
    key = key.lower()
    if section in {"alias", "filter", "gpg", "merge", "mergetool", "difftool"}:
        return True
    if section == "include" or section.startswith("includeif"):
        return True
    if section == "credential" and (key == "helper" or key.endswith(".helper")):
        return True
    if section == "commit" and key == "gpgsign":
        return True
    if section == "tag" and key == "gpgsign":
        return True
    if section == "diff" and (
        key == "external" or key.endswith(".textconv") or key.endswith(".command")
    ):
        return True
    if section == "core" and key in {
        "editor",
        "fsmonitor",
        "hookspath",
        "sshcommand",
        "worktree",
    }:
        return True
    return False


def _assert_safe_local_config(
    cwd: os.PathLike[str] | str,
    *,
    fixture_root: os.PathLike[str] | str | None,
) -> None:
    for config in _git_config_files(cwd, fixture_root):
        if not config.exists():
            continue
        if config.is_symlink() or not config.is_file():
            raise GitSafetyError(f"Git config is not a regular file: {config}")
        section = ""
        try:
            lines = config.read_text(encoding="utf-8", errors="strict").splitlines()
        except OSError as error:
            raise GitSafetyError(f"cannot inspect Git config {config}: {error}") from error
        for line_number, raw_line in enumerate(lines, 1):
            line = raw_line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].split(None, 1)[0].strip().lower()
                continue
            if "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if _config_key_is_dangerous(section, key):
                raise GitSafetyError(
                    f"unsafe local Git config {section}.{key} at {config}:{line_number}"
                )


def _safe_child_path(
    raw_path: str,
    cwd: os.PathLike[str] | str,
    fixture_root: os.PathLike[str] | str | None,
) -> Path:
    root = _validated_disposable_root(fixture_root)
    cwd_path = _require_fixture_cwd(cwd, root)
    path = Path(raw_path)
    if not raw_path or "\x00" in raw_path or ".." in path.parts:
        raise GitSafetyError(f"unsafe Git fixture path: {raw_path!r}")
    resolved = path.resolve() if path.is_absolute() else (cwd_path / path).resolve()
    if not resolved.is_relative_to(root):
        raise GitSafetyError(f"Git fixture path escapes fixture root: {raw_path!r}")
    return resolved


def _validate_git_arguments(
    args: Sequence[str],
    cwd: os.PathLike[str] | str,
    fixture_root: os.PathLike[str] | str | None,
) -> None:
    command = _git_subcommand(args)
    values = list(args[1:])
    if (
        (command == "config" and "--get" not in values)
        or command in {"commit", "init"}
        or (command == "symbolic-ref" and tuple(values) == ("HEAD", "refs/heads/main"))
    ):
        _require_fixture_cwd(cwd, fixture_root)
    forbidden_options = {
        "--file",
        "--global",
        "--system",
        "--config",
        "--config-env",
        "--exec-path",
        "--ext-diff",
        "--textconv",
        "--upload-pack",
        "--receive-pack",
    }
    if any(value in forbidden_options or value.split("=", 1)[0] in forbidden_options for value in values):
        raise GitSafetyError(f"unsafe Git option for {command}: {values!r}")
    if command == "init":
        allowed = {"--bare", "--initial-branch=main", "--object-format=sha256"}
        paths = []
        index = 0
        while index < len(values):
            value = values[index]
            if value in allowed:
                index += 1
                continue
            if value == "--object-format":
                if index + 1 >= len(values) or values[index + 1] != "sha256":
                    raise GitSafetyError("only SHA-256 object format is allowed")
                index += 2
                continue
            if value.startswith("-"):
                raise GitSafetyError(f"unsafe git init option: {value}")
            paths.append(value)
            index += 1
        if len(paths) > 1:
            raise GitSafetyError("git init accepts at most one fixture path")
        if paths:
            _safe_child_path(paths[0], cwd, fixture_root)
    elif command == "config":
        if any(value in {"--local", "--get"} for value in values):
            pass
        dangerous = {"--file", "--global", "--system"}
        if any(value in dangerous or value.split("=", 1)[0] in dangerous for value in values):
            raise GitSafetyError("Git config scope/file override is forbidden")
        key_values = [value for value in values if not value.startswith("-")]
        if key_values and "--get" not in values:
            key = key_values[0].split(".", 1)
            if len(key) == 2 and _config_key_is_dangerous(key[0], key[1]):
                raise GitSafetyError(f"unsafe Git config key: {key_values[0]}")
    elif command == "add":
        if not values or any(value.startswith("-") for value in values):
            raise GitSafetyError("git add requires explicit relative fixture paths")
        for value in values:
            _safe_child_path(value, cwd, fixture_root)
    elif command == "commit":
        index = 0
        while index < len(values):
            value = values[index]
            if value in {"--no-gpg-sign", "--allow-empty", "--no-verify"}:
                index += 1
                continue
            if value == "-m":
                if index + 1 >= len(values):
                    raise GitSafetyError("git commit -m requires a message")
                index += 2
                continue
            raise GitSafetyError(f"unsafe git commit option: {value}")
    elif command == "symbolic-ref":
        allowed = {
            ("--quiet", "--short", "HEAD"),
            ("HEAD", "refs/heads/main"),
        }
        if tuple(values) not in allowed:
            raise GitSafetyError(f"unsafe git symbolic-ref arguments: {values!r}")
    elif command == "worktree":
        if values == ["list", "--porcelain"]:
            return
        if len(values) < 5 or values[:2] != ["add", "-b"]:
            raise GitSafetyError("only git worktree list or add -b <branch> <path> <ref> is allowed")
        branch = values[2]
        positional = values[3:-1]
        ref = values[-1]
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or ".." in branch:
            raise GitSafetyError(f"unsafe worktree branch: {branch!r}")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", ref) or ".." in ref:
            raise GitSafetyError(f"unsafe worktree ref: {ref!r}")
        if len(positional) != 1 or positional[0].startswith("-"):
            raise GitSafetyError("worktree add options are not allowed; path must be explicit")
        _safe_child_path(positional[0], cwd, fixture_root)
    elif command == "for-each-ref":
        if values != ["--format=%(refname)\t%(objectname)"]:
            raise GitSafetyError("unsafe for-each-ref format")
    elif command == "rev-parse":
        if tuple(values) not in {
            ("--show-toplevel",),
            ("--verify", "HEAD"),
            ("--show-object-format",),
        }:
            raise GitSafetyError(f"unsafe rev-parse arguments: {values!r}")
    elif command == "status":
        if values != ["--porcelain=v2", "--branch", "--untracked-files=all"] and values != ["--short"]:
            raise GitSafetyError(f"unsafe git status arguments: {values!r}")
    elif command == "diff":
        if values:
            raise GitSafetyError("git diff accepts no arguments in Phase 1")
    elif command == "var":
        if values != ["GIT_EDITOR"]:
            raise GitSafetyError(f"unsafe git var arguments: {values!r}")


class GitObservationError(RuntimeError):
    """Raised when a Git observation is unavailable or failed."""

    def __init__(self, operation: str, result: subprocess.CompletedProcess[str]):
        self.operation = operation
        self.result = result
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        super().__init__(
            f"Git observation failed for {operation}: returncode={result.returncode}: {detail}"
        )


def _require_git_observation(
    operation: str,
    result: subprocess.CompletedProcess[str],
) -> subprocess.CompletedProcess[str]:
    if result.returncode != 0:
        raise GitObservationError(operation, result)
    return result


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# Host-state snapshots must stay bounded on real home trees that routinely
# exceed a million files (IDE caches, language runtimes, package stores).
# Dependency/cache roots are pruned from the walk, regular-file content is
# hashed only below a size cap, and the walk stops at a deterministic entry
# bound with an explicit truncation marker so before/after comparisons remain
# sound and reproducible.
_SNAPSHOT_PRUNE_DIRS = frozenset({
    "node_modules", ".git", "__pycache__", ".bun", ".npm", ".cache",
    ".antigravity", ".antigravity-server", ".pi", ".codex", ".config", ".dropbox",
    ".venv", "venv",
})
_MAX_SNAPSHOT_ENTRIES = 50_000
_MAX_SNAPSHOT_FILE_BYTES = 4 * 1024 * 1024


def _filesystem_entry(path: Path) -> dict[str, Any]:
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    entry: dict[str, Any] = {
        "kind": (
            "directory"
            if stat.S_ISDIR(info.st_mode)
            else "file"
            if stat.S_ISREG(info.st_mode)
            else "symlink"
            if stat.S_ISLNK(info.st_mode)
            else "other"
        ),
        "mode": mode,
        "inode": info.st_ino,
        "device": info.st_dev,
        "size": info.st_size,
    }
    if stat.S_ISLNK(info.st_mode):
        entry["target"] = os.readlink(path)
    elif stat.S_ISREG(info.st_mode):
        if info.st_size <= _MAX_SNAPSHOT_FILE_BYTES:
            try:
                entry["sha256"] = _file_digest(path)
            except (OSError, PermissionError) as error:
                entry["read_error"] = f"{type(error).__name__}: {error}"
        else:
            entry["unhashed"] = True
    return entry


def snapshot_filesystem(path: os.PathLike[str] | str) -> dict[str, dict[str, Any]]:
    """Snapshot entries, permissions, inodes, and regular-file content.

    Symlinks and special files are lstat'ed and never followed.  Returned keys
    are relative to ``path`` and sorted for deterministic equality checks.
    The walk is bounded: cache/dependency directories are pruned, files larger
    than ``_MAX_SNAPSHOT_FILE_BYTES`` are recorded metadata-only, and the walk
    stops at ``_MAX_SNAPSHOT_ENTRIES`` with an explicit ``truncated`` marker on
    the root entry.  Truncation is a deterministic prefix of the same sorted
    traversal, so equal trees still produce equal snapshots.
    """

    root = Path(path)
    if not root.exists() and not root.is_symlink():
        return {".": {"kind": "missing", "mode": None, "inode": None}}
    result: dict[str, dict[str, Any]] = {}
    pending = [root]
    truncated = False
    while pending:
        if len(result) >= _MAX_SNAPSHOT_ENTRIES:
            truncated = True
            break
        current = pending.pop()
        relative = "." if current == root else current.relative_to(root).as_posix()
        try:
            result[relative] = _filesystem_entry(current)
        except OSError as error:
            result[relative] = {
                "kind": "unreadable",
                "mode": None,
                "inode": None,
                "error": f"{type(error).__name__}: {error}",
            }
            continue
        try:
            if result[relative]["kind"] == "directory":
                children = [
                    Path(entry.path)
                    for entry in os.scandir(current)
                    if not (entry.is_dir(follow_symlinks=False) and entry.name in _SNAPSHOT_PRUNE_DIRS)
                ]
                pending.extend(sorted(children, key=lambda item: item.name, reverse=True))
        except OSError as error:
            result[relative]["scan_error"] = f"{type(error).__name__}: {error}"
    if truncated:
        result["."]["truncated"] = True
    return {key: result[key] for key in sorted(result)}


filesystem_snapshot = snapshot_filesystem


def assert_snapshot_unchanged(
    path: os.PathLike[str] | str,
    before: Mapping[str, Mapping[str, Any]],
    *,
    label: str = "filesystem",
) -> None:
    after = snapshot_filesystem(path)
    if dict(before) != after:
        before_keys = set(before)
        after_keys = set(after)
        added = sorted(after_keys - before_keys)
        removed = sorted(before_keys - after_keys)
        changed = sorted(
            key for key in before_keys & after_keys if dict(before[key]) != after[key]
        )
        raise AssertionError(
            f"{label} changed: added={added[:8]!r}, removed={removed[:8]!r}, changed={changed[:8]!r}"
        )


def _sqlite_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__bytes__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, memoryview):
        return {"__bytes__": base64.b64encode(value.tobytes()).decode("ascii")}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _quote_sqlite_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def snapshot_sqlite_rows(
    connection_or_path: sqlite3.Connection | os.PathLike[str] | str,
    *,
    tables: Iterable[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Return deterministic row snapshots without mutating a database."""

    owned_connection = not isinstance(connection_or_path, sqlite3.Connection)
    connection: sqlite3.Connection
    if owned_connection:
        path = Path(connection_or_path)
        if not path.exists():
            return {}
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    else:
        connection = connection_or_path
    try:
        if tables is None:
            rows = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            table_names = [str(row[0]) for row in rows]
        else:
            table_names = sorted({str(table) for table in tables})
        snapshot: dict[str, list[dict[str, Any]]] = {}
        for table in table_names:
            quoted = _quote_sqlite_identifier(table)
            columns = [
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({quoted})").fetchall()
            ]
            if not columns:
                snapshot[table] = []
                continue
            rows = connection.execute(f"SELECT * FROM {quoted}").fetchall()
            values = [
                {column: _sqlite_value(value) for column, value in zip(columns, row)}
                for row in rows
            ]
            values.sort(key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")))
            snapshot[table] = values
        return snapshot
    finally:
        if owned_connection:
            connection.close()


sqlite_rows_snapshot = snapshot_sqlite_rows


def _git_result(
    args: Sequence[str],
    repository: os.PathLike[str] | str,
    environ: Mapping[str, Any] | None,
) -> subprocess.CompletedProcess[str]:
    return git_run(args, cwd=repository, environ=environ, check=False)


def snapshot_git_refs(
    repository: os.PathLike[str] | str,
    *,
    environ: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    result = _require_git_observation(
        "refs",
        _git_result(
            ["for-each-ref", "--format=%(refname)\t%(objectname)"], repository, environ
        ),
    )
    refs: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "\t" in line:
            name, oid = line.split("\t", 1)
            refs[name] = oid
    return {name: refs[name] for name in sorted(refs)}


def snapshot_git_worktrees(
    repository: os.PathLike[str] | str,
    *,
    environ: Mapping[str, Any] | None = None,
) -> list[dict[str, str | bool]]:
    result = _require_git_observation(
        "worktrees",
        _git_result(["worktree", "list", "--porcelain"], repository, environ),
    )
    worktrees: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] = {}
    for raw_line in result.stdout.splitlines() + [""]:
        line = raw_line.strip()
        if not line:
            if current:
                worktrees.append(dict(current))
                current = {}
            continue
        if line == "bare":
            current["bare"] = True
        elif line == "detached":
            current["detached"] = True
        elif line == "locked":
            current["locked"] = True
        elif line.startswith("prunable "):
            current["prunable"] = line.removeprefix("prunable ")
        elif " " in line:
            key, value = line.split(" ", 1)
            current[key] = value
    worktrees.sort(key=lambda item: str(item.get("worktree", "")))
    return worktrees


def snapshot_git_status(
    repository: os.PathLike[str] | str,
    *,
    environ: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = _require_git_observation(
        "status",
        _git_result(
            ["status", "--porcelain=v2", "--branch", "--untracked-files=all"],
            repository,
            environ,
        ),
    )
    return {
        "returncode": result.returncode,
        "lines": tuple(result.stdout.splitlines()),
        "stderr": result.stderr.strip(),
    }


def snapshot_git_repository(
    repository: os.PathLike[str] | str,
    *,
    environ: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Snapshot refs/OIDs, HEAD, linked worktrees, and status."""

    head_result = _git_result(["symbolic-ref", "--quiet", "--short", "HEAD"], repository, environ)
    oid_result = _git_result(["rev-parse", "--verify", "HEAD"], repository, environ)
    format_result = _require_git_observation(
        "object-format",
        _git_result(["rev-parse", "--show-object-format"], repository, environ),
    )
    return {
        "object_format": format_result.stdout.strip(),
        "head_ref": head_result.stdout.strip() if head_result.returncode == 0 else None,
        "head_oid": oid_result.stdout.strip() if oid_result.returncode == 0 else None,
        "refs": snapshot_git_refs(repository, environ=environ),
        "worktrees": snapshot_git_worktrees(repository, environ=environ),
        "status": snapshot_git_status(repository, environ=environ),
    }


git_snapshot = snapshot_git_repository
snapshot_git = snapshot_git_repository


def snapshot_observations(observable: Any) -> Any:
    """Copy a fake adapter/event stream without sharing mutable state."""

    if hasattr(observable, "snapshot"):
        value = observable.snapshot()
    elif hasattr(observable, "observations"):
        value = observable.observations
    else:
        value = observable
    return copy.deepcopy(value)


def snapshot_events(events: Any) -> Any:
    return snapshot_observations(events)


@dataclass(frozen=True)
class ChildProcessResult:
    """Bounded result from a helper-owned child process."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def exited_with_failpoint(self) -> bool:
        return self.returncode != 0


@dataclass(frozen=True)
class CrashRecoveryResult:
    terminated: ChildProcessResult
    recovered: ChildProcessResult


def _import_target(target: str) -> Callable[..., Any]:
    if ":" not in target:
        raise ValueError("child target must use module:callable syntax")
    module_name, attribute_path = target.split(":", 1)
    module = importlib.import_module(module_name)
    value: Any = module
    for component in attribute_path.split("."):
        value = getattr(value, component)
    if not callable(value):
        raise TypeError(f"child target is not callable: {target}")
    return value


def _call_child_target(target: Callable[..., Any], controller: FailpointController, payload: Any) -> Any:
    try:
        signature = inspect.signature(target)
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
        accepts_varargs = any(
            parameter.kind == parameter.VAR_POSITIONAL
            for parameter in signature.parameters.values()
        )
    except (TypeError, ValueError):
        positional = []
        accepts_varargs = True
    if accepts_varargs or len(positional) >= 2:
        return target(controller, payload)
    return target(controller)


def _child_runner_main(argv: Sequence[str]) -> int:
    if len(argv) != 5:
        print("usage: --run-child module:callable failpoint-name exit-code payload-json", file=sys.stderr)
        return 2
    _, target_spec, selected_name, raw_exit_code, raw_payload = argv
    _register_child_fixture_root_from_environment()
    target = _import_target(target_spec)
    payload = json.loads(raw_payload)
    if selected_name == "__no_failpoint__":
        controller: FailpointController = NoOpFailpointController()
    else:
        controller = ConfiguredFailpointController(
            selected_name,
            exit_code=int(raw_exit_code),
        )
    result = _call_child_target(target, controller, payload)
    # Keep the direct runner alive briefly so the parent can observe and
    # terminate descendants even if a target calls setsid before returning.
    time.sleep(0.1)
    if result is not None:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def _target_spec(target: str | Callable[..., Any]) -> str:
    if isinstance(target, str):
        module_name, separator, attribute_path = target.partition(":")
        if not separator or not module_name or not attribute_path:
            raise ValueError("child target must use module:callable syntax")
        if any(not component for component in attribute_path.split(".")):
            raise ValueError("child target attribute path is empty")
        # Do not import a string target in the parent.  Import and side effects
        # belong exclusively to the helper-owned child process.
        return target
    module = getattr(target, "__module__", None)
    qualname = getattr(target, "__qualname__", None)
    if not module or not qualname or "<locals>" in qualname:
        raise ValueError("child target callable must be importable at module scope")
    return f"{module}:{qualname}"


def _child_payload_json(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_CHILD_PAYLOAD:
        raise ValueError(f"child payload exceeds {_MAX_CHILD_PAYLOAD}-byte bound")
    return encoded.decode("utf-8")


@contextlib.contextmanager
def _child_sandbox_environment(environ: Mapping[str, Any] | None):
    """Give every helper child a disposable HOME/XDG/TMP boundary."""

    with safe_temporary_directory(prefix="pi-control-child-sandbox-") as raw_root:
        root = Path(raw_root)
        home = root / "home"
        state = root / "state"
        runtime = root / "runtime"
        temp = root / "tmp"
        for path in (home, state, runtime, temp):
            path.mkdir(parents=True, exist_ok=True)
            try:
                path.chmod(0o700)
            except OSError:
                pass
        child_environment = sanitize_git_environment(environ)
        for key in _CHILD_FIXTURE_ROOT_KEYS:
            child_environment.pop(key, None)
        child_environment.update(
            {
                "HOME": str(home),
                "XDG_STATE_HOME": str(state),
                "XDG_RUNTIME_DIR": str(runtime),
                "TMPDIR": str(temp),
                "TMP": str(temp),
                "TEMP": str(temp),
            }
        )
        yield child_environment, root


def _child_result(returncode: int, stdout: bytes, stderr: bytes) -> ChildProcessResult:
    return ChildProcessResult(
        returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _run_child_target(
    spec: str,
    selected_name: str,
    exit_code: int,
    payload: Any,
    *,
    cwd: os.PathLike[str] | str | None,
    fixture_root: os.PathLike[str] | str | None,
    environ: Mapping[str, Any] | None,
    timeout: float,
) -> ChildProcessResult:
    source_root = Path(__file__).resolve().parents[2]
    module_root = Path(__file__).resolve().parent
    authorized_root = (
        _validated_disposable_root(fixture_root)
        if fixture_root is not None
        else None
    )
    with _child_sandbox_environment(environ) as (child_environment, sandbox_root):
        existing_pythonpath = child_environment.get("PYTHONPATH")
        # Include the package parent so child targets importable under either
        # the tests.control_plane.* or the control_plane.* spelling resolve.
        child_environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(source_root), str(module_root.parent), str(module_root), existing_pythonpath) if part
        )
        if authorized_root is not None:
            metadata = authorized_root.stat()
            child_environment.update(
                {
                    "PI_CONTROL_FIXTURE_ROOT": str(authorized_root),
                    "PI_CONTROL_FIXTURE_ROOT_DEV": str(metadata.st_dev),
                    "PI_CONTROL_FIXTURE_ROOT_INO": str(metadata.st_ino),
                }
            )
        command = [
            sys.executable,
            "-m",
            "tests.control_plane.helpers",
            "--run-child",
            spec,
            selected_name,
            str(exit_code),
            _child_payload_json(payload),
        ]
        if authorized_root is None:
            if cwd is not None:
                raise GitSafetyError(
                    "explicit child cwd requires an explicit disposable fixture_root"
                )
            child_cwd = sandbox_root
        else:
            child_cwd = (
                _require_fixture_cwd(cwd, authorized_root)
                if cwd is not None
                else authorized_root
            )
        returncode, stdout, stderr = _bounded_process(
            command,
            cwd=str(child_cwd),
            environ=child_environment,
            timeout=timeout,
        )
        return _child_result(returncode, stdout, stderr)


def run_fresh_process(
    target: str | Callable[..., Any],
    *,
    payload: Any = None,
    cwd: os.PathLike[str] | str | None = None,
    fixture_root: os.PathLike[str] | str | None = None,
    environ: Mapping[str, Any] | None = None,
    timeout: float = 15.0,
) -> ChildProcessResult:
    """Invoke an importable observation/recovery function in a fresh process."""

    return _run_child_target(
        _target_spec(target),
        "__no_failpoint__",
        0,
        payload,
        cwd=cwd,
        fixture_root=fixture_root,
        environ=environ,
        timeout=timeout,
    )


def run_failpoint_child(
    target: str | Callable[..., Any],
    selected_name: str,
    *,
    exit_code: int = 97,
    payload: Any = None,
    cwd: os.PathLike[str] | str | None = None,
    fixture_root: os.PathLike[str] | str | None = None,
    environ: Mapping[str, Any] | None = None,
    timeout: float = 15.0,
) -> ChildProcessResult:
    """Run an operation with an injected child-only ``os._exit`` failpoint."""

    _validate_failpoint_name(selected_name)
    if not 1 <= exit_code <= 255:
        raise ValueError("exit_code must be in the range 1..255")
    return _run_child_target(
        _target_spec(target),
        selected_name,
        exit_code,
        payload,
        cwd=cwd,
        fixture_root=fixture_root,
        environ=environ,
        timeout=timeout,
    )


def run_failpoint_then_recover(
    target: str | Callable[..., Any],
    recovery_target: str | Callable[..., Any],
    selected_name: str,
    *,
    exit_code: int = 97,
    payload: Any = None,
    recovery_payload: Any = None,
    cwd: os.PathLike[str] | str | None = None,
    fixture_root: os.PathLike[str] | str | None = None,
    environ: Mapping[str, Any] | None = None,
    timeout: float = 15.0,
) -> CrashRecoveryResult:
    """Terminate at a selected boundary, then observe/reconcile fresh state."""

    terminated = run_failpoint_child(
        target,
        selected_name,
        exit_code=exit_code,
        payload=payload,
        cwd=cwd,
        fixture_root=fixture_root,
        environ=environ,
        timeout=timeout,
    )
    recovered = run_fresh_process(
        recovery_target,
        payload=recovery_payload,
        cwd=cwd,
        fixture_root=fixture_root,
        environ=environ,
        timeout=timeout,
    )
    return CrashRecoveryResult(terminated=terminated, recovered=recovered)


spawn_failpoint_child = run_failpoint_child
fresh_process_observation = run_fresh_process


def _discover_repository(path: Path) -> Path:
    result = git_run(
        ["rev-parse", "--show-toplevel"],
        cwd=path,
        environ=sanitize_git_environment(),
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    return path.resolve()


def _init_git(
    path: Path,
    *,
    bare: bool = False,
    object_format: str | None = None,
    fixture_root: Path,
) -> None:
    args = ["init"]
    if bare:
        args.append("--bare")
    args.extend(["--initial-branch=main"])
    if object_format is not None:
        args.extend(["--object-format", object_format])
    args.append(str(path))
    result = git_run(args, cwd=path.parent, check=False, fixture_root=fixture_root)
    if result.returncode != 0 and "--initial-branch" in args:
        # Git versions before 2.28 do not understand --initial-branch.
        args.remove("--initial-branch=main")
        result = git_run(args, cwd=path.parent, check=False, fixture_root=fixture_root)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr
        )
    if not bare and object_format is None:
        git_run(
            ["symbolic-ref", "HEAD", "refs/heads/main"],
            cwd=path,
            check=True,
            fixture_root=fixture_root,
        )


def supports_git_sha256() -> bool:
    """Capability probe that creates no repository outside a temp directory."""

    with safe_temporary_directory(prefix="pi-control-sha256-probe-") as raw_root:
        root = Path(raw_root)
        result = git_run(
            ["init", "--object-format=sha256", str(root / "repo")],
            cwd=root,
            check=False,
            fixture_root=root,
        )
        return result.returncode == 0


class OptionalGitCapabilityUnavailable(RuntimeError):
    """Raised only when a caller explicitly requests unsupported SHA-256 Git."""


class DisposableEnvironment:
    """A complete disposable HOME/XDG/Git/session/adapters fixture.

    Construction takes a baseline snapshot of the caller's repository and
    real HOME only when ``capture_host_state=True`` is explicitly requested
    (the snapshot exists solely for :meth:`assert_untouched`).  On hosts with
    very large home trees the default must stay opt-in so ordinary fixtures
    never pay an O(home-size) content walk at construction.
    """

    def __init__(
        self,
        *,
        repository_under_test: os.PathLike[str] | str | None = None,
        real_home: os.PathLike[str] | str | None = None,
        include_sha256: bool = False,
        capture_host_state: bool = False,
    ) -> None:
        self._temporary = safe_temporary_directory(prefix="pi-control-plane-")
        self._closed = False
        self.root = Path(self._temporary.name)
        self.home = self.root / "home"
        self.state_home = self.root / "state"
        self.runtime_dir = self.root / "runtime"
        self.temp_dir = self.root / "tmp"
        self.worktree_root = self.root / "worktrees"
        self.repo = self.root / "repository"
        self.primary_checkout = self.repo
        self.primary_worktree = self.repo
        self.remote = self.root / "remote.git"
        self.bare_remote = self.remote
        self.linked_worktree = self.worktree_root / "linked"
        self.pi_home = self.home / ".pi"
        self.session_dir = self.pi_home / "agent" / "sessions"
        self.session_id = "fixture-session-0001"
        self.session_path = self.session_dir / f"{self.session_id}.jsonl"
        self.sha256_repo: Path | None = None
        self.include_sha256 = bool(include_sha256)
        self.sha256_supported = supports_git_sha256()
        self.environment = sanitize_git_environment()
        self.environment.update(
            {
                "HOME": str(self.home),
                "XDG_STATE_HOME": str(self.state_home),
                "XDG_RUNTIME_DIR": str(self.runtime_dir),
                "TMPDIR": str(self.temp_dir),
                "TMP": str(self.temp_dir),
                "TEMP": str(self.temp_dir),
                "PI_CONTROL_FIXTURE": "1",
            }
        )
        self.git_environment = self.environment
        self.repository_under_test = _discover_repository(
            Path(repository_under_test) if repository_under_test is not None else Path.cwd()
        )
        self.real_home = Path(
            real_home
            if real_home is not None
            else os.environ.get("HOME", str(Path.home()))
        ).expanduser().resolve()
        self._capture_host_state = capture_host_state
        self._host_repository_filesystem = (
            snapshot_filesystem(self.repository_under_test) if capture_host_state else None
        )
        self._host_home_filesystem = (
            snapshot_filesystem(self.real_home) if capture_host_state else None
        )
        self._host_repository_git = (
            snapshot_git_repository(self.repository_under_test)
            if capture_host_state
            else None
        )
        try:
            self._create_fixture()
        except BaseException:
            self.close()
            raise

    def _create_fixture(self) -> None:
        for path in (
            self.home,
            self.state_home,
            self.temp_dir,
            self.worktree_root,
            self.session_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
            try:
                path.chmod(0o700)
            except OSError:
                pass
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.runtime_dir.chmod(0o700)
        except OSError:
            pass
        _init_git(self.repo, fixture_root=self.root)
        git_run(
            ["config", "--local", "user.name", "Pi Control Fixture"],
            cwd=self.repo,
            fixture_root=self.root,
        )
        git_run(
            ["config", "--local", "user.email", "fixture@example.invalid"],
            cwd=self.repo,
            fixture_root=self.root,
        )
        (self.repo / "README.md").write_text("disposable control-plane fixture\n", encoding="utf-8")
        (self.repo / "fixture.txt").write_text("initial\n", encoding="utf-8")
        commit_environment = dict(self.environment)
        commit_environment.update(
            {
                "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z",
                "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z",
            }
        )
        git_run(
            ["add", "README.md", "fixture.txt"],
            cwd=self.repo,
            environ=commit_environment,
            fixture_root=self.root,
        )
        git_run(
            ["commit", "--no-gpg-sign", "-m", "fixture initial commit"],
            cwd=self.repo,
            environ=commit_environment,
            fixture_root=self.root,
        )

        # Keep a bare Git fixture for object/common-directory scenarios, but do
        # not add a remote URL or perform fetch/push/network operations.
        _init_git(self.remote, bare=True, fixture_root=self.root)

        git_run(
            ["worktree", "add", "-b", "fixture-linked", str(self.linked_worktree), "main"],
            cwd=self.repo,
            fixture_root=self.root,
        )
        self._write_session()

        try:
            from .fake_adapters import (
                FakeEventEmitter,
                FakePresentationAdapter,
                FakeProcessAdapter,
                FakeRuntimeAdapter,
            )
        except ImportError:  # Supports unittest discovery with this directory as top level.
            from fake_adapters import (
                FakeEventEmitter,
                FakePresentationAdapter,
                FakeProcessAdapter,
                FakeRuntimeAdapter,
            )

        self.process_adapter = FakeProcessAdapter()
        self.runtime_adapter = FakeRuntimeAdapter()
        self.presentation_adapter = FakePresentationAdapter()
        self.event_emitter = FakeEventEmitter()
        self.fake_process = self.process_adapter
        self.fake_runtime = self.runtime_adapter
        self.fake_presentation = self.presentation_adapter
        if self.include_sha256 and self.sha256_supported:
            sha_root = self.root / "sha256-repository"
            _init_git(sha_root, object_format="sha256", fixture_root=self.root)
            git_run(
                ["config", "--local", "user.name", "Pi Control Fixture"],
                cwd=sha_root,
                fixture_root=self.root,
            )
            git_run(
                ["config", "--local", "user.email", "fixture@example.invalid"],
                cwd=sha_root,
                fixture_root=self.root,
            )
            (sha_root / "fixture.txt").write_text("sha256\n", encoding="utf-8")
            git_run(["add", "fixture.txt"], cwd=sha_root, fixture_root=self.root)
            git_run(
                ["commit", "--no-gpg-sign", "-m", "sha256 fixture"],
                cwd=sha_root,
                fixture_root=self.root,
            )
            self.sha256_repo = sha_root
        elif self.include_sha256:
            raise OptionalGitCapabilityUnavailable("installed Git lacks SHA-256 object format")

    @property
    def _requested_sha256(self) -> bool:
        # Kept as a property so construction remains backwards compatible with
        # callers that only inspect ``sha256_repo``.
        return bool(getattr(self, "include_sha256", False))

    def _write_session(self) -> None:
        records = (
            {
                "type": "session",
                "id": self.session_id,
                "version": 1,
                "cwd": str(self.primary_checkout),
                "timestamp": "2024-01-01T00:00:00.000000Z",
            },
            {
                "type": "message",
                "id": "fixture-entry-0001",
                "role": "user",
                "text": "disposable fixture entry",
            },
            {
                "type": "message",
                "id": "fixture-entry-0002",
                "role": "assistant",
                "text": "disposable fixture response",
            },
        )
        self.session_path.write_text(
            "".join(
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        try:
            self.session_path.chmod(0o600)
        except OSError:
            pass
        self.session_records = records
        self.session_jsonl = self.session_path

    def subprocess_environment(self, **overrides: Any) -> dict[str, str]:
        environment = dict(self.environment)
        environment.update({key: str(value) for key, value in overrides.items()})
        return sanitize_git_environment(environment)

    def assert_untouched(self) -> None:
        if not self._capture_host_state:
            raise AssertionError("host-state snapshots were disabled for this fixture")
        assert self._host_repository_filesystem is not None
        assert self._host_home_filesystem is not None
        assert self._host_repository_git is not None
        assert_snapshot_unchanged(
            self.repository_under_test,
            self._host_repository_filesystem,
            label="repository under test",
        )
        assert_snapshot_unchanged(self.real_home, self._host_home_filesystem, label="real HOME")
        current_git = snapshot_git_repository(self.repository_under_test)
        if current_git != self._host_repository_git:
            raise AssertionError("repository Git refs/worktrees/status changed")

    def assert_isolated(self) -> None:
        for fixture_path in (self.home, self.state_home, self.runtime_dir, self.repo, self.remote):
            if fixture_path.resolve() == self.real_home:
                raise AssertionError(f"fixture path aliases real HOME: {fixture_path}")
            if fixture_path.resolve() == self.repository_under_test:
                raise AssertionError(f"fixture path aliases repository under test: {fixture_path}")
        if self.home.resolve().is_relative_to(self.real_home):
            raise AssertionError("fixture HOME is nested under real HOME")
        if self.repo.resolve().is_relative_to(self.repository_under_test):
            raise AssertionError("fixture repository is nested under repository under test")

    assert_fixture_and_host_untouched = assert_untouched
    assert_real_home_and_repo_unchanged = assert_untouched

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._temporary.cleanup()

    def __enter__(self) -> "DisposableEnvironment":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        self.close()
        return False

    def __del__(self) -> None:  # pragma: no cover - best-effort fallback
        try:
            self.close()
        except Exception:
            pass


DisposableFixture = DisposableEnvironment
ControlPlaneFixture = DisposableEnvironment


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--run-child":
        raise SystemExit(_child_runner_main(sys.argv[1:]))
    raise SystemExit("helpers is a library; use --run-child only through the child helpers")
