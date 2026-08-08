#!/usr/bin/env python3
"""Validate the active greenfield Pi contracts and release surfaces."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
ACTIVE_DOCS = [
    ROOT / "pi/control-plane/PRODUCT_CONTRACT.md",
    ROOT / "pi/control-plane/STATE_CONTRACT.md",
    ROOT / "pi/control-plane/EXECUTION_CONTRACT.md",
    ROOT / "pi/control-plane/CHANGE_INTEGRATION_CONTRACT.md",
    ROOT / "pi/control-plane/OBSERVABILITY_CONTINUITY_CONTRACT.md",
    ROOT / "pi/control-plane/SYSTEM_INTEGRATION_TEST_PLAN.md",
    ROOT / "pi/control-plane/ACCEPTANCE_PLAN.md",
    ROOT / "pi/control-plane/GREENFIELD_CUTOVER_AND_ROLLBACK.md",
    ROOT / "pi/control-plane/PRE_ACTIVATION_ACCEPTANCE_RUNBOOK.md",
]
REQUIRED_MODULES = [
    "scripts/pi_control/greenfield_schema.py",
    "scripts/pi_control/greenfield_store.py",
    "scripts/pi_control/greenfield_client.py",
    "scripts/pi_control/projects.py",
    "scripts/pi_control/conversations.py",
    "scripts/pi_control/messages.py",
    "scripts/pi_control/command_requests.py",
    "scripts/pi_control/network_runner.py",
    "scripts/pi_control/launch.py",
    "scripts/pi_control/writer_lock.py",
    "scripts/pi_control/docker_runtime.py",
    "scripts/pi_control/dependencies.py",
    "scripts/pi_control/package_environment.py",
    "scripts/pi_control/scoped_read.py",
    "scripts/pi_control/greenfield_reconcile.py",
    "scripts/pi_control/greenfield_install.py",
    "scripts/pi-system-run.py",
    "scripts/pi-system-container-run.py",
]
FORBIDDEN_ACTIVE_PHRASES = (
    "legacy -> shadow -> controller",
    "project activation mode",
    "old conversation import",
    "compatibility facade",
    "dual writer",
)


def validate(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    for path in ACTIVE_DOCS:
        if not path.is_file() or path.is_symlink():
            errors.append(f"missing active contract: {path.relative_to(root)}")
            continue
        text = path.read_text(encoding="utf-8").lower()
        for phrase in FORBIDDEN_ACTIVE_PHRASES:
            if phrase in text:
                errors.append(f"forbidden active contract phrase {phrase!r}: {path.relative_to(root)}")
    for relative in REQUIRED_MODULES:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            errors.append(f"missing required greenfield module: {relative}")
    manifest = root / "tests/system/action-manifest.v1.json"
    if manifest.is_file():
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                errors.append("action manifest must be an object")
        except json.JSONDecodeError as error:
            errors.append(f"action manifest is invalid JSON: {error}")
    else:
        errors.append("missing action manifest")
    return errors


def validate_repository(root: Path = ROOT) -> dict[str, object]:
    errors = validate(root)
    return {"ok": not errors, "errors": errors, "activeDocs": [str(path.relative_to(root)) for path in ACTIVE_DOCS]}


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"validate-plan-docs: {error}")
        return 1
    print("validate-plan-docs: greenfield contracts and modules are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
