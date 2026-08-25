from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import unittest

from scripts.pisec.models import InvalidRequestError, validate_git_oid, validate_remote_url, validate_sha256
from scripts.pisec.operations import authoritative_workstream_creation


class Phase1ContractTests(unittest.TestCase):
    def test_digest_oid_and_remote_validators_are_strict(self):
        for value in ("A" * 64, "g" * 64, "a" * 63, "a" * 65):
            with self.assertRaises(InvalidRequestError):
                validate_sha256(value)
        for value in ("A" * 40, "g" * 40, "a" * 39, "a" * 41, "a" * 64 + "g"):
            with self.assertRaises(InvalidRequestError):
                validate_git_oid(value)
        for value in (
            "https://git@example.com/repo.git",
            "https://example.com/repo.git?token=secret",
            "ssh://user@example.com/repo.git",
            "git@example.com:repo.git\n--upload-pack=cat",
            "file:///tmp/repo.git",
        ):
            with self.assertRaises(InvalidRequestError):
                validate_remote_url(value)
        self.assertEqual(validate_remote_url("https://example.com/repo.git"), "https://example.com/repo.git")
        self.assertEqual(validate_remote_url("ssh://git@example.com/repo.git"), "ssh://git@example.com/repo.git")
        self.assertEqual(validate_remote_url("git@example.com:repo.git"), "git@example.com:repo.git")

    def test_authoritative_creation_lookup_fails_closed_for_zero_and_duplicate_rows(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE operations(operation_id TEXT, workstream_id TEXT, kind TEXT, state TEXT, created_at TEXT)")
        store = SimpleNamespace(conn=connection)
        workstream_id = "ws_" + "a" * 32
        with self.assertRaisesRegex(Exception, "missing"):
            authoritative_workstream_creation(store, workstream_id)
        for operation_id in ("op_" + "b" * 32, "op_" + "c" * 32):
            connection.execute("INSERT INTO operations VALUES(?,?,?,?,?)", (operation_id, workstream_id, "workstream.create", "succeeded", operation_id))
        with self.assertRaisesRegex(Exception, "multiple"):
            authoritative_workstream_creation(store, workstream_id)

    def test_catalogue_tools_are_bidirectionally_present_by_socket_and_role(self):
        root = Path(__file__).resolve().parents[1]
        entries = json.loads((root / "pisec" / "operation-catalogue.json").read_text())["entries"]
        catalogue_tools = {(entry["socket"], entry["role"], entry["tool"]) for entry in entries if entry["tool"]}
        source = (root / "omp" / "extensions" / "pisec.ts").read_text()
        exposed = set(re.findall(r'(?:semantic|fleet|runtimeTool)\("([^"]+)"', source)) | set(re.findall(r'name: "(pisec_[^"]+)"', source))
        self.assertEqual({tool for _socket, _role, tool in catalogue_tools}, exposed)
        for socket, role, tool in catalogue_tools:
            self.assertEqual(sum(1 for entry in entries if entry["socket"] == socket and entry["role"] == role and entry["tool"] == tool), 1)


if __name__ == "__main__":
    unittest.main()
