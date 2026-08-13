#!/usr/bin/env python3
"""Validate Pi harness contracts and bounded P6 program/catalog progress."""

from __future__ import annotations

import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "PI_IMPLEMENTATION_PLAN.md"
CONTRACTS = (
    ROOT / "pi/control-plane/README.md",
    ROOT / "pi/control-plane/PRODUCT_CONTRACT.md",
    ROOT / "pi/control-plane/STATE_CONTRACT.md",
    ROOT / "pi/control-plane/EXECUTION_CONTRACT.md",
    ROOT / "pi/control-plane/CHANGE_INTEGRATION_CONTRACT.md",
    ROOT / "pi/control-plane/OBSERVABILITY_CONTINUITY_CONTRACT.md",
    ROOT / "pi/control-plane/SYSTEM_INTEGRATION_TEST_PLAN.md",
    ROOT / "pi/control-plane/ACCEPTANCE_PLAN.md",
    ROOT / "pi/control-plane/CUTOVER_AND_ROLLBACK.md",
    ROOT / "pi/control-plane/PRE_ACTIVATION_ACCEPTANCE_RUNBOOK.md",
)
ACTIVE_DOCS = list(CONTRACTS[1:])
CANONICAL_DOCS = [PLAN, *CONTRACTS]
RETIRED_DOCS = [
    ROOT / "pi/MIGRATION.md",
    ROOT / "pi/OBSERVABILITY_UI_PLAN.md",
    ROOT / "pi/control-plane/MVP_IMPLEMENTATION_PLAN.md",
    ROOT / "pi/control-plane/IMPLEMENTATION_SLICE_BRIEFS.md",
    ROOT / "pi/control-plane/COMPLETION_IMPLEMENTATION_PLAN.md",
    ROOT / "pi/control-plane/CORRECTION_LEDGER.md",
    ROOT / "pi/control-plane/POST_IMPLEMENTATION_AUDIT_AND_REMEDIATION.md",
    ROOT / "pi/control-plane/MIGRATION_ACTIVATION_PLAN.md",
    ROOT / "pi/control-plane/PHASE_11D_CANARY_RUNBOOK.md",
]
CATALOG_FILES = (
    "tests/system/action-manifest.schema.json",
    "tests/system/action-manifest.v1.json",
    "tests/system/launcher-surface.v1.json",
    "tests/system/loaded-extensions.v1.json",
    "tests/system/configured-packages.v1.json",
    "pi/pi-resources.v1.json",
)
MECHANICAL_STATUSES = {
    "not-started", "source-passed", "installed-passed",
    "cumulative-passed", "release-passed",
}
PHASES = {f"P{index}" for index in range(13)}
INVARIANTS = {
    "GF-PLAT-001", "GF-TOPO-001", "GF-TOOL-001", "GF-DOCKER-001",
    "GF-MANIFEST-001", "GF-STATE-001", "GF-REACH-001",
    "GF-CHANNEL-001", "GF-DEPS-001", "GF-APPROVAL-001", "GF-GIT-001",
    "GF-CUTOVER-001",
}
OWNERSHIP_LINES = {
    "pi/control-plane/README.md": "Owns: document catalog and conflict routing only.",
    "pi/control-plane/PRODUCT_CONTRACT.md": "Owns: user-visible roles, capabilities, and release-1 scope.",
    "pi/control-plane/STATE_CONTRACT.md": "Owns: state identity, authority, freshness, and transitions.",
    "pi/control-plane/EXECUTION_CONTRACT.md": "Owns: process topology, tool execution, run identity, Docker lifecycle,",
    "pi/control-plane/CHANGE_INTEGRATION_CONTRACT.md": "Owns: Git mutation authority, submission, review, and local integration.",
    "pi/control-plane/OBSERVABILITY_CONTINUITY_CONTRACT.md": "Owns: evidence envelopes, user attention, and restart continuity.",
    "pi/control-plane/SYSTEM_INTEGRATION_TEST_PLAN.md": "Owns: release scenario inventory and evidence-tier coverage.",
    "pi/control-plane/ACCEPTANCE_PLAN.md": "Owns: gate evaluation and release-verifier behavior.",
    "pi/control-plane/CUTOVER_AND_ROLLBACK.md": "Owns: atomic cutover and rollback transaction.",
    "pi/control-plane/PRE_ACTIVATION_ACCEPTANCE_RUNBOOK.md": "Owns: human pre-activation procedure.",
}
REQUIRED_PLAN_SECTIONS = (
    "## Program Status", "## Document Authority", "## Target Topology",
    "## Trust Boundary", "## Invariant Index", "## Role And Authority Matrix",
    "## P0 Change Boundary", "## Dependency DAG And Gates",
    "## Acceptance And Evidence Rules", "## Decision Ledger",
    "## Release Reachability And Legacy Exclusion", "## Cutover Authority",
)
ALLOWED_PI_TASK_LINES = {
    "Every run has one identity document exported as `PI_RUNTIME_MANIFEST`. No",
    "`PI_TASK_*` compatibility behavior is permitted.",
    "controller-scoped host adapters. Personal, workstream, and integration writer",
    "authority, working copy, build, and writer generation, then creates one run",
    "identity exported as `PI_RUNTIME_MANIFEST`. No `PI_TASK_*` compatibility",
    "behavior exists.",
}
EXCLUDED_RELEASE_ENTRYPOINTS = {
    "scripts/pi-runtime.py", "scripts/pi-workspace.py",
    "scripts/pi-secretary-control.py", "scripts/pi-root-session.py",
    "scripts/pi_control/schema.py", "scripts/pi_control/store.py",
    "scripts/pi_control/client.py", "scripts/pi_control/cli.py",
}
ACTION_REQUIRED = {
    "actionId", "name", "surface", "entrypoints", "authority",
    "mutationClass", "authorizationClass", "modes", "scenarios", "tiers",
    "assertions", "risk", "status", "owningPhase",
}
ACTION_STATUSES = {"implemented-source", "planned", "out-of-scope"}
ACTION_SURFACES = {"launcher", "cli", "extension", "controller", "installer"}
ACTION_MUTATIONS = {"read", "controller", "git", "runtime", "presentation", "host"}
ACTION_AUTHORIZATIONS = {"none", "exact-create", "exact-command", "exact-review", "exact-integrate", "tty-approval", "final-activation"}
ACTION_RISKS = {"normal", "high"}
RETIRED_CATALOG_WORDS = re.compile(r"\b(?:legacy|shadow|compatibility)\b", re.IGNORECASE)
ACTION_OWNERS = {
    "HA-001": "P2", "HA-002": "P4", "HA-003": "P4", "HA-004": "P5",
    "HA-005": "P7", "HA-006": "P6", "HA-007": "P6", "HA-008": "P8",
    "HA-009": "P9", "HA-010": "P1", "HA-011": "P6", "HA-012": "P12",
    "HA-013": "P12", "HA-014": "P6", "HA-015": "P2", "HA-016": "P3",
    "HA-017": "P4", "HA-018": "P7",
    "HA-019": "P7", "HA-020": "P7", "HA-021": "P5", "HA-022": "P4",
    "HA-023": "P7", "HA-024": "P7", "HA-025": "P7", "HA-026": "P7",
    "HA-027": "P7", "HA-028": "P7", "HA-029": "P8",
    "HA-031": "P8", "HA-032": "P7",
}


