"""Strict fake process/runtime executable used only by system fixtures."""

from __future__ import annotations

import json
import os
import sys


def _valid_args(name: str, args: list[str]) -> bool:
    if any("\x00" in value for value in args):
        return False
    if name == "pi":
        return args in (["--version"], ["--no-session"], ["--continue"], ["--resume"]) or (
            len(args) == 2 and args[0] in {"--session", "--session-dir"} and args[1].startswith("/")
        )
    if name == "docker":
        return args in (["info"], ["ps"], ["inspect"])
    if name == "tmux":
        return args in (["list-panes"], ["list-sessions"], ["has-session"])
    if name == "herdr":
        return args == ["status"]
    return False


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    name = os.environ.get("PI_FAKE_EXECUTABLE", "pi")
    if not _valid_args(name, args):
        print(json.dumps({"error": "unknown fake command", "argv": args}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps({"fake": name, "argv": args, "network": False}, sort_keys=True))
    return 0


if __name__ == "__main__": raise SystemExit(main())
