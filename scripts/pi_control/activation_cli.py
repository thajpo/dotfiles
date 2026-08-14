"""Controlling-TTY authority for one exact Pi generation activation and cutover."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, TextIO

from .errors import ControlPlaneError
from .pi_install import InstallError, activate, ensure_fresh_state, verify_stage
from .staged_build import load_build_manifest


class ActivationCliError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pi-activate")
    parser.add_argument("--staged-root", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--reset-state", action="store_true")
    parser.add_argument("--test-only-decision", choices=("approve", "reject"))
    parser.add_argument("--test-only-defer-execution", action="store_true")
    return parser


def _test_fixture(root: Path) -> bool:
    marker = root / ".pi-activate-test-fixture"
    try:
        info = marker.lstat()
        body = marker.read_text(encoding="ascii")
    except OSError:
        return False
    return root.resolve().is_relative_to(Path("/tmp").resolve()) and not stat.S_ISLNK(info.st_mode) and stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and body == "P12-NONPRODUCTION-TEST-ONLY\n" and os.environ.get("PI_ACTIVATE_TEST_FIXTURE") == "1"


def _write_display(stream: TextIO, display: dict[str, Any]) -> None:
    lines = [
        "Pi exact generation activation",
        f"staged root: {display['stagedRoot']}",
        f"data root: {display['dataRoot']}",
        f"buildId: {display['buildId']}",
        f"manifestDigest: {display['manifestDigest']}",
        f"piVersion: {display['piVersion']}",
        f"sourceCommit: {display.get('sourceCommit')}",
        f"sourceMode: {display.get('sourceMode')}",
        f"dirtyReleasePaths: {json.dumps(display.get('dirtyReleasePaths', []), sort_keys=True)}",
        f"overlayFiles: {json.dumps(display.get('overlayFiles', []), sort_keys=True)}",
        f"resetState: {display.get('resetState', False)}",
        f"rollback plan: {json.dumps(display['rollbackPlan'], sort_keys=True, separators=(',', ':'))}",
        f"effect: activate one exact accepted build and switch the live data root",
    ]
    stream.write("\n".join(lines) + "\n")
    stream.flush()


def _tty_decision(display: dict[str, Any]) -> str:
    try:
        descriptor = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_NOCTTY", 0))
    except OSError as error:
        raise ActivationCliError("pi-activate requires a real controlling TTY at /dev/tty") from error
    try:
        if not os.isatty(descriptor):
            raise ActivationCliError("pi-activate /dev/tty is not a terminal")
        with os.fdopen(os.dup(descriptor), "w", encoding="utf-8", buffering=1) as output, os.fdopen(os.dup(descriptor), "r", encoding="utf-8", buffering=1) as input_stream:
            _write_display(output, display)
            output.write("Type ACTIVATE or REJECT: ")
            output.flush()
            decision = input_stream.readline(64).strip()
            if decision == "ACTIVATE" and display.get("resetState", False):
                output.write("RESET STATE permanently deletes the existing controller database.\n")
                output.write("Type RESET STATE to confirm: ")
                output.flush()
                if input_stream.readline(64).strip() != "RESET STATE":
                    raise ActivationCliError("state reset requires the exact confirmation RESET STATE")
    finally:
        os.close(descriptor)
    if decision not in {"ACTIVATE", "REJECT"}:
        raise ActivationCliError("activation decision must be exactly ACTIVATE or REJECT")
    return decision.lower()


def _rollback_plan(staged_root: Path, data_root: Path) -> dict[str, Any]:
    target = data_root.resolve()
    backups = sorted((item for item in target.parent.glob(f"{target.name}.rollback.*") if item.is_dir() and not item.is_symlink() and (item / "activation.json").is_file() and (item / "build-manifest.json").is_file() and (item / "bin" / "pi-control").is_file()), key=lambda item: item.stat().st_mtime_ns)
    return {"existingDataRoot": target.exists(), "availableRollbackGenerations": len(backups), "latestRollback": str(backups[-1]) if backups else None}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    staged_root = Path(args.staged_root).expanduser().absolute()
    data_root = Path(args.data_root).expanduser().absolute()
    state_root = Path(args.state_root).expanduser().absolute()
    try:
        verified = verify_stage(staged_root)
        manifest = load_build_manifest(staged_root / "build-manifest.json")
        metadata = manifest.payload.get("metadata", {})
        dirty_paths = tuple(metadata.get("dirtyReleasePaths", ())) if isinstance(metadata, dict) else ()
        overlay_files = tuple(metadata.get("overlayFiles", ())) if isinstance(metadata, dict) else ()
        if (dirty_paths or overlay_files) and not args.allow_dirty:
            raise ActivationCliError("this generation contains uncommitted release files; pass --allow-dirty after reviewing the displayed build")
        display = {
            "stagedRoot": str(staged_root), "dataRoot": str(data_root),
            "buildId": verified["buildId"], "manifestDigest": verified["manifestDigest"],
            "piVersion": verified.get("piVersion", ""), "sourceCommit": manifest.payload.get("sourceCommit"),
            "sourceMode": metadata.get("sourceMode") if isinstance(metadata, dict) else None,
            "dirtyReleasePaths": list(dirty_paths), "overlayFiles": list(overlay_files),
            "resetState": args.reset_state,
            "rollbackPlan": _rollback_plan(staged_root, data_root),
        }
        if args.test_only_decision:
            if not _test_fixture(Path(args.data_root).expanduser().absolute()):
                raise ActivationCliError("noninteractive activation is restricted to an explicit disposable test fixture")
            decision = args.test_only_decision
            _write_display(sys.stderr, display)
        else:
            if args.test_only_defer_execution:
                raise ActivationCliError("deferred execution is test-only")
            decision = _tty_decision(display)
        if decision == "reject":
            result = {"activated": False, "decision": "reject", "buildId": verified["buildId"]}
        else:
            result = activate(staged_root, data_root, state_root=state_root, reset_state=args.reset_state)
            fresh = ensure_fresh_state(state_root) if not (state_root / "control.db").exists() else {"stateRoot": str(state_root), "fresh": False}
            result = {**result, "freshState": fresh}
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, RuntimeError, ControlPlaneError) as error:
        payload = error.as_dict() if isinstance(error, ControlPlaneError) else {"message": str(error)[:1024]}
        print(json.dumps({"error": payload}, sort_keys=True, separators=(",", ":")), file=sys.stderr)
        return 2


__all__ = ["ActivationCliError", "main"]