def _nonempty_strings(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) and bool(item) for item in value)


def _relative(root: Path, template: Path) -> Path:
    return root / template.relative_to(ROOT)


def _section(text: str, heading: str, next_heading: str) -> str:
    start = text.find(heading)
    end = text.find(next_heading, start + len(heading))
    return "" if start < 0 or end < 0 else text[start:end]


def _validate_documents(root: Path, errors: list[str]) -> None:
    documents: dict[str, str] = {}
    for template in CANONICAL_DOCS:
        path = _relative(root, template)
        relative = str(path.relative_to(root))
        if not path.is_file() or path.is_symlink():
            errors.append(f"missing canonical document: {relative}")
            continue
        documents[relative] = path.read_text(encoding="utf-8")

    for template in RETIRED_DOCS:
        path = _relative(root, template)
        if path.exists() or path.is_symlink():
            errors.append(f"retired document returned: {path.relative_to(root)}")

    plan = documents.get("PI_IMPLEMENTATION_PLAN.md", "")
    for heading in REQUIRED_PLAN_SECTIONS:
        if heading not in plan:
            errors.append(f"canonical plan is missing section: {heading}")

    status_section = _section(plan, "## Program Status", "## Document Authority")
    rows = re.findall(r"^\| (P(?:[0-9]|1[0-2])) \| `([^`]+)` \|", status_section, re.MULTILINE)
    if len(rows) != 13 or {phase for phase, _ in rows} != PHASES:
        errors.append("canonical plan must contain exactly one current-status row for P0-P12")
    invalid_statuses = sorted({status for _, status in rows} - MECHANICAL_STATUSES)
    if invalid_statuses:
        errors.append(f"canonical plan uses invalid mechanical status values: {', '.join(invalid_statuses)}")
    row_map = dict(rows)
    if row_map.get("P0") not in {"not-started", "source-passed"}:
        errors.append("P0 may only be not-started or source-passed")
    if row_map.get("P1") not in {"not-started", "source-passed", "installed-passed", "cumulative-passed"}:
        errors.append("P1 may advance only through its non-release gate states")
    if row_map.get("P1") != "not-started" and row_map.get("P0") != "source-passed":
        errors.append("P1 cannot advance before P0 source-passed")
    if row_map.get("P2") != "not-started" and row_map.get("P0") != "source-passed":
        errors.append("P2 cannot advance before P0 source-passed")
    if row_map.get("P2") not in {"not-started", "source-passed", "installed-passed", "cumulative-passed"}:
        errors.append("P2 may advance only through its non-release gate states")
    if row_map.get("P3") not in {"not-started", "source-passed", "installed-passed", "cumulative-passed"}:
        errors.append("P3 may advance only through its non-release gate states")
    if row_map.get("P3") != "not-started" and (row_map.get("P1") != "cumulative-passed" or row_map.get("P2") != "cumulative-passed"):
        errors.append("P3 cannot advance before P1 and P2 cumulative-passed")
    if row_map.get("P4") not in {"not-started", "source-passed", "installed-passed", "cumulative-passed"}:
        errors.append("P4 may advance only through its non-release gate states")
    if row_map.get("P4") != "not-started" and (row_map.get("P2") != "cumulative-passed" or row_map.get("P3") != "cumulative-passed"):
        errors.append("P4 cannot advance before P2 and P3 cumulative-passed")
    if row_map.get("P5") not in {"not-started", "source-passed", "installed-passed", "cumulative-passed"}:
        errors.append("P5 may advance only through its non-release gate states")
    if row_map.get("P5") != "not-started" and (row_map.get("P2") != "cumulative-passed" or row_map.get("P3") != "cumulative-passed"):
        errors.append("P5 cannot advance before P2 and P3 cumulative-passed")
    if row_map.get("P6") not in {"not-started", "source-passed", "installed-passed", "cumulative-passed"}:
        errors.append("P6 may advance only through its non-release gate states")
    if row_map.get("P6") != "not-started" and (row_map.get("P2") != "cumulative-passed" or row_map.get("P3") != "cumulative-passed"):
        errors.append("P6 cannot advance before P2 and P3 cumulative-passed")
    if row_map.get("P7") != "not-started" and any(row_map.get(phase) != "cumulative-passed" for phase in ("P4", "P5", "P6")):
        errors.append("P7 cannot advance before P4-P6 cumulative-passed")
    if row_map.get("P8") != "not-started" and row_map.get("P7") != "cumulative-passed":
        errors.append("P8 cannot advance before P7 cumulative-passed")
    if any(row_map.get(f"P{index}") != "not-started" for index in range(9, 13)) and row_map.get("P8") in {"not-started", "source-passed"}:
        errors.append("P9-P12 cannot advance before P8 is at least installed-passed")
    for status in MECHANICAL_STATUSES:
        if f"`{status}`" not in status_section:
            errors.append(f"canonical plan is missing mechanical status vocabulary: {status}")

    dag_section = _section(plan, "## Dependency DAG And Gates", "## Acceptance And Evidence Rules")
    dag_phases = re.findall(r"^\| (P(?:[0-9]|1[0-2])) \|", dag_section, re.MULTILINE)
    if len(dag_phases) != 13 or set(dag_phases) != PHASES:
        errors.append("canonical plan dependency DAG must contain exactly one gate row for P0-P12")

    invariant_rows = set(re.findall(r"^\| (GF-[A-Z]+-[0-9]{3}) \|", plan, re.MULTILINE))
    if invariant_rows != INVARIANTS:
        errors.append("canonical plan invariant index is incomplete or contains unknown IDs")

    required_decisions = (
        "Release 1 is Linux-only.",
        "scripts/pi_control/docker_runtime.py (sole Docker lifecycle owner)",
        "`pi-sandbox-control` is a broker client",
        "one controller-created, controller-owned writer container",
        "One `PI_RUNTIME_MANIFEST`",
        "npm `package-lock.json`",
        "Python `uv.lock` plus hash-pinned requirements",
        "separate TTY-bound host CLI",
        "OpenCode remains unchanged",
    )
    for text in required_decisions:
        if text not in plan:
            errors.append(f"canonical plan is missing accepted decision: {text}")

    all_text = "\n".join(documents.values())
    forbidden_topology = (
        r"\bconversational Pi (?:runs|executes|resides) (?:inside|in) (?:a|the) [^.\n]*container",
        r"\bPi itself runs (?:inside|in) (?:a|the) [^.\n]*container",
        r"\b(?:coding|writer|personal|workstream|integration) (?:Pi|agent|model|session|conversation)s? [^.\n]*(?:runs|resides|is hosted) (?:inside|in) (?:a|the) [^.\n]*container",
        r"\| (?:personal coding agent|workstream coding agent|integration agent) \| container \|",
        r"\bpi-sandbox-control is (?:the )?(?:intended )?sole Docker lifecycle owner",
        r"\bpi-sandbox-control owns Docker lifecycle",
        r"\bDocker lifecycle is owned by pi-sandbox-control",
    )
    for pattern in forbidden_topology:
        if re.search(pattern, all_text, re.IGNORECASE):
            errors.append("canonical documents contain contradictory runtime topology")
            break

    for relative, text in documents.items():
        for line in text.splitlines():
            if "PI_TASK_" in line and line not in ALLOWED_PI_TASK_LINES:
                errors.append(f"target PI_TASK compatibility appears in canonical document: {relative}")
                break

    for relative, owner in OWNERSHIP_LINES.items():
        text = documents.get(relative, "")
        if owner not in text:
            errors.append(f"canonical contract has missing or changed ownership declaration: {relative}")

    contract_requirements = {
        "pi/control-plane/STATE_CONTRACT.md": ("never reads, imports, maps, resumes, reconciles, or adopts historical Pi", "canonical store and CLI are the Pi controller families"),
        "pi/control-plane/EXECUTION_CONTRACT.md": ("Release 1 supports Linux only.", "Every conversational Pi, model, session, and", "scripts/pi_control/docker_runtime.py` is the intended sole Docker lifecycle", "npm projects with a committed `package-lock.json`", "Python projects with a committed `uv.lock`", "TTY-bound host CLI"),
        "pi/control-plane/CHANGE_INTEGRATION_CONTRACT.md": ("host controller alone creates commits", "no writable Git metadata", "No operation pushes"),
        "pi/control-plane/CUTOVER_AND_ROLLBACK.md": ("OpenCode and its configuration remain unchanged",),
    }
    for relative, snippets in contract_requirements.items():
        text = documents.get(relative, "")
        for snippet in snippets:
            if snippet not in text:
                errors.append(f"canonical contract is missing required boundary ({snippet}): {relative}")

    for relative in OWNERSHIP_LINES:
        if relative == "pi/control-plane/README.md":
            continue
        text = documents.get(relative, "")
        duplicated = sorted(status for status in MECHANICAL_STATUSES if re.search(rf"(?<![A-Za-z0-9-]){re.escape(status)}(?![A-Za-z0-9-])", text))
        if duplicated:
            errors.append(f"contract duplicates program status vocabulary: {relative}")


