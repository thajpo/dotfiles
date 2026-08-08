from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import hashlib

try:
    from .action_driver import run_action
    from .fault_driver import run_fault
    from .action_catalog import action_ids_for_group
    from .evidence import aggregate, write_evidence
    from .fixture import SystemFixture
except ImportError:
    from action_driver import run_action
    from fault_driver import run_fault
    from action_catalog import action_ids_for_group
    from evidence import aggregate, write_evidence
    from fixture import SystemFixture

GROUPS = {
    "launch-session-presentation": ["launchers", "sessions", "personal", "secretary", "presentation"],
    "parent-secretary-workstream": ["tools", "children", "workstreams", "host_command_feedback", "cleanup_publication", "reviews_integration"],
    "controller-change-ui": ["cli_actions", "projects", "runs", "changes", "personal", "workstreams", "reviews_integration", "continuity_observability", "cleanup_publication"],
    "migration-admin": ["migration", "installation", "recovery_security", "docker"],
}
JOURNEYS = {
    "JOURNEY-01": ["HA-001", "HA-020", "HA-060"],
    "JOURNEY-02": ["HA-005", "HA-068", "HA-071"],
    "JOURNEY-03": ["HA-020", "HA-030", "HA-081"],
    "JOURNEY-04": ["HA-001", "HA-002", "HA-009", "HA-010"],
    "JOURNEY-05": ["HA-100", "HA-104", "HA-110"],
    "JOURNEY-06": ["HA-087", "HA-088", "HA-089"],
    "JOURNEY-07": ["HA-120", "HA-121", "HA-128"],
}


def _fixture_id(fixture: SystemFixture) -> str:
    return "fixture_" + hashlib.sha256(str(fixture.root).encode()).hexdigest()[:32]


def _set_identity(result: dict, fixture: SystemFixture) -> dict:
    value = dict(result)
    value["fixtureId"] = _fixture_id(fixture)
    value["sourceBuildId"] = os.environ.get("PI_SYSTEM_SOURCE_BUILD_ID", "source-only")
    value["buildId"] = os.environ.get("PI_SYSTEM_BUILD_ID", "process-fixture")
    value["faultSeed"] = os.environ.get("PI_SYSTEM_FAULT_SEED", "none")
    value["noLiveAction"] = True
    return value


def _run_journey(fixture: SystemFixture, journey_id: str, action_ids: list[str]) -> dict:
    before = fixture.snapshot_namespace()
    results = []
    for action_id in action_ids:
        result = run_action(fixture, action_id, scenario_id=journey_id)
        results.append(result)
        if result["status"] != "PASS":
            break
    after = fixture.snapshot_namespace()
    commands = [command for result in results for command in result.get("commands", [])]
    status = "FAIL" if any(result["status"] == "FAIL" for result in results) else ("STOP" if any(result["status"] == "STOP" for result in results) else "PASS")
    reason = next((result.get("reason") for result in results if result.get("reason")), None)
    return {
        "schemaVersion": 1, "scenarioId": journey_id, "actionIds": action_ids, "status": status, "tier": "T2",
        "fixtureId": _fixture_id(fixture), "sourceBuildId": os.environ.get("PI_SYSTEM_SOURCE_BUILD_ID", "source-only"), "buildId": os.environ.get("PI_SYSTEM_BUILD_ID", "process-fixture"),
        "before": {"namespaceDigest": fixture.digest_snapshot(before)}, "after": {"namespaceDigest": fixture.digest_snapshot(after)},
        "capability": {"processFixture": True, "journey": True, "network": False, "liveAction": False}, "faultSeed": os.environ.get("PI_SYSTEM_FAULT_SEED", "none"), "noLiveAction": True,
        "assertions": {"namespaceUnchanged": before == after, "hostUnchanged": True, "noLiveAction": True, "noNetwork": True, "firstFailureStops": True}, "commands": commands,
        **({"reason": reason} if reason else {}),
    }


def _write_results(results: list[dict]) -> None:
    destination = os.environ.get("PI_SYSTEM_EVIDENCE_DIR")
    if not destination:
        return
    directory = Path(destination).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    used: set[str] = set()
    for index, result in enumerate(results):
        base = result["scenarioId"].replace("/", "_") + "-" + "-".join(result["actionIds"])
        filename = base + ".json"
        if filename in used:
            filename = f"{base}-{index}.json"
        used.add(filename)
        write_evidence(result, directory / filename)
    (directory / "aggregate.json").write_text(json.dumps({"schemaVersion": 1, "statuses": [item["status"] for item in results], "exitCode": aggregate(item["status"] for item in results)}, sort_keys=True) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(); parser.add_argument("--group", required=True); args = parser.parse_args(argv)
    if "PI_SYSTEM_PROCESS_FIXTURE" not in os.environ:
        return 77
    results: list[dict] = []
    if args.group == "journeys":
        # A journey intentionally shares one fixture for its ordered state
        # transitions, but every journey gets an independent fixture.
        for journey_id, action_ids in JOURNEYS.items():
            with SystemFixture.create() as fixture:
                results.append(_run_journey(fixture, journey_id, action_ids))
                fixture.assert_host_unchanged()
    else:
        module_names = GROUPS.get(args.group)
        if module_names is None:
            return 2
        allowed_actions = set(action_ids_for_group(args.group))
        for module_name in module_names:
            module = __import__(f"tests.system.scenarios.{module_name}", fromlist=["SCENARIOS"])
            for scenario in module.SCENARIOS:
                for action_id in scenario["actionIds"]:
                    if action_id not in allowed_actions:
                        continue
                    # No action may inherit state, files, processes, or a
                    # database from a preceding action.
                    with SystemFixture.create() as fixture:
                        results.append(_set_identity(run_action(fixture, action_id, scenario_id=scenario["scenarioId"]), fixture))
                        fixture.assert_host_unchanged()
        if args.group == "migration-admin":
            recovery = __import__("tests.system.scenarios.recovery_security", fromlist=["FAULTS"])
            for fault in recovery.FAULTS:
                with SystemFixture.create() as fixture:
                    results.append(_set_identity(run_fault(fixture, fault, action_id="HA-107"), fixture))
                    fixture.assert_host_unchanged()
    _write_results(results)
    print(json.dumps({"group": args.group, "statusCounts": {status: sum(item["status"] == status for item in results) for status in {item["status"] for item in results}}, "evidenceCount": len(results)}, sort_keys=True))
    return aggregate([result["status"] for result in results])

if __name__ == "__main__": raise SystemExit(main())
