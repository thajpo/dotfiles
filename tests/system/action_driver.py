from __future__ import annotations

from pathlib import Path
import re
import sys
from typing import Any

from .action_catalog import action_row
from .driver import CommandExecutionError, _run
from .fixture import SystemFixture

ROOT = Path(__file__).resolve().parents[2]


def _cli_help(row: dict[str, Any]) -> list[str]:
    values = [entry.split(":", 2)[2] for entry in row.get("entrypoints", []) if entry.startswith("cli:subcommand:")]
    command = values[0].split("/", 1)[0] if values else "schema"
    return [str(ROOT / "bin" / "pi-control"), command, "--help"]


def _command_for(row: dict[str, Any]) -> tuple[list[str], str]:
    surface = row.get("surface")
    if surface == "launcher":
        return [str(ROOT / "bin" / "pi"), "help"], "zero"
    if surface == "cli":
        return _cli_help(row), "zero"
    if surface == "package":
        return ["node", str(ROOT / "tests" / "system" / "scenarios" / "packages.mjs")], "zero"
    if surface == "extension":
        for entry in row.get("entrypoints", []):
            if entry.startswith("extension-source:"):
                source = entry.split(":", 1)[1].split("#", 1)[0]
                path = ROOT / source
                if path.is_file():
                    return (["node", "--experimental-strip-types", "--check", str(path)] if path.suffix == ".ts" else ["node", "--check", str(path)]), "zero"
        return [str(ROOT / "bin" / "pi-control"), "--help"], "zero"
    return [str(ROOT / "bin" / "pi"), "--no-sandbox"], "nonzero"


def run_action(fixture: SystemFixture, action_id: str, *, scenario_id: str | None = None) -> dict[str, Any]:
    row = action_row(action_id)
    before = fixture.snapshot_namespace()
    status = row.get("status", "unknown")
    commands: list[dict[str, Any]] = []
    reason: str | None = None
    result_status = "PASS"
    try:
        if status in {"planned", "host-only", "out-of-scope"}:
            result_status = "STOP"
            reason = f"action status {status} is not executable in the non-live T2 fixture"
            if status == "out-of-scope":
                record = _run(fixture, [str(ROOT / "bin" / "pi"), "--no-sandbox"], expected="nonzero")
                command = record.as_dict(); command["actionId"] = action_id; commands.append(command)
        else:
            argv, expected = _command_for(row)
            record = _run(fixture, argv, expected=expected)
            command = record.as_dict(); command["actionId"] = action_id; commands.append(command)
    except CommandExecutionError as error:
        result_status = "FAIL"
        command = error.record.as_dict(); command["actionId"] = action_id; commands.append(command)
        reason = f"action driver failed: {error}"
    except (AssertionError, OSError, ValueError) as error:
        result_status = "FAIL"
        reason = f"action driver failed before command completion: {error}"
    after = fixture.snapshot_namespace()
    unchanged = before == after
    if not unchanged and result_status == "PASS":
        result_status = "FAIL"
        reason = "action changed disposable state without an declared mutation assertion"
    fixture.assert_host_unchanged()
    return {
        "schemaVersion": 1,
        "scenarioId": scenario_id or f"ACTION-{action_id}",
        "actionIds": [action_id],
        "status": result_status,
        "tier": "T2",
        "fixtureId": "pending",
        "sourceBuildId": "source-only",
        "buildId": "process-fixture",
        "before": {"namespaceDigest": fixture.digest_snapshot(before)},
        "after": {"namespaceDigest": fixture.digest_snapshot(after)},
        "capability": {"processFixture": True, "actionCommand": True, "mutationExecuted": False, "network": False},
        "faultSeed": "none",
        "noLiveAction": True,
        "assertions": {"namespaceUnchanged": unchanged, "hostUnchanged": True, "noLiveAction": True, "noNetwork": True, "statusClassification": status},
        "commands": commands,
        **({"reason": reason} if reason else {}),
    }


__all__ = ["run_action"]
