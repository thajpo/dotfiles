"""Owner-only filesystem primitives shared by Pisec adapters."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import stat
from typing import Any

from .models import NeedsAttentionError, PisecError


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(path.name + f".tmp-{secrets.token_hex(8)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _secure_tree(root: Path, child: Path) -> None:
    root = root.absolute()
    if root.is_symlink():
        raise PisecError("Pisec state root is a symlink")
    if not root.exists():
        root.mkdir(parents=True, mode=0o700)
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.geteuid():
        raise PisecError("Pisec state root is not an owner directory")
    if stat.S_IMODE(root_info.st_mode) != 0o700:
        os.chmod(root, 0o700)
    child = child.absolute()
    try:
        parts = child.relative_to(root).parts
    except ValueError as error:
        raise PisecError("managed runtime path escapes the Pisec state root") from error
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise PisecError("managed runtime directory contains a symlink")
        if current.exists():
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise PisecError("managed runtime directory is not owner-only")
        else:
            current.mkdir(mode=0o700)


def _secure_secret(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise PisecError("gateway token file must be an owner-only regular file")
    token = path.read_text().strip()
    if len(token) < 32 or len(token) > 512 or "\x00" in token:
        raise PisecError("gateway token is invalid")
    return token


def _read_runtime_secret(path: Path) -> str:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise NeedsAttentionError("runtime launch secret is missing") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise NeedsAttentionError("runtime launch secret is unsafe")
    token = path.read_text().strip()
    if len(token) < 32 or len(token) > 512 or "\x00" in token:
        raise NeedsAttentionError("runtime launch secret is invalid")
    return token
