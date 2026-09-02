#!/usr/bin/env python3
"""Focused regression tests for workers-summarize transcript recovery."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "extract_user_messages.py"
SPEC = importlib.util.spec_from_file_location("extract_user_messages", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def event(message_id: str, text: str) -> dict:
    return {
        "type": "response_item",
        "timestamp": "2026-01-01T00:00:00Z",
        "payload": {
            "type": "message",
            "role": "user",
            "id": message_id,
            "content": [{"type": "input_text", "text": text}] if text else [],
            "internal_chat_message_metadata_passthrough": {
                "content_item_kinds": ["user.text"],
                "turn_id": "turn-1",
            },
        },
    }


class ExtractUserMessagesTest(unittest.TestCase):
    def write_rollout(self, lines: list[str]) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "rollout-test.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_latest_nonempty_revision_wins(self) -> None:
        path = self.write_rollout(
            [
                json.dumps(event("same-id", "")),
                json.dumps(event("same-id", "draft")),
                json.dumps(event("same-id", "final")),
            ]
        )

        messages, diagnostics = MODULE.extract(path)

        self.assertEqual(messages[0]["source_ref"], "M1")
        self.assertEqual(messages[0]["text"], "final")
        self.assertEqual(diagnostics["revisions_applied"], 1)
        self.assertEqual(diagnostics["unresolved_empty_user_events"], 0)

    def test_injected_and_invalid_user_events_are_not_silently_included(self) -> None:
        injected = event("injected", "environment")
        injected["payload"]["internal_chat_message_metadata_passthrough"][
            "content_item_kinds"
        ] = ["environment_context"]
        invalid = event("invalid", "ambiguous")
        invalid["payload"]["internal_chat_message_metadata_passthrough"] = None
        path = self.write_rollout(
            [json.dumps(injected), json.dumps(invalid), json.dumps(event("real", "hello"))]
        )

        messages, diagnostics = MODULE.extract(path)

        self.assertEqual([message["text"] for message in messages], ["hello"])
        self.assertEqual(diagnostics["invalid_user_events"], 1)

    def test_cli_fails_closed_on_malformed_json(self) -> None:
        path = self.write_rollout(["{not-json", json.dumps(event("real", "hello"))])

        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--session-file", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)

        self.assertEqual(result.returncode, 2)
        self.assertFalse(payload["complete"])
        self.assertIn("malformed_json_lines", payload["partial_reasons"])

    def test_missing_ids_and_malformed_text_parts_make_result_partial(self) -> None:
        damaged = event("unused", "kept")
        damaged["payload"]["id"] = None
        damaged["payload"]["content"].append({"type": "input_text", "text": None})
        path = self.write_rollout([json.dumps(damaged)])

        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--session-file", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["messages"][0]["text"], "kept")
        self.assertIn("events_without_message_id", payload["partial_reasons"])
        self.assertIn("malformed_content_parts", payload["partial_reasons"])

    def test_output_is_paginated_and_has_terminal_marker(self) -> None:
        path = self.write_rollout(
            [json.dumps(event(f"id-{number}", f"message-{number}")) for number in range(7)]
        )

        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--session-file", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["page"]["next_start"], 6)
        self.assertTrue(payload["page"]["has_more_messages"])
        self.assertEqual(payload["output_end"], "workers-summarize-extract-v1")

    def test_unsafe_session_id_is_rejected_without_listing_paths(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--session-id", "*"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("unsupported characters", result.stderr)
        self.assertNotIn("rollout-", result.stderr)

    def test_later_page_fails_if_user_input_snapshot_changed(self) -> None:
        path = self.write_rollout([json.dumps(event("same-id", "first revision"))])
        first = subprocess.run(
            [sys.executable, str(SCRIPT), "--session-file", str(path), "--limit", "1"],
            check=False,
            capture_output=True,
            text=True,
        )
        first_payload = json.loads(first.stdout)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event("same-id", "second revision")) + "\n")

        second = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--session-file",
                str(path),
                "--limit",
                "1",
                "--snapshot",
                first_payload["snapshot_id"],
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        second_payload = json.loads(second.stdout)

        self.assertEqual(second.returncode, 2)
        self.assertFalse(second_payload["snapshot_match"])
        self.assertIn("snapshot_changed", second_payload["partial_reasons"])


if __name__ == "__main__":
    unittest.main()