def _validate_action_manifest(root: Path, value: object, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append("action manifest must be an object")
        return
    if set(value) != {"$schema", "version", "sourcePlan", "actions"}:
        errors.append("action manifest has an invalid object shape")
    if value.get("$schema") != "action-manifest.schema.json":
        errors.append("action manifest does not link the canonical schema")
    if value.get("version") != 1 or value.get("sourcePlan") != "PI_IMPLEMENTATION_PLAN.md":
        errors.append("action manifest identity is invalid")
    actions = value.get("actions")
    if not isinstance(actions, list) or not actions:
        errors.append("action manifest requires actions")
        return
    seen: set[str] = set()
    for index, action in enumerate(actions):
        label = f"action manifest row {index + 1}"
        if not isinstance(action, dict) or set(action) != ACTION_REQUIRED:
            errors.append(f"{label} has an invalid object shape")
            continue
        action_id = action.get("actionId")
        if not isinstance(action_id, str) or re.fullmatch(r"HA-[0-9]{3}", action_id) is None or action_id in seen:
            errors.append(f"{label} has an invalid or duplicate actionId")
        else:
            seen.add(action_id)
        for field in ("entrypoints", "scenarios", "tiers", "assertions"):
            if not _nonempty_strings(action.get(field)) or len(set(action[field])) != len(action[field]):
                errors.append(f"{label} requires unique non-empty {field}")
        for field in ("name", "authority"):
            if not isinstance(action.get(field), str) or not action[field]:
                errors.append(f"{label} requires non-empty {field}")
        if action.get("surface") not in ACTION_SURFACES:
            errors.append(f"{label} has an invalid surface")
        if action.get("mutationClass") not in ACTION_MUTATIONS:
            errors.append(f"{label} has an invalid mutationClass")
        if action.get("authorizationClass") not in ACTION_AUTHORIZATIONS:
            errors.append(f"{label} has an invalid authorizationClass")
        if action.get("risk") not in ACTION_RISKS:
            errors.append(f"{label} has an invalid risk")
        if action.get("modes") != ["controller"]:
            errors.append(f"{label} must use only controller mode")
        if action.get("status") not in ACTION_STATUSES:
            errors.append(f"{label} has an invalid status")
        if action.get("owningPhase") not in PHASES:
            errors.append(f"{label} has an invalid owningPhase")
        elif ACTION_OWNERS.get(str(action_id)) != action.get("owningPhase"):
            errors.append(f"{label} has owningPhase drift")
        for entrypoint in action.get("entrypoints", []):
            source = entrypoint.split(" ", 1)[0]
            if source in EXCLUDED_RELEASE_ENTRYPOINTS:
                errors.append(f"{label} makes an excluded runtime/controller family release-reachable: {source}")
            if action.get("status") == "implemented-source" and "/" in source and not (root / source).is_file():
                errors.append(f"{label} references a missing implemented entrypoint: {source}")


def _validate_support_catalogs(root: Path, catalogs: dict[str, object], errors: list[str]) -> None:
    launcher = catalogs.get("tests/system/launcher-surface.v1.json")
    if isinstance(launcher, dict):
        implemented = launcher.get("releaseCanary")
        planned = launcher.get("plannedLaunchers")
        excluded = launcher.get("excludedLaunchers")
        if set(launcher) != {"version", "releaseCanary", "plannedLaunchers", "excludedLaunchers", "acceptanceRule"} or launcher.get("version") != 1 or not _nonempty_strings(implemented) or not _nonempty_strings(excluded) or not isinstance(planned, list) or any(not isinstance(item, str) or not item for item in planned) or not isinstance(launcher.get("acceptanceRule"), str) or not launcher["acceptanceRule"]:
            errors.append("launcher catalog has an invalid shape")
        elif set(implemented) & (set(planned) | set(excluded)) or set(planned) & set(excluded):
            errors.append("launcher catalog overlaps release, planned, and excluded paths")
        else:
            for relative in implemented:
                if not (root / relative).is_file():
                    errors.append(f"launcher catalog references a missing implemented path: {relative}")
    extensions = catalogs.get("tests/system/loaded-extensions.v1.json")
    if isinstance(extensions, dict):
        rows = extensions.get("extensions")
        if set(extensions) != {"version", "extensions"} or extensions.get("version") != 1 or not isinstance(rows, list) or not rows:
            errors.append("extension catalog has an invalid shape")
        else:
            paths: set[str] = set()
            for index, row in enumerate(rows):
                if not isinstance(row, dict) or set(row) != {"path", "roles", "artifactStatus", "installedProcessEvidence"} or row.get("artifactStatus") != "packaged" or not isinstance(row.get("installedProcessEvidence"), bool) or not _nonempty_strings(row.get("roles")):
                    errors.append(f"extension catalog row {index + 1} is invalid")
                    continue
                path = row.get("path")
                if not isinstance(path, str) or path in paths:
                    errors.append(f"extension catalog row {index + 1} has an invalid path")
                    continue
                paths.add(path)
                if row["artifactStatus"] == "packaged" and not (root / path).is_file():
                    errors.append(f"extension catalog row {index + 1} references a missing implemented path")
    packages = catalogs.get("tests/system/configured-packages.v1.json")
    if isinstance(packages, dict):
        rows = packages.get("packages")
        if set(packages) != {"version", "packages"} or packages.get("version") != 1 or not isinstance(rows, list) or not rows:
            errors.append("package catalog has an invalid shape")
        else:
            names: set[str] = set()
            for index, row in enumerate(rows):
                if not isinstance(row, dict) or set(row) != {"name", "version", "source", "artifactStatus", "reason"} or row.get("artifactStatus") not in {"packaged", "planned", "out-of-scope"} or any(not isinstance(row.get(field), str) or not row[field] for field in ("name", "version", "source", "reason")) or row["name"] in names:
                    errors.append(f"package catalog row {index + 1} is invalid")
                    continue
                names.add(row["name"])
                source_path = root / row["source"]
                if row["artifactStatus"] == "packaged" and not source_path.exists():
                    errors.append(f"package catalog row {index + 1} references a missing implemented source")
                    continue
                if not source_path.exists():
                    continue
                if source_path.is_dir():
                    try:
                        metadata = json.loads((source_path / "package.json").read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        errors.append(f"package catalog row {index + 1} has unreadable package metadata")
                    else:
                        if metadata.get("name") != row["name"] or metadata.get("version") != row["version"]:
                            errors.append(f"package catalog row {index + 1} does not match package metadata")
                elif source_path.name == "PI_VERSION" and source_path.read_text(encoding="utf-8").strip() != row["version"]:
                    errors.append(f"package catalog row {index + 1} does not match PI_VERSION")
    resource_catalog = catalogs.get("pi/pi-resources.v1.json")
    if isinstance(resource_catalog, dict) and isinstance(launcher, dict) and isinstance(extensions, dict) and isinstance(packages, dict):
        extension_rows = extensions.get("extensions", [])
        configured_rows = packages.get("packages", [])
        expected_extensions = [(row.get("path"), row.get("roles")) for row in extension_rows if isinstance(row, dict)]
        expected_packages = [(row.get("name"), row.get("version"), row.get("source")) for row in configured_rows if isinstance(row, dict)]
        actual_packages = [(row.get("name"), row.get("version"), row.get("source")) for row in resource_catalog.get("packages", []) if isinstance(row, dict)]
        if resource_catalog.get("launchers") != launcher.get("releaseCanary") or resource_catalog.get("excludedLaunchers") != launcher.get("excludedLaunchers"):
            errors.append("production resource catalog differs from the launcher catalog")
        if [(row.get("path"), row.get("roles")) for row in resource_catalog.get("extensions", []) if isinstance(row, dict)] != expected_extensions:
            errors.append("production resource catalog differs from the extension catalog")
        if actual_packages != expected_packages:
            errors.append("production resource catalog differs from the package catalog")


def _validate_catalogs(root: Path, errors: list[str]) -> None:
    catalogs: dict[str, object] = {}
    for relative in CATALOG_FILES:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            errors.append(f"missing catalog: {relative}")
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            errors.append(f"catalog is invalid JSON ({relative}): {error}")
            continue
        if not isinstance(value, dict):
            errors.append(f"catalog must be an object: {relative}")
            continue
        if relative != "tests/system/action-manifest.schema.json" and RETIRED_CATALOG_WORDS.search(json.dumps(value, sort_keys=True)):
            errors.append(f"retired product mode appears in catalog: {relative}")
        catalogs[relative] = value

    schema = catalogs.get("tests/system/action-manifest.schema.json")
    manifest = catalogs.get("tests/system/action-manifest.v1.json")
    if isinstance(schema, dict):
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            errors.append(f"action manifest schema is invalid: {error.message}")
    if isinstance(schema, dict) and isinstance(manifest, dict):
        if manifest.get("$schema") != Path("tests/system/action-manifest.schema.json").name:
            errors.append("action manifest does not link the canonical schema")
        for error in Draft202012Validator(schema).iter_errors(manifest):
            location = ".".join(str(item) for item in error.absolute_path) or "root"
            errors.append(f"action manifest schema violation at {location}: {error.message}")
    if manifest is not None:
        _validate_action_manifest(root, manifest, errors)
    _validate_support_catalogs(root, catalogs, errors)


def validate(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    _validate_documents(root, errors)
    _validate_catalogs(root, errors)
    return errors


def validate_repository(root: Path = ROOT) -> dict[str, object]:
    errors = validate(root)
    return {
        "ok": not errors,
        "errors": errors,
        "canonicalDocs": [str(path.relative_to(ROOT)) for path in CANONICAL_DOCS],
        "retiredDocs": [str(path.relative_to(ROOT)) for path in RETIRED_DOCS],
    }


def main() -> int:
    errors = validate()
    if errors:
        for error in errors:
            print(f"validate-plan-docs: {error}")
        return 1
    print("validate-plan-docs: contracts and bounded P6 progress are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
