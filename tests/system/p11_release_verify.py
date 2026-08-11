"""P11 release evidence aggregator: every HA action covered by installed PASS evidence on one build."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.system.evidence import DEFAULT_ACTION_MANIFEST, validate_release_evidence


def _load_evidence(paths: list[Path]) -> list[dict]:
    envelopes: list[dict] = []
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"evidence path is not a regular file: {path}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"evidence is not valid JSON: {path}") from error
        validate_release_evidence(value)
        envelopes.append(value)
    return envelopes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="p11-release-verify")
    parser.add_argument("--evidence-dir", action="append", default=[], required=True)
    parser.add_argument("--manifest", default=str(DEFAULT_ACTION_MANIFEST))
    args = parser.parse_args(argv)

    files: list[Path] = []
    for directory in args.evidence_dir:
        root = Path(directory)
        if not root.is_dir():
            print(f"STOP: evidence directory is unavailable: {root}", file=sys.stderr)
            return 77
        for item in sorted(root.rglob("*.json")):
            if not item.is_file() or item.is_symlink():
                continue
            if "opencode" in item.parts:
                continue
            files.append(item)
    if not files:
        print("STOP: no evidence envelopes found", file=sys.stderr)
        return 77

    envelopes = _load_evidence(files)
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    catalog = {action["actionId"]: action for action in manifest["actions"]}

    build_ids = {value["buildId"] for value in envelopes if value["status"] == "PASS" and value["tier"] in {"staged-installed", "docker"}}
    covered: dict[str, list[str]] = {}
    failures: list[str] = []
    for value in envelopes:
        for action_id in value["actionIds"]:
            if value["status"] != "PASS":
                failures.append(f"{action_id}: {value['status']} at {value['scenarioId']}")
                continue
            covered.setdefault(action_id, []).append(f"{value['scenarioId']}/{value['tier']}")
    for action_id in sorted(catalog):
        if action_id not in covered:
            failures.append(f"{action_id}: no PASS evidence")

    # The single-artifact rule binds the installed/docker tiers to one build.
    # Activation and rollback tiers legitimately stage multiple generations.
    if len(build_ids) != 1:
        failures.append(f"installed/docker PASS evidence spans multiple build IDs: {sorted(build_ids)}")

    print("release evidence coverage:")
    for action_id in sorted(catalog):
        scenarios = covered.get(action_id, [])
        print(f"  {action_id}: {'PASS ' + ', '.join(scenarios) if scenarios else 'MISSING'}")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"PASS: all {len(catalog)} actions covered by installed evidence on build {sorted(build_ids)[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
