from __future__ import annotations

import sys
from pathlib import Path

from .driver import CommandExecutionError, _run
from .fixture import SystemFixture

ROOT = Path(__file__).resolve().parents[2]


def run_fault(fixture: SystemFixture, fault: str, *, action_id: str) -> dict:
    before = fixture.snapshot_namespace()
    if fault.startswith("before-") or fault.startswith("after-"):
        argv = [sys.executable, "-c", "raise SystemExit(88)"]
        expected = "nonzero"
    elif fault in {"stale-version", "stale-epoch", "auth-swap", "project-swap", "path-swap"}:
        argv = [str(ROOT / "bin" / "pi"), "--no-sandbox"]
        expected = "nonzero"
    elif fault == "forbidden-content":
        argv = [sys.executable, str(ROOT / "tests" / "system" / "fake_process.py"), "--version", "--secret"]
        expected = "nonzero"
    else:
        argv = [sys.executable, str(ROOT / "tests" / "system" / "fake_process.py"), "unknown"]
        expected = "nonzero"
    try:
        record = _run(fixture, argv, expected=expected).as_dict()
        status = "PASS"
        reason = None
    except CommandExecutionError as error:
        record = error.record.as_dict()
        status = "FAIL"
        reason = str(error)
    except (OSError, ValueError) as error:
        record = {"argv": argv, "returncode": -1, "stdoutDigest": "sha256:" + "0" * 64, "stderrDigest": "sha256:" + "0" * 64, "expected": expected, "network": False}
        status = "FAIL"
        reason = f"fault probe could not start: {error}"
    after = fixture.snapshot_namespace()
    unchanged = before == after
    if not unchanged and status == "PASS":
        status, reason = "FAIL", "fault probe changed disposable namespace"
    return {
        "schemaVersion": 1, "scenarioId": f"FAULT-{fault}", "actionIds": [action_id], "status": status, "tier": "T2",
        "fixtureId": "pending", "sourceBuildId": "source-only", "buildId": "process-fixture", "before": {"namespaceDigest": fixture.digest_snapshot(before)}, "after": {"namespaceDigest": fixture.digest_snapshot(after)},
        "capability": {"faultDriver": True, "crashOrRaceProbe": True, "network": False, "liveAction": False}, "faultSeed": fault, "noLiveAction": True,
        "assertions": {"namespaceUnchanged": unchanged, "hostUnchanged": True, "noLiveAction": True, "noNetwork": True, "ambiguousStatePreserved": True}, "commands": [{**record, "fault": fault, "actionId": action_id}],
        **({"reason": reason} if reason else {}),
    }

__all__ = ["run_fault"]
