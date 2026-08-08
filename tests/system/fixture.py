"""Disposable system fixture with host-state and namespace escape guards."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

try:
    from tests.control_plane.helpers import snapshot_filesystem, snapshot_git_repository
except ImportError:
    snapshot_filesystem = None
    snapshot_git_repository = None


@dataclass
class SystemFixture:
    temporary: tempfile.TemporaryDirectory[str]
    root: Path
    home: Path
    xdg: Path
    repository: Path
    state: Path
    evidence: Path
    _host_snapshot: dict[str, Any]
    _fixture_snapshot: dict[str, Any]

    @classmethod
    def create(cls) -> "SystemFixture":
        temporary = tempfile.TemporaryDirectory(prefix="pi-system-")
        root = Path(temporary.name)
        home, xdg, repository, state, evidence = (root / name for name in ("home", "xdg", "repository", "state", "evidence"))
        for path in (home, xdg, repository, state, evidence):
            path.mkdir(mode=0o700)
        fixture = cls(temporary, root, home, xdg, repository, state, evidence, cls._snapshot_host(), {})
        fixture._initialize_repository()
        fixture._fixture_snapshot = fixture.snapshot_namespace()
        return fixture

    def _initialize_repository(self) -> None:
        environment = self.environment()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repository)], check=True, env=environment, capture_output=True)
        for key, value in (("user.name", "Pi System Fixture"), ("user.email", "fixture@example.invalid")):
            subprocess.run(["git", "-C", str(self.repository), "config", key, value], check=True, env=environment, capture_output=True)
        (self.repository / "fixture.txt").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repository), "add", "fixture.txt"], check=True, env=environment, capture_output=True)
        subprocess.run(["git", "-C", str(self.repository), "commit", "-q", "-m", "fixture"], check=True, env={**environment, "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z"}, capture_output=True)

    @staticmethod
    def _snapshot_host() -> dict[str, Any]:
        repository = Path.cwd().resolve()
        home = Path.home().resolve()
        value: dict[str, Any] = {"cwd": str(repository), "home": str(home), "xdg": os.environ.get("XDG_STATE_HOME"), "pid": os.getpid()}
        if snapshot_filesystem is not None:
            value["repositoryFilesystem"] = snapshot_filesystem(repository)
            value["homeFilesystem"] = snapshot_filesystem(home)
        if snapshot_git_repository is not None:
            value["repositoryGit"] = snapshot_git_repository(repository)
        return value

    def environment(self) -> dict[str, str]:
        value = {key: item for key, item in os.environ.items() if not key.startswith("GIT_")}
        value.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.xdg / "config"), "XDG_STATE_HOME": str(self.state), "XDG_RUNTIME_DIR": str(self.root / "runtime"), "TMPDIR": str(self.root / "tmp"), "TMP": str(self.root / "tmp"), "TEMP": str(self.root / "tmp"), "PI_CODING_AGENT_DIR": str(self.home / ".pi" / "agent"), "PI_SYSTEM_FIXTURE": "1"})
        for path in (Path(value["XDG_CONFIG_HOME"]), Path(value["XDG_RUNTIME_DIR"]), Path(value["TMPDIR"]), Path(value["PI_CODING_AGENT_DIR"])):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return value

    def snapshot_namespace(self) -> dict[str, Any]:
        value: dict[str, Any] = {"filesystem": snapshot_filesystem(self.root) if snapshot_filesystem is not None else {}}
        if snapshot_git_repository is not None:
            value["git"] = snapshot_git_repository(self.repository)
        return value

    @staticmethod
    def digest_snapshot(value: dict[str, Any]) -> str:
        body = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return "sha256:" + hashlib.sha256(body).hexdigest()

    def assert_namespace_unchanged(self, before: dict[str, Any]) -> None:
        after = self.snapshot_namespace()
        if before != after:
            raise AssertionError("disposable system fixture changed during read-only process scenario")

    def assert_host_unchanged(self) -> None:
        current = self._snapshot_host()
        if current != self._host_snapshot:
            raise AssertionError("system fixture escaped into host state")

    def close(self) -> None:
        self.assert_host_unchanged()
        self.temporary.cleanup()

    def __enter__(self) -> "SystemFixture": return self
    def __exit__(self, *_: Any) -> None: self.close()


__all__ = ["SystemFixture"]
