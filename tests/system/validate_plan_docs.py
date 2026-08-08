#!/usr/bin/env python3
"""Read-only C0b action/plan validation.

This module deliberately uses source text, Python AST, and JSON only.  It never
imports product modules or invokes a launcher, extension, package, Git command,
or external service.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[2]
SYSTEM = ROOT / "tests" / "system"
PLAN = ROOT / "pi" / "control-plane" / "SYSTEM_INTEGRATION_TEST_PLAN.md"
BRIEFS = ROOT / "pi" / "control-plane" / "IMPLEMENTATION_SLICE_BRIEFS.md"
SETTINGS = ROOT / "pi" / "settings.json"
MAX_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024
STATUSES = {"supported", "compatibility", "planned", "host-only", "out-of-scope"}
REQUIRED_ACTION_FIELDS = {
    "actionId", "name", "surface", "entrypoints", "authority", "mutationClass",
    "authorizationClass", "modes", "scenarios", "tiers", "assertions", "risk", "status",
}
BRIEF_HEADINGS = (
    "#### Goal", "#### Prerequisites", "#### Required reading", "#### Allowed files",
    "#### Must remain unchanged", "#### Required behavior", "#### Failure and retry behavior",
    "#### Tests to add first", "#### Acceptance commands", "#### Stop and escalate",
)


class ValidationFailure(Exception):
    """A concise collection of deterministic validation errors."""

    def __init__(self, errors: Iterable[str]):
        self.errors = tuple(dict.fromkeys(str(error) for error in errors))
        super().__init__("; ".join(self.errors))


def _bounded_bytes(path: Path, limit: int = MAX_BYTES) -> bytes:
    if not path.exists() or not path.is_file() or path.is_symlink():
        raise ValidationFailure([f"missing or unsafe file: {path}"])
    size = path.stat().st_size
    if size > limit:
        raise ValidationFailure([f"file exceeds {limit} byte bound: {path}"])
    return path.read_bytes()


def read_text(path: Path, limit: int = MAX_SOURCE_BYTES) -> str:
    try:
        return _bounded_bytes(path, limit).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationFailure([f"file is not UTF-8: {path}"]) from exc


def load_json(path: Path) -> Any:
    try:
        return json.loads(_bounded_bytes(path).decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationFailure([f"invalid JSON {path}: line {exc.lineno} column {exc.colno}"]) from exc
    except UnicodeDecodeError as exc:
        raise ValidationFailure([f"JSON is not UTF-8: {path}"]) from exc


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _resource(resource_id: str, kind: str, source: str, **extra: Any) -> dict[str, Any]:
    value = {"resourceId": resource_id, "kind": kind, "source": source}
    value.update(extra)
    return value


def _literal_string(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class _ArgparseDiscovery(ast.NodeVisitor):
    """Resolve the small, conventional parser builder used by cli.py."""

    def __init__(self) -> None:
        self.paths: dict[str, tuple[str, ...]] = {}
        self.resources: list[dict[str, Any]] = []
        self.parser_vars: set[str] = set()

    def _targets(self, node: ast.AST) -> list[str]:
        if isinstance(node, ast.Name):
            return [node.id]
        if isinstance(node, (ast.Tuple, ast.List)):
            result: list[str] = []
            for item in node.elts:
                result.extend(self._targets(item))
            return result
        return []

    def _receiver_path(self, func: ast.AST) -> tuple[str, ...] | None:
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            return self.paths.get(func.value.id)
        return None

    def visit_Assign(self, node: ast.Assign) -> Any:
        value = node.value
        path: tuple[str, ...] | None = None
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
            attr = value.func.attr
            receiver = self._receiver_path(value.func)
            if attr == "ArgumentParser":
                path = ()
            elif attr == "add_subparsers":
                path = receiver
            elif attr == "add_parser" and receiver is not None and value.args:
                name = _literal_string(value.args[0])
                if name:
                    path = receiver + (name,)
                    self.resources.append(_resource(
                        f"cli:subcommand:{'/'.join(path)}", "cli-subcommand", "scripts/pi_control/cli.py",
                        path=list(path), line=node.lineno,
                    ))
            if path is not None:
                for target in node.targets:
                    for name in self._targets(target):
                        self.paths[name] = path
                        self.parser_vars.add(name)
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> Any:
        self._visit_call(node.value, node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        self._visit_call(node, node.lineno)
        self.generic_visit(node)

    def _visit_call(self, node: ast.AST, line: int) -> None:
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            return
        receiver = self._receiver_path(node.func)
        if receiver is None:
            return
        if node.func.attr == "add_argument" and node.args:
            names = [_literal_string(item) for item in node.args]
            names = [item for item in names if item]
            if not names:
                return
            path = "/".join(receiver) or "root"
            for name in names:
                label = name if name.startswith("-") else f"<{name}>"
                self.resources.append(_resource(
                    f"cli:argument:{path}:{label}", "cli-argument", "scripts/pi_control/cli.py",
                    path=list(receiver), argument=name, line=line,
                ))


def discover_cli(root: Path = ROOT) -> list[dict[str, Any]]:
    path = root / "scripts" / "pi_control" / "cli.py"
    try:
        tree = ast.parse(read_text(path), filename=str(path))
    except SyntaxError as exc:
        raise ValidationFailure([f"cannot parse CLI source {path}: line {exc.lineno}"]) from exc
    visitor = _ArgparseDiscovery()
    visitor.visit(tree)
    return sorted(visitor.resources, key=lambda item: item["resourceId"])


def _case_alternatives(text: str) -> set[str]:
    values: set[str] = set()
    # Case arms are the safest static source for accepted launcher words and
    # flags.  Do not execute shell or interpolate variables.
    for line in text.splitlines():
        if ")" not in line:
            continue
        left = line.split(")", 1)[0].strip()
        if (not left or left.startswith(("#", "[[", "((", "printf", "echo")) or
                any(marker in left for marker in ("=", "$(", "||", "&&", "{"))):
            continue
        for token in left.split("|"):
            token = token.strip().strip("'").strip('"')
            # Options are discovered separately.  Only a complete bare case
            # word is a launcher action; this avoids treating shell command
            # fragments inside substitutions as public actions.
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", token) and token not in {
                "awk", "bash", "dash", "fish", "node", "nvim", "python", "python3",
                "sh", "sleep", "true", "vi", "vim", "zsh", "sha256sum", "shasum",
            }:
                values.add(token)
    return values


def _long_flags(text: str) -> set[str]:
    return set(re.findall(r"(?<![A-Za-z0-9_])--[A-Za-z][A-Za-z0-9_-]*", text))


def discover_launcher(path: Path, root: Path = ROOT) -> list[dict[str, Any]]:
    text = read_text(path)
    rel = _rel(root, path)
    values = _case_alternatives(text)
    flags = _long_flags(text)
    # Explicit short options that are part of case patterns, not shell
    # arithmetic or Git command text.
    short_flags: set[str] = set()
    for line in text.splitlines():
        if ")" not in line:
            continue
        left = line.split(")", 1)[0]
        for token in left.split("|"):
            token = token.strip().strip("'").strip('"')
            if re.fullmatch(r"-[A-Za-z][A-Za-z0-9_-]*", token):
                short_flags.add(token)
    resources: list[dict[str, Any]] = []
    for action in sorted(values):
        resources.append(_resource(f"launcher:{rel}#action:{action}", "launcher-action", rel, action=action))
    for flag in sorted(flags | short_flags):
        resources.append(_resource(f"launcher:{rel}#flag:{flag}", "launcher-flag", rel, flag=flag))
    # Keep a stable source resource even for a thin exec wrapper whose public
    # action is implemented by another checked-in helper.
    resources.append(_resource(f"launcher:{rel}#surface", "launcher-surface", rel))
    # A root invocation is public even though bin/pi delegates the actual
    # command to pinned Pi and has no `root)` shell case arm.
    if rel == "bin/pi":
        resources.append(_resource(f"launcher:{rel}#action:root", "launcher-action", rel, action="root"))
    return resources


PUBLIC_LAUNCHERS = (
    "pi", "pi-help-custom", "pi-start", "pi-personal", "pisec", "pi-restart",
    "pi-secretary", "pi-review-agent", "pi-herdr-workstream", "pi-host", "pidev",
    "pi-root-session", "pi-sandbox-gc", "pi-harness-feedback", "pi-secretary-stats",
    "pi-tmux-session", "pi-control", "pi-personal-herdr", "pi-secretary-herdr",
)


def discover_launchers(root: Path = ROOT) -> list[dict[str, Any]]:
    resources: list[dict[str, Any]] = []
    for name in PUBLIC_LAUNCHERS:
        path = root / "bin" / name
        if path.exists():
            resources.extend(discover_launcher(path, root))
    return sorted({item["resourceId"]: item for item in resources}.values(), key=lambda item: item["resourceId"])


def _launcher_extension_loads(path: Path, root: Path) -> list[dict[str, Any]]:
    text = read_text(path)
    rel = _rel(root, path)
    result: list[dict[str, Any]] = []
    # Capture the literal argument expression after -e.  Variable expressions
    # are intentionally represented as dynamic resources, never evaluated.
    for match in re.finditer(r"(?:^|\s)-e\s+(?:\\?\"([^\"]+)\\?\"|'([^']+)'|([^\s)]+))", text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_prefix = text[line_start:match.start()]
        # `[[ -e PATH ]]` and `set -e` are shell syntax, not Pi extension
        # loading.  Only command/array argument occurrences are resources.
        if "[[" in line_prefix or re.search(r"\bset\s*$", line_prefix):
            continue
        expression = next((value for value in match.groups() if value is not None), "")
        token = expression.replace("$", "var-").replace("{", "").replace("}", "").replace("/", "_")
        token = re.sub(r"[^A-Za-z0-9_.:-]+", "_", token).strip("_") or "unknown"
        result.append(_resource(
            f"extension-load:{rel}#e:{token}", "extension-load", rel,
            expression=expression, owningLauncher=rel, profile=rel.removeprefix("bin/").removesuffix(".sh"),
            dynamic="$" in expression, provenance=f"literal -e expression at {rel}:{text[:match.start()].count(chr(10)) + 1}",
        ))
    for match in re.finditer(r"--tools(?:=|\s+)([A-Za-z0-9_,.-]+)", text):
        for tool in match.group(1).split(","):
            if tool:
                result.append(_resource(
                    f"tool-load:{rel}#tool:{tool}", "tool-load", rel, tool=tool,
                    owningLauncher=rel, profile=rel.removeprefix("bin/").removesuffix(".sh"),
                    provenance=f"literal --tools entry at {rel}:{text[:match.start()].count(chr(10)) + 1}",
                ))
    return result


def discover_launcher_loads(root: Path = ROOT) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name in PUBLIC_LAUNCHERS:
        path = root / "bin" / name
        if path.exists():
            result.extend(_launcher_extension_loads(path, root))
    return sorted({item["resourceId"]: item for item in result}.values(), key=lambda item: item["resourceId"])


def _local_source_files(root: Path, entry: Path) -> list[Path]:
    """Follow bounded relative TS/JS imports without loading executable code."""
    queue = [entry]
    seen: set[Path] = set()
    total = 0
    result: list[Path] = []
    import_re = re.compile(r"(?:\bfrom\s*|\bimport\s*(?:\(\s*)?)[\"'](\.[^\"']+)[\"']")
    root_resolved = root.resolve()
    while queue:
        raw_path = queue.pop(0)
        if raw_path.is_symlink():
            raise ValidationFailure([f"relative extension import is a symlink: {raw_path}"])
        path = raw_path.resolve()
        try:
            path.relative_to(root_resolved)
        except ValueError as exc:
            raise ValidationFailure([f"relative extension import escapes repository: {path}"]) from exc
        if path in seen:
            continue
        seen.add(path)
        if len(result) >= 200:
            raise ValidationFailure([f"extension import graph exceeds 200 files: {entry}"])
        if not path.exists() or not path.is_file() or path.is_symlink():
            raise ValidationFailure([f"relative extension import is unavailable: {path}"])
        text = read_text(path)
        total += len(text.encode("utf-8"))
        if total > 16 * 1024 * 1024:
            raise ValidationFailure([f"extension import graph exceeds 16 MiB: {entry}"])
        result.append(path)
        without_comments = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        without_comments = re.sub(r"//[^\n]*", "", without_comments)
        for spec in import_re.findall(without_comments):
            candidate = path.parent / spec
            candidates = [candidate]
            if candidate.suffix in {".js", ".mjs"}:
                # TypeScript package sources commonly publish .js specifiers
                # while the checked-in source retains the .ts suffix.
                candidates.append(candidate.with_suffix(".ts"))
            if not candidate.suffix:
                candidates.extend(candidate.with_suffix(ext) for ext in (".ts", ".mjs", ".js"))
                candidates.extend(candidate / f"index{ext}" for ext in (".ts", ".mjs", ".js"))
            resolved = next((item for item in candidates if item.exists()), None)
            if resolved is None:
                raise ValidationFailure([f"unresolved relative extension import {spec!r} from {path}"])
            queue.append(resolved)
    return result


def parse_registered_resources(text: str, source: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    # Literal names are safe to discover without evaluating TypeScript.  A
    # computed name/callback remains a dynamic, line-stable resource.
    call_re = re.compile(r"(?:registerTool|registerCommand)\s*\(", re.M)
    for match in call_re.finditer(text):
        call = match.group(0)
        kind = "tool" if "registerTool" in call else "command"
        line = text[:match.start()].count("\n") + 1
        tail = text[match.end():match.end() + 200]
        if kind == "command":
            literal = re.match(r"\s*([\"'])([^\"']+)\1", tail)
        else:
            literal = re.search(r"\bname\s*:\s*([\"'])([^\"']+)\1", tail)
        if literal:
            name = literal.group(2)
            resource_id = f"extension:{source}#{kind}:{name}"
            dynamic = False
        else:
            resource_id = f"extension:{source}#{kind}-dynamic:{line}"
            dynamic = True
            name = None
        result.append(_resource(
            resource_id, kind, source, name=name, line=line, dynamic=dynamic,
            provenance=f"static registration scan at {source}:{line}",
        ))
    return result


def _source_path(root: Path, source: str) -> Path:
    return root / source if not source.startswith("/") else Path(source)


def discover_loaded_extensions(root: Path = ROOT, loaded_path: Path | None = None) -> list[dict[str, Any]]:
    loaded_path = loaded_path or root / "tests" / "system" / "loaded-extensions.v1.json"
    document = load_json(loaded_path)
    if not isinstance(document, dict) or not isinstance(document.get("resources"), list):
        raise ValidationFailure(["loaded-extensions.v1.json requires a resources array"])
    result: dict[str, dict[str, Any]] = {}
    declared_ids = {
        record.get("resourceId") for record in document["resources"]
        if isinstance(record, dict) and isinstance(record.get("resourceId"), str)
    }
    for record in document["resources"]:
        if not isinstance(record, dict):
            raise ValidationFailure(["loaded extension resource is not an object"])
        resource_id = record.get("resourceId")
        source = record.get("source")
        if not isinstance(resource_id, str) or not resource_id:
            raise ValidationFailure(["loaded extension resource has no stable resourceId"])
        if not isinstance(source, str) or not source:
            raise ValidationFailure([f"loaded extension {resource_id} has no source"])
        if record.get("dynamic") is True:
            if not isinstance(record.get("provenance"), str) or not record["provenance"].strip():
                raise ValidationFailure([f"dynamic resource {resource_id} has no provenance"])
            if not isinstance(record.get("owningLauncher"), str) or not record["owningLauncher"].strip():
                raise ValidationFailure([f"dynamic resource {resource_id} has no owning launcher"])
            if not isinstance(record.get("profile"), str) or not record["profile"].strip():
                raise ValidationFailure([f"dynamic resource {resource_id} has no owning profile"])
        result[resource_id] = dict(record)
        if record.get("scan") is True and record.get("availability", "available") == "available":
            source_path = _source_path(root, source)
            if not source_path.exists():
                raise ValidationFailure([f"loaded extension source is missing: {resource_id} -> {source}"])
            for local_source in _local_source_files(root, source_path):
                for registration in parse_registered_resources(read_text(local_source), _rel(root, local_source)):
                    if registration["resourceId"] not in declared_ids:
                        raise ValidationFailure([f"unlisted extension registration: {registration['resourceId']}"])
                    result[registration["resourceId"]] = {
                        **registration,
                        "owningLauncher": record.get("owningLauncher", record.get("owningPackage", "")),
                        "profile": record.get("profile", ""),
                        "declaredBy": resource_id,
                    }
    return sorted(result.values(), key=lambda item: item["resourceId"])


def _package_name_version(source: str) -> tuple[str, str]:
    spec = source.removeprefix("npm:")
    index = spec.rfind("@")
    if index <= 0:
        raise ValidationFailure([f"package source has no version: {source}"])
    return spec[:index], spec[index + 1:]


def _package_dir(root: Path, name: str) -> Path:
    return root / "pi" / "npm" / "node_modules" / name


def package_extension_paths(root: Path, source: str, package_json: Mapping[str, Any] | None) -> list[str]:
    if not package_json:
        return []
    pi = package_json.get("pi")
    if not isinstance(pi, dict) or not isinstance(pi.get("extensions"), list):
        return []
    name, _version = _package_name_version(source)
    base = _package_dir(root, name)
    return [(_rel(root, base / value.lstrip("./"))) for value in pi["extensions"] if isinstance(value, str)]


def discover_packages(root: Path = ROOT, package_path: Path | None = None) -> list[dict[str, Any]]:
    package_path = package_path or root / "tests" / "system" / "configured-packages.v1.json"
    settings = load_json(root / "pi" / "settings.json")
    configured = settings.get("packages") if isinstance(settings, dict) else None
    if not isinstance(configured, list) or len(configured) != 13:
        raise ValidationFailure(["pi/settings.json must contain exactly 13 configured packages"])
    document = load_json(package_path)
    rows = document.get("packages") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise ValidationFailure(["configured-packages.v1.json requires a packages array"])
    by_source = {row.get("source"): row for row in rows if isinstance(row, dict)}
    result: list[dict[str, Any]] = []
    for source in configured:
        if not isinstance(source, str) or source not in by_source:
            raise ValidationFailure([f"configured package is missing from catalog: {source}"])
        row = by_source[source]
        name, version = _package_name_version(source)
        current = row.get("current")
        if not isinstance(current, dict):
            raise ValidationFailure([f"package {source} has no current record"])
        if current.get("source") != source or current.get("name") != name or current.get("version") != version:
            raise ValidationFailure([f"package {source} current source/name/version mismatch"])
        staged = row.get("plannedStagedSource")
        if source == "npm:pi-subagents@0.35.1" and staged != "./packages/pi-subagents-control":
            raise ValidationFailure(["pi-subagents staged source is not first-party control package"])
        if source == "npm:@kjrjay/pi-sandbox@0.2.0" and staged != "./packages/pi-sandbox-control":
            raise ValidationFailure(["pi-sandbox staged source is not first-party control package"])
        if not isinstance(row.get("loadedResources"), list) or not row["loadedResources"]:
            raise ValidationFailure([f"package {source} has no loaded resources"])
        if not isinstance(row.get("representative"), dict) or not row["representative"].get("actionId"):
            raise ValidationFailure([f"package {source} has no representative action"])
        if not isinstance(row.get("remoteCapability"), str) or not row["remoteCapability"]:
            raise ValidationFailure([f"package {source} has no remote capability class"])
        package_json_path = _package_dir(root, name) / "package.json"
        package_json: dict[str, Any] | None = None
        if package_json_path.exists() and not package_json_path.is_symlink():
            value = load_json(package_json_path)
            if not isinstance(value, dict):
                raise ValidationFailure([f"package metadata is not an object: {source}"])
            package_json = value
            if value.get("name") != name or value.get("version") != version:
                raise ValidationFailure([f"installed package metadata mismatch: {source}"])
        elif current.get("availability") != "unavailable":
            raise ValidationFailure([f"package source unavailable without unavailable status: {source}"])
        result.extend(_resource(
            f"package:{source}", "package", source, name=name, version=version,
            availability=current.get("availability", "available"),
        ) for _ in [0])
        for extension in package_extension_paths(root, source, package_json):
            result.append(_resource(f"package:{source}#extension:{extension}", "package-extension", extension, package=source))
    return sorted(result, key=lambda item: item["resourceId"])


def discover_host_operations(root: Path = ROOT) -> list[dict[str, Any]]:
    # These are documented host-only boundaries, represented as source-backed
    # observations.  No operation is called by this validator.
    values = (
        ("host:install.sh#apply", "install.sh", "install disposable generation"),
        ("host:bin/pi-sandbox-gc#apply", "bin/pi-sandbox-gc", "apply exact Pi-owned cleanup"),
        ("host:bin/pi-root-session#migrate", "bin/pi-root-session", "apply reviewed root migration"),
        ("host:bin/pi-root-session#cleanup", "bin/pi-root-session", "review managed cleanup"),
        ("host:phase-11d#canary", "pi/control-plane/PHASE_11D_CANARY_RUNBOOK.md", "apply reviewed canary"),
        ("host:phase-11d#rollout", "pi/control-plane/PHASE_11D_CANARY_RUNBOOK.md", "apply later rollout"),
    )
    result = []
    for resource_id, source, operation in values:
        if (root / source).exists() or source.startswith("pi/control-plane/"):
            result.append(_resource(resource_id, "host-operation", source, operation=operation, hostOnly=True))
    return result


def discover_resources(root: Path = ROOT, loaded_path: Path | None = None, package_path: Path | None = None) -> list[dict[str, Any]]:
    resources: dict[str, dict[str, Any]] = {}
    for collection in (
        discover_cli(root), discover_launchers(root), discover_launcher_loads(root),
        discover_loaded_extensions(root, loaded_path), discover_packages(root, package_path),
        discover_host_operations(root),
    ):
        for item in collection:
            resources[item["resourceId"]] = item
    return sorted(resources.values(), key=lambda item: item["resourceId"])


def _parse_action_rows(plan_text: str) -> dict[str, dict[str, Any]]:
    surface = ""
    rows: dict[str, dict[str, Any]] = {}
    for line in plan_text.splitlines():
        heading = re.match(r"^### 7\.\d+ (.+)$", line)
        if heading:
            surface = heading.group(1).split(",", 1)[0].strip().lower()
        match = re.match(r"^\| (HA-\d+) \| (.*?) \| (.*?) \| (.*?) \|$", line)
        if not match:
            match = re.match(r"^\| (HA-\d+) \| (.*?) \| (.*?) \|$", line)
        if match:
            action_id = match.group(1)
            cells = match.groups()
            if len(cells) == 4:
                _id, name, scenarios, proof = cells
            else:
                _id, name, scenarios = cells
                proof = scenarios
                scenarios = ""
            rows[action_id] = {"actionId": action_id, "name": name.strip(), "scenarioText": scenarios.strip(), "proof": proof.strip(), "surfaceText": surface}
    return rows


def action_rows(root: Path = ROOT) -> dict[str, dict[str, Any]]:
    return _parse_action_rows(read_text(root / "pi" / "control-plane" / "SYSTEM_INTEGRATION_TEST_PLAN.md"))


def _scenario_list(text: str, action_id: str, out_of_scope: bool = False) -> list[str]:
    tokens = re.findall(r"[A-Z][A-Z0-9-]+-(?:\d+|[A-Z]+)(?:\.\.\d+)?", text)
    if not tokens:
        return [f"REFUSAL-{action_id}" if out_of_scope else f"TARGET-{action_id}"]
    return sorted(dict.fromkeys(tokens))


def _surface(action_id: str) -> str:
    number = int(action_id[3:])
    if number < 20:
        return "launcher"
    if number < 40:
        return "package"
    if number < 60:
        return "extension"
    if number < 80:
        return "cli"
    if number < 100:
        return "package"
    if number < 120:
        return "host-runbook"
    return "host-runbook"


def _planned_owner(action_id: str) -> str:
    number = int(action_id[3:])
    owners = {
        26: "C7b", 28: "C8c", 44: "C5c", 48: "C7d", 76: "C7e",
        77: "C7e", 80: "C7b", 81: "C7b", 101: "C3a", 103: "C4a",
        106: "C10b", 109: "C4a", 111: "C10c", 112: "C11",
    }
    if number in owners:
        return owners[number]
    if number in (66, 67):
        return "C1c"
    if number in (69,):
        return "C5a"
    if number in (70, 71, 74):
        return "C7c" if number == 71 else "C7d"
    if number in (82, 83, 84, 85):
        return "C8a" if number in (82, 83) else "C8b"
    return "C1c"


def _resource_action(resource: Mapping[str, Any]) -> str:
    rid = resource["resourceId"]
    source = str(resource.get("source", ""))
    if rid.startswith("cli:"):
        if "schema/status" in rid: return "HA-060"
        if "project/" in rid: return "HA-061"
        if "working-copy/" in rid: return "HA-062"
        if "reconcile" in rid or "cli:subcommand:inspect" in rid or "cli:subcommand:status" in rid: return "HA-063"
        if "operation/" in rid or "event/" in rid: return "HA-064"
        if "cli:subcommand:focus" in rid: return "HA-065"
        if "change/" in rid: return "HA-070"
        if "workstream/" in rid: return "HA-069"
        if "personal/" in rid: return "HA-068"
        if "review/" in rid: return "HA-071"
        if "integration/analyze" in rid: return "HA-072"
        if "integration/authorize" in rid: return "HA-073"
        if "integration/integrate" in rid: return "HA-074"
        if "recovery/" in rid: return "HA-075"
        if "migration/inventory" in rid or "legacy/inventory" in rid: return "HA-100"
        if "migration/shadow" in rid: return "HA-102"
        if "build/manifest" in rid: return "HA-104"
    if rid.startswith("host:"):
        if "sandbox-gc" in rid: return "HA-107"
        if "root-session" in rid: return "HA-108"
        if "install" in rid: return "HA-105"
        if "canary" in rid: return "HA-110"
        return "HA-112"
    if rid.startswith("package-source:"):
        if "pi-web-access" in rid or "github-pr" in rid or "usage" in rid: return "HA-089"
        if "subagents" in rid or "sandbox" in rid: return "HA-090"
        return "HA-089"
    if rid.startswith("planned-source:"):
        return "HA-090"
    if rid.startswith("package:"):
        if "pi-btw" in rid: return "HA-027"
        if "image-tools" in rid: return "HA-086"
        if "pi-goal" in rid: return "HA-022"
        if "plan-mode" in rid: return "HA-021"
        if any(name in rid for name in ("pi-vim", "pi-nvim", "pisesh")): return "HA-088"
        if "statusline" in rid: return "HA-085"
        if "usage" in rid or "github-pr" in rid or "web-access" in rid: return "HA-089"
        if "subagents" in rid: return "HA-023"
        if "sandbox" in rid: return "HA-020"
        return "HA-089"
    if rid.startswith("tool-load:"):
        name = rid.rsplit("#tool:", 1)[-1]
        if name in {"read", "grep", "find", "ls", "edit", "write", "bash"}: return "HA-020"
        if name.startswith("secretary_"): return "HA-040" if name in {"secretary_git"} else "HA-049"
        if name in {"host_command"}: return "HA-030"
        if name in {"harness_feedback"}: return "HA-029"
        if name in {"subagent", "subagent_supervisor", "intercom"}: return "HA-023"
        return "HA-020"
    if rid.startswith("launcher:"):
        launcher = rid.split("#", 1)[0].split(":", 1)[1]
        token = rid.rsplit(":", 1)[-1].lstrip("-")
        if launcher == "bin/pi":
            if rid.endswith("#surface"): return "HA-001"
            if token in {"continue", "resume", "session", "session-id"}: return "HA-002"
            if token == "fork": return "HA-003"
            if token == "root": return "HA-001"
            if token in {"help", "help-custom", "version", "h", "v"}: return "HA-013"
            return "HA-120"
        if launcher == "bin/pi-start":
            if token == "project": return "HA-004"
            if token in {"personal"}: return "HA-005"
            if token in {"secretary"}: return "HA-007"
            if token == "host": return "HA-012"
            if token == "all": return "HA-009"
            if token in {"mobile", "herdr"}: return "HA-009"
            return "HA-010" if token == "no-attach" else "HA-013"
        if launcher in {"bin/pi-personal", "bin/pi-personal-herdr"}: return "HA-005"
        if launcher == "bin/pisec":
            if token in {"register", "list", "launch-info"}: return "HA-006"
            if token in {"activate", "use", "swap"}: return "HA-008"
            if token in {"open", "launch", "default"}: return "HA-007"
            return "HA-007"
        if launcher == "bin/pi-restart": return "HA-010"
        if launcher in {"bin/pi-secretary", "bin/pi-secretary-herdr"}: return "HA-007" if token not in {"herdr"} else "HA-011"
        if launcher == "bin/pi-review-agent": return "HA-046"
        if launcher == "bin/pi-herdr-workstream": return "HA-044"
        if launcher == "bin/pi-host": return "HA-012"
        if launcher == "bin/pidev": return "HA-004"
        if launcher == "bin/pi-root-session": return "HA-108"
        if launcher == "bin/pi-sandbox-gc": return "HA-107"
        if launcher == "bin/pi-harness-feedback": return "HA-029"
        if launcher == "bin/pi-root-session": return "HA-108"
        if launcher == "bin/pi-secretary-stats": return "HA-091"
        if launcher == "bin/pi-control": return "HA-060"
        return "HA-013"
    if rid.startswith("extension-load:"):
        return "HA-046" if "review" in rid else "HA-007" if "secretary" in rid else "HA-090"
    if rid.startswith("extension-source:"):
        if "continuity" in source: return "HA-082"
        if "workstream-channel" in source: return "HA-045"
        if "observability" in source: return "HA-084"
        if "secretary" in source: return "HA-040"
        if "fast-mode" in source: return "HA-087"
        if "harness-feedback" in source: return "HA-029"
        return "HA-021"
    if rid.startswith("extension:"):
        name = str(resource.get("name") or "")
        haystack = f"{source} {name}".lower()
        if "pi-goal" in haystack: return "HA-022"
        if "pi-plan-mode" in haystack: return "HA-021"
        if "pi-image-tools" in haystack: return "HA-086"
        if "pi-statusline" in haystack: return "HA-085"
        if "pi-usage" in haystack: return "HA-089"
        if "pi-github-pr" in haystack or "pi-web-access" in haystack: return "HA-089"
        if "control-plane" in haystack:
            mapping = {
                "controller_status": "HA-063", "controller_focus": "HA-065", "controller_submit_change": "HA-070",
                "controller_create_workstream": "HA-069", "controller_request_review": "HA-071", "controller_submit_review": "HA-071",
                "controller_analyze_integration": "HA-072", "controller_authorize_integration": "HA-073",
                "controller_integrate": "HA-074", "controller_recovery_status": "HA-075", "controller_technical_details": "HA-084",
            }
            return mapping.get(name, "HA-063")
        if "secretary" in haystack:
            if name in {"secretary_git_write"}: return "HA-050"
            if name in {"secretary_git_cleanup", "secretary_cleanup_workstream"}: return "HA-049"
            if name in {"secretary_create_reviewer"}: return "HA-046"
            if name in {"secretary_land_reviewed"}: return "HA-047"
            if name in {"secretary_create_integration"}: return "HA-048"
            if name in {"secretary_relaunch_workstream"}: return "HA-044"
            if name in {"secretary_create_workstream", "secretary_list_workstreams"}: return "HA-042"
            if name in {"secretary_open_workstream"}: return "HA-043"
            if name in {"secretary_list_attention", "secretary_acknowledge_attention", "secretary_record_idea"}: return "HA-041"
            return "HA-040"
        if "host-command" in haystack: return "HA-030"
        if "harness-feedback" in haystack: return "HA-029"
        if "workstream-channel" in haystack: return "HA-045"
        if "review-receipt" in haystack: return "HA-046"
        if "continuity" in haystack: return "HA-083" if "command" in str(resource.get("kind")) else "HA-082"
        if "observability" in haystack: return "HA-084"
        if "fast-mode" in haystack: return "HA-087"
        if "subagents" in haystack:
            if name in {"subagents-fleet", "subagents-stop", "subagents-watchdog"}: return "HA-024"
            if name in {"subagents-doctor", "subagents-models", "subagents-profiles", "subagents-load-profile", "subagents-refresh-provider-models", "subagents-generate-profiles", "subagents-check-profile", "subagent-cost"}: return "HA-091"
            if name in {"intercom"}: return "HA-028"
            return "HA-023"
        if "sandbox" in haystack: return "HA-020"
        return "HA-021"
    return "HA-021"


def validate_slice_briefs(root: Path = ROOT, briefs_path: Path | None = None) -> dict[str, Any]:
    briefs_path = briefs_path or root / "pi" / "control-plane" / "IMPLEMENTATION_SLICE_BRIEFS.md"
    text = read_text(briefs_path, MAX_BYTES)
    starts = list(re.finditer(r"^### (C(?:\d+[a-z]?\d?|11)) — ", text, re.M))
    errors: list[str] = []
    if len(starts) != 40:
        errors.append(f"expected 40 lettered briefs, found {len(starts)}")
    seen: set[str] = set()
    for index, match in enumerate(starts):
        label = match.group(1)
        if label in seen:
            errors.append(f"duplicate brief {label}")
        seen.add(label)
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[match.start():end]
        missing = [heading for heading in BRIEF_HEADINGS if heading not in block]
        for heading in missing:
            errors.append(f"{label} missing {heading[5:]}")
        command_section = ""
        if "#### Acceptance commands" in block:
            command_section = block.split("#### Acceptance commands", 1)[1]
            command_section = command_section.split("#### Stop and escalate", 1)[0]
        if not command_section.strip() or not re.search(r"\b(?:run|python|bash|node|git|pytest|unittest|command|runner|shell)\b|```", command_section, re.I):
            errors.append(f"{label} has no acceptance command")
        prerequisite = block.split("#### Prerequisites", 1)[1].split("#### Required reading", 1)[0] if "#### Prerequisites" in block and "#### Required reading" in block else ""
        if re.search(rf"\b{re.escape(label)}\b", prerequisite):
            errors.append(f"{label} has a self-prerequisite")
    if errors:
        raise ValidationFailure(errors)
    return {"briefCount": len(starts), "errors": []}


def _catalog_rows(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = manifest.get("actions")
    if not isinstance(rows, list):
        raise ValidationFailure(["action manifest requires an actions array"])
    return [row for row in rows if isinstance(row, dict)]


def validate_action_manifest(
    root: Path = ROOT,
    manifest_path: Path | None = None,
    launcher_path: Path | None = None,
    loaded_path: Path | None = None,
    package_path: Path | None = None,
) -> dict[str, Any]:
    system = root / "tests" / "system"
    manifest_path = manifest_path or system / "action-manifest.v1.json"
    launcher_path = launcher_path or system / "launcher-surface.v1.json"
    loaded_path = loaded_path or system / "loaded-extensions.v1.json"
    package_path = package_path or system / "configured-packages.v1.json"
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValidationFailure(["action manifest version must be 1"])
    rows = _catalog_rows(manifest)
    errors: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        action_id = row.get("actionId")
        if not isinstance(action_id, str) or not re.fullmatch(r"HA-\d{3}", action_id):
            errors.append(f"invalid actionId: {action_id!r}")
            continue
        if action_id in by_id:
            errors.append(f"duplicate actionId: {action_id}")
        by_id[action_id] = row
        for field in REQUIRED_ACTION_FIELDS:
            if field not in row:
                errors.append(f"{action_id} missing {field}")
        if row.get("status") not in STATUSES:
            errors.append(f"{action_id} has invalid status {row.get('status')!r}")
        for field in ("entrypoints", "modes", "scenarios", "tiers", "assertions"):
            if field in row and (not isinstance(row[field], list) or not row[field] or any(not isinstance(v, str) or not v for v in row[field])):
                errors.append(f"{action_id} requires non-empty string array {field}")
        status = row.get("status")
        if status == "planned":
            owner = row.get("owningSlice")
            if not isinstance(owner, str) or not re.fullmatch(r"C(?:[0-9]+[a-z][0-9]*|11)", owner):
                errors.append(f"{action_id} planned action has invalid owningSlice")
        if status in {"supported", "compatibility", "host-only"}:
            for field in ("scenarios", "tiers", "assertions"):
                if not row.get(field): errors.append(f"{action_id} {status} action has no {field}")
        if status == "out-of-scope" and not row.get("refusalScenarios"):
            errors.append(f"{action_id} out-of-scope action has no refusal scenarios")
        if row.get("refusalScenarios") is not None and (not isinstance(row["refusalScenarios"], list) or not row["refusalScenarios"]):
            errors.append(f"{action_id} refusalScenarios must be a non-empty array")
    try:
        rows_from_plan = action_rows(root)
        expected_ids = set(rows_from_plan)
        if set(by_id) != expected_ids:
            errors.append(f"manifest action IDs differ from System §7: missing={sorted(expected_ids - set(by_id))} orphan={sorted(set(by_id) - expected_ids)}")
    except ValidationFailure as exc:
        errors.extend(exc.errors)
        rows_from_plan = {}

    try:
        discovered = discover_resources(root, loaded_path, package_path)
        loaded_records = discover_loaded_extensions(root, loaded_path)
    except ValidationFailure as exc:
        errors.extend(exc.errors)
        discovered = []
        loaded_records = []
    discovered_ids = {item["resourceId"] for item in discovered}
    loaded_ids = {item["resourceId"] for item in loaded_records}
    loaded_by_id = {item["resourceId"]: item for item in loaded_records}
    for load in discover_launcher_loads(root):
        if load.get("kind") == "extension-load" and load["resourceId"] not in loaded_ids:
            errors.append(f"dynamic extension load missing from loaded catalog: {load['resourceId']}")
    owners: defaultdict[str, list[str]] = defaultdict(list)
    for row in rows:
        for entrypoint in row.get("entrypoints", []) if isinstance(row.get("entrypoints"), list) else []:
            if isinstance(entrypoint, str): owners[entrypoint].append(row.get("actionId", "?"))
    for resource_id in sorted(discovered_ids):
        if not owners.get(resource_id):
            errors.append(f"discovered resource has no manifest owner: {resource_id}")
    for resource_id, action_ids in sorted(owners.items()):
        if resource_id in discovered_ids:
            continue
        if resource_id.startswith(("planned:", "refusal:")):
            continue
        # A package/resource can be explicitly unavailable but must remain in
        # the dynamic allowlist and carry provenance.
        if resource_id in loaded_by_id and loaded_by_id[resource_id].get("dynamic") is True and loaded_by_id[resource_id].get("provenance"):
            continue
        errors.append(f"manifest entrypoint is not discoverable: {resource_id} (owners={action_ids})")
    for row in rows:
        if row.get("status") == "planned":
            for entrypoint in row.get("entrypoints", []):
                if entrypoint in discovered_ids:
                    errors.append(f"planned action is currently discovered: {row.get('actionId')} -> {entrypoint}")

    # A surface catalog is independently checked against launcher source.  It
    # cannot hide a newly added accepted case or option.
    try:
        surface = load_json(launcher_path)
        surface_rows = surface.get("launchers") if isinstance(surface, dict) else None
        if not isinstance(surface_rows, list):
            errors.append("launcher-surface.v1.json requires a launchers array")
            surface_rows = []
        surface_by_source = {item.get("source"): item for item in surface_rows if isinstance(item, dict)}
        launcher_resources = discover_launchers(root)
        launcher_by_source: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for resource in launcher_resources:
            launcher_by_source[resource["source"]].append(resource)
        for source in sorted(surface_by_source):
            if source not in launcher_by_source:
                errors.append(f"launcher surface has no source file: {source}")
        for resource in launcher_resources:
            source = resource["source"]
            record = surface_by_source.get(source)
            if not record:
                errors.append(f"launcher source missing from surface catalog: {source}")
                continue
            rid = resource["resourceId"]
            if "#action:" in rid:
                action = rid.rsplit("#action:", 1)[1]
                if action not in record.get("actions", []) and action != "root":
                    errors.append(f"launcher action missing from surface catalog: {rid}")
            elif "#flag:" in rid:
                flag = rid.rsplit("#flag:", 1)[1]
                if flag not in record.get("publicFlags", []) + record.get("internalFlags", []):
                    errors.append(f"launcher flag missing from surface catalog: {rid}")
        for source, record in surface_by_source.items():
            if not isinstance(source, str) or not isinstance(record.get("actions"), list):
                errors.append(f"invalid launcher surface record: {source}")
                continue
            source_discovered_ids = {item["resourceId"] for item in launcher_by_source.get(source, [])}
            for action in record.get("actions", []):
                if f"launcher:{source}#action:{action}" not in source_discovered_ids and not (source == "bin/pi" and action == "root"):
                    errors.append(f"launcher surface action is not discoverable: {source}#{action}")
            for flag in record.get("publicFlags", []) + record.get("internalFlags", []):
                if f"launcher:{source}#flag:{flag}" not in source_discovered_ids:
                    errors.append(f"launcher surface flag is not discoverable: {source}#{flag}")
    except ValidationFailure as exc:
        errors.extend(exc.errors)

    # Package rows and their resources are checked independently so removing a
    # package cannot be masked by an action entrypoint.
    try:
        package_resources = discover_packages(root, package_path)
        package_ids = {item["resourceId"] for item in package_resources}
        package_document = load_json(package_path)
        package_rows = package_document.get("packages", []) if isinstance(package_document, dict) else []
        for package_row in package_rows:
            if not isinstance(package_row, dict):
                continue
            source = package_row.get("source", "?")
            loaded_resources = package_row.get("loadedResources", [])
            if not isinstance(loaded_resources, list) or not loaded_resources:
                errors.append(f"configured package has no loaded resource list: {source}")
                continue
            for resource_id in loaded_resources:
                if not isinstance(resource_id, str) or not resource_id:
                    errors.append(f"configured package has invalid loaded resource: {source}")
                    continue
                if resource_id not in discovered_ids and resource_id not in loaded_ids and not resource_id.startswith(("planned:", "refusal:")):
                    errors.append(f"configured package loaded resource is unknown: {source} -> {resource_id}")
                if resource_id in discovered_ids and not owners.get(resource_id):
                    errors.append(f"configured package loaded resource has no manifest owner: {resource_id}")
        for resource_id in package_ids:
            if not owners.get(resource_id):
                errors.append(f"configured package resource has no manifest owner: {resource_id}")
    except ValidationFailure as exc:
        errors.extend(exc.errors)

    if errors:
        raise ValidationFailure(errors)
    counts = Counter(row.get("surface") for row in rows)
    statuses = Counter(row.get("status") for row in rows)
    return {
        "actionCount": len(rows), "resourceCount": len(discovered),
        "surfaceCounts": dict(sorted(counts.items())), "statusCounts": dict(sorted(statuses.items())),
        "dynamicResources": sorted(item["resourceId"] for item in discovered if item.get("dynamic") is True),
    }


def validate_repository(root: Path = ROOT) -> dict[str, Any]:
    brief_report = validate_slice_briefs(root)
    manifest_report = validate_action_manifest(root)
    return {"briefs": brief_report, "manifest": manifest_report}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate C0b plans and static action catalogs")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        report = validate_repository(args.root.resolve())
    except ValidationFailure as exc:
        for error in exc.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
