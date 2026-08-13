"""Tests for fixture-owned container identity and cleanup."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from tests.system import container_hygiene


def _state(root: Path) -> Path:
    state = root / "state"
    state.mkdir()
    connection = sqlite3.connect(state / "control.db")
    connection.executescript(
        """
        CREATE TABLE runs (run_id TEXT, project_id TEXT, working_copy_id TEXT, writer_epoch INTEGER, build_id TEXT, container_id TEXT, authority TEXT);
        CREATE TABLE command_requests (command_request_id TEXT, execution_place TEXT);
        CREATE TABLE package_requests (package_request_id TEXT);
        """
    )
    connection.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)", ("run_" + "a" * 32, "prj_" + "b" * 32, "wc_" + "c" * 32, 4, "build_" + "d" * 32, "e" * 64, "writer-container"))
    connection.execute("INSERT INTO command_requests VALUES (?,?)", ("cmd_" + "f" * 32, "container-network"))
    connection.execute("INSERT INTO command_requests VALUES (?,?)", ("cmd_" + "0" * 32, "host"))
    connection.execute("INSERT INTO package_requests VALUES (?)", ("pkreq_" + "1" * 32,))
    connection.commit()
    connection.close()
    return state


class ContainerHygieneTests(unittest.TestCase):
    def test_refs_include_only_fixture_owned_container_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            refs = container_hygiene.fixture_container_refs(_state(Path(raw)))
        self.assertEqual([item.kind for item in refs], ["writer", "network", "package"])
        self.assertEqual(refs[0].name, "pi-tool-" + "a" * 32)
        self.assertEqual(refs[1].name, "pi-network-" + "f" * 32)
        self.assertEqual(refs[2].name, "pi-package-" + "1" * 32)

    def test_foreign_managed_container_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = _state(Path(raw))
            def inspect(identifier: str):
                if identifier == "e" * 64:
                    return {"id": identifier, "name": "pi-tool-" + "a" * 32, "labels": dict(container_hygiene.fixture_container_refs(state)[0].labels), "running": False}
                return None
            with mock.patch.object(container_hygiene, "_inspect", side_effect=inspect):
                self.assertIsNone(container_hygiene.inspect_fixture_containers(state)[1].container_id)

    def test_mismatched_identity_is_refused_without_docker_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = _state(Path(raw))
            with mock.patch.object(container_hygiene, "_inspect", return_value={"id": "e" * 64, "name": "pi-tool-" + "a" * 32, "labels": {"pi.control.managed": "true", "pi.control.run-id": "other"}, "running": True}), mock.patch.object(container_hygiene, "_run") as run:
                with self.assertRaisesRegex(AssertionError, "mismatched"):
                    container_hygiene.cleanup_fixture_containers(state)
                run.assert_not_called()

    def test_matching_container_is_stopped_removed_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = _state(Path(raw))
            refs = container_hygiene.fixture_container_refs(state)
            matching = {"id": refs[0].container_id, "name": refs[0].name, "labels": refs[0].labels, "running": True}
            calls: list[str] = []
            def inspect(identifier: str):
                if identifier in {refs[0].container_id, refs[0].name} and "removed" not in calls:
                    return matching
                return None
            def run(argv: list[str]) -> None:
                if argv[2] == "stop":
                    calls.append("stopped")
                else:
                    calls.append("removed")
            with mock.patch.object(container_hygiene, "_inspect", side_effect=inspect), mock.patch.object(container_hygiene, "_run", side_effect=run):
                result = container_hygiene.cleanup_fixture_containers(state)
            self.assertEqual(calls, ["stopped", "removed"])
            self.assertEqual(len(result), 3)


if __name__ == "__main__":
    unittest.main()
