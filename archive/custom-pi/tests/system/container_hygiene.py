"""Fixture-owned Docker identity and cleanup helpers for installed journeys."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import subprocess
from typing import Any


MANAGED_LABEL = "pi.control.managed"


@dataclass(frozen=True)
class ContainerRef:
    kind: str
    identity: str
    container_id: str | None
    name: str
    labels: dict[str, str]


@dataclass(frozen=True)
class ContainerObservation:
    ref: ContainerRef
    container_id: str | None
    name: str | None
    labels: dict[str, str] | None
    running: bool | None
    present_by_id: bool
    present_by_name: bool
    identity_matches: bool

    @property
    def absent(self) -> bool:
        return not self.present_by_id and not self.present_by_name

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.ref.kind,
            "identity": self.ref.identity,
            "expectedId": self.ref.container_id,
            "expectedName": self.ref.name,
            "actualId": self.container_id,
            "actualName": self.name,
            "labels": self.labels,
            "running": self.running,
            "presentById": self.present_by_id,
            "presentByName": self.present_by_name,
            "identityMatches": self.identity_matches,
            "absent": self.absent,
        }


def _rows(state_root: Path, query: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    connection = sqlite3.connect(state_root / "control.db")
    connection.row_factory = sqlite3.Row
    try:
        return list(connection.execute(query, parameters))
    finally:
        connection.close()


def _writer_labels(row: sqlite3.Row) -> dict[str, str]:
    required = ("run_id", "project_id", "working_copy_id", "writer_epoch", "build_id")
    if any(row[key] is None for key in required):
        raise AssertionError(f"writer run has incomplete container identity: {dict(row)}")
    return {
        MANAGED_LABEL: "true",
        "pi.control.run-id": str(row["run_id"]),
        "pi.control.project-id": str(row["project_id"]),
        "pi.control.working-copy-id": str(row["working_copy_id"]),
        "pi.control.writer-epoch": str(row["writer_epoch"]),
        "pi.control.controller-build-id": str(row["build_id"]),
    }


def fixture_container_refs(state_root: Path) -> list[ContainerRef]:
    """Return only container identities durable in this fixture state root."""
    refs: list[ContainerRef] = []
    for row in _rows(
        state_root,
        "SELECT run_id,project_id,working_copy_id,writer_epoch,build_id,container_id "
        "FROM runs WHERE authority='writer-container'",
    ):
        run_id = str(row["run_id"])
        refs.append(ContainerRef("writer", run_id, row["container_id"], f"pi-tool-{run_id.removeprefix('run_')}", _writer_labels(row)))

    for row in _rows(
        state_root,
        "SELECT command_request_id FROM command_requests WHERE execution_place='container-network'",
    ):
        request_id = str(row["command_request_id"])
        refs.append(ContainerRef(
            "network",
            request_id,
            None,
            f"pi-network-{request_id.split('_', 1)[1]}",
            {MANAGED_LABEL: "true", "pi.control.request-id": request_id, "pi.control.kind": "one-shot-network"},
        ))

    for row in _rows(state_root, "SELECT package_request_id FROM package_requests"):
        request_id = str(row["package_request_id"])
        refs.append(ContainerRef(
            "package",
            request_id,
            None,
            f"pi-package-{request_id.removeprefix('pkreq_')}",
            {MANAGED_LABEL: "true", "pi.control.request-id": request_id, "pi.control.kind": "one-shot-package"},
        ))
    return refs


def _inspect(identifier: str) -> dict[str, Any] | None:
    result = subprocess.run(
        ["docker", "container", "inspect", identifier],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout)
        raw = value[0]
        return {
            "id": str(raw["Id"]),
            "name": str(raw["Name"]).removeprefix("/"),
            "labels": {str(key): str(value) for key, value in (raw.get("Config", {}).get("Labels") or {}).items()},
            "running": bool(raw.get("State", {}).get("Running")),
        }
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise AssertionError(f"docker inspect returned invalid container identity for {identifier}: {error}") from error


def inspect_fixture_containers(state_root: Path) -> list[ContainerObservation]:
    observations: list[ContainerObservation] = []
    for ref in fixture_container_refs(state_root):
        by_id = _inspect(ref.container_id) if ref.container_id else None
        by_name = _inspect(ref.name)
        actual = by_id or by_name
        present_by_id = by_id is not None
        present_by_name = by_name is not None
        identity_matches = (
            actual is not None
            and actual["name"] == ref.name
            and actual["labels"] == ref.labels
            and (ref.container_id is None or actual["id"] == ref.container_id)
            and (not present_by_id or by_id == actual)
            and (not present_by_name or by_name == actual)
        )
        observations.append(ContainerObservation(
            ref,
            actual["id"] if actual else None,
            actual["name"] if actual else None,
            actual["labels"] if actual else None,
            actual["running"] if actual else None,
            present_by_id,
            present_by_name,
            identity_matches,
        ))
    return observations


def assert_fixture_containers_absent(state_root: Path) -> None:
    observations = inspect_fixture_containers(state_root)
    present = [item.as_dict() for item in observations if not item.absent]
    mismatched = [item.as_dict() for item in observations if not item.absent and not item.identity_matches]
    if present:
        detail = {"present": present, "identityMismatches": mismatched}
        raise AssertionError(f"fixture-owned managed containers remain or have mismatched identity: {json.dumps(detail, sort_keys=True)}")


def _run(argv: list[str]) -> None:
    result = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(f"container cleanup command failed: {argv!r}: {result.stderr[-1024:]}")


def cleanup_fixture_containers(state_root: Path) -> list[dict[str, Any]]:
    """Clean only durable fixture identities after validating their ownership."""
    observations = inspect_fixture_containers(state_root)
    mismatched = [item.as_dict() for item in observations if not item.absent and not item.identity_matches]
    if mismatched:
        raise AssertionError(f"refusing cleanup of mismatched fixture containers: {json.dumps(mismatched, sort_keys=True)}")
    cleaned: list[dict[str, Any]] = []
    for item in observations:
        if item.absent:
            cleaned.append(item.as_dict())
            continue
        identifier = item.container_id or item.name
        if item.running:
            _run(["docker", "container", "stop", "--time", "5", identifier])
        _run(["docker", "container", "rm", identifier])
        after = inspect_fixture_containers(state_root)
        current = next(value for value in after if value.ref == item.ref)
        if not current.absent:
            raise AssertionError(f"fixture container absence was not proved: {current.as_dict()}")
        cleaned.append(current.as_dict())
    return cleaned


__all__ = [
    "ContainerObservation",
    "ContainerRef",
    "assert_fixture_containers_absent",
    "cleanup_fixture_containers",
    "fixture_container_refs",
    "inspect_fixture_containers",
]
