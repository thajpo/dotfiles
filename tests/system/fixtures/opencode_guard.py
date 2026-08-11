#!/usr/bin/env python3
"""P11 OpenCode protection guard: snapshot ~/.config/opencode and ~/.opencode
before and after a journey and verify they are unchanged and launchable."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

PRUNE = {"node_modules", ".cache", "__pycache__", ".git", ".npm", ".bun"}


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def snapshot(root: Path) -> dict[str, dict[str, object]]:
    entries: dict[str, dict[str, object]] = {}
    if not root.is_dir():
        return entries
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(item for item in directories if item not in PRUNE)
        relative = Path(current).relative_to(root)
        for name in sorted(files):
            path = Path(current) / name
            if path.is_symlink():
                continue
            key = str(relative / name)
            info = path.lstat()
            entries[key] = {"size": info.st_size, "sha256": _digest(path)}
    return entries


def launchable() -> bool:
    executable = os.environ.get("OPENCODE_BIN") or "opencode"
    try:
        result = subprocess.run([executable, "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def opencode_roots() -> list[Path]:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return [config_home / "opencode", Path("~/.opencode").expanduser()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opencode-guard")
    parser.add_argument("--before")
    parser.add_argument("--after")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    roots = opencode_roots()
    current = {str(root): snapshot(root) for root in roots}
    result: dict[str, object] = {"opencodeConfigUnchanged": True, "opencodeLaunchable": launchable()}

    if args.before and Path(args.before).is_file():
        prior = json.loads(Path(args.before).read_text(encoding="utf-8"))
        if prior != current:
            result["opencodeConfigUnchanged"] = False

    if args.before and not Path(args.before).is_file():
        Path(args.before).write_text(json.dumps(current, sort_keys=True), encoding="utf-8")

    if args.after:
        Path(args.after).write_text(json.dumps(current, sort_keys=True), encoding="utf-8")

    if args.output:
        Path(args.output).write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
        print(json.dumps(result, sort_keys=True))
    if result["opencodeConfigUnchanged"] is not True:
        print("FAIL: OpenCode configuration changed during the journey", file=sys.stderr)
        return 1
    if result["opencodeLaunchable"] is not True:
        print("FAIL: OpenCode is not launchable", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
