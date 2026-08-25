"""Sanitized local Git execution for Pisec-managed repositories."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Iterable, Mapping

from .models import InvalidRequestError


GIT_CONFIG_ARGS = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "commit.gpgSign=false",
    "-c", "tag.gpgSign=false",
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
)


def sanitized_git_environment(*, role: str | None = None) -> dict[str, str]:
    """Return the complete inherited environment for non-authenticated Git."""
    environment = {
        "HOME": "/nonexistent",
        "PATH": os.defpath,
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TEMPLATE_DIR": "/nonexistent",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_PAGER": "cat",
    }
    if role is not None:
        identities = {
            "worker": ("Pisec Worker", "pisec-worker@invalid"),
            "secretary": ("Pisec Secretary", "pisec-secretary@invalid"),
        }
        try:
            name, email = identities[role]
        except KeyError as error:
            raise InvalidRequestError("Git role is invalid") from error
        environment.update({
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        })
    return environment


def _git_executable() -> str:
    executable = shutil.which("git", path=os.defpath)
    if executable is None:
        raise InvalidRequestError("git is unavailable")
    return executable


def run_git(
    repository: Path | str | None,
    args: Iterable[str],
    *,
    accepted: Iterable[int] = (0,),
    input_text: str | None = None,
    timeout: float = 30.0,
    role: str | None = None,
    max_bytes: int = 256 * 1024,
) -> subprocess.CompletedProcess[str]:
    """Run local Git with no user/system configuration or transport secrets."""
    command = [_git_executable(), *GIT_CONFIG_ARGS]
    if repository is not None:
        command.extend(("-C", str(Path(repository))))
    command.extend(str(argument) for argument in args)
    previous_umask = os.umask(0o077)
    try:
        result = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            env=sanitized_git_environment(role=role),
            timeout=timeout,
            check=False,
        )
    finally:
        os.umask(previous_umask)
    accepted_codes = frozenset(int(code) for code in accepted)
    if result.returncode not in accepted_codes:
        raise InvalidRequestError(
            "Git operation failed",
            detail={
                "command": command[1:],
                "exitCode": result.returncode,
                "stderr": result.stderr[:1024],
            },
        )
    if len(result.stdout.encode("utf-8")) > max_bytes or len(result.stderr.encode("utf-8")) > max_bytes:
        raise InvalidRequestError("Git operation output was too large")
    return result


def git_text(repository: Path | str | None, *args: str, **kwargs: object) -> str:
    return run_git(repository, args, **kwargs).stdout.strip()
