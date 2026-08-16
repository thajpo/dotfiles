from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.pi_control.models import new_id
from scripts.pi_control.web_timeline import read_session_timeline


class WebTimelineTests(unittest.TestCase):
    def test_session_projection_allowlists_messages_and_bounds_tool_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_id = new_id("prj")
            conversation_id = new_id("conv")
            session = root / "sessions" / project_id / f"{conversation_id}.jsonl"
            session.parent.mkdir(parents=True)
            records = [
                {"type": "message", "id": "entry-1", "role": "user", "text": "hello"},
                {"type": "message", "id": "entry-2", "role": "assistant", "text": "world"},
                {"type": "message", "id": "entry-3", "role": "toolResult", "toolName": "grep", "content": "secret raw output"},
                {"type": "message", "id": "entry-4", "role": "system", "text": "must not appear"},
            ]
            session.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")

            timeline = read_session_timeline(root, project_id=project_id, conversation_id=conversation_id, session_file=str(session))

        self.assertEqual([item["kind"] for item in timeline], ["user", "assistant", "tool"])
        self.assertEqual([item["entryId"] for item in timeline], ["entry-1", "entry-2", "entry-3"])
        self.assertEqual(timeline[2]["summary"], "Used grep")
        self.assertNotIn("secret raw output", json.dumps(timeline))

    def test_session_projection_truncates_oversized_visible_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_id = new_id("prj")
            conversation_id = new_id("conv")
            session = root / "sessions" / project_id / f"{conversation_id}.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(json.dumps({"type": "message", "id": "entry-1", "role": "user", "text": "x" * 20000}) + "\n", encoding="utf-8")

            timeline = read_session_timeline(root, project_id=project_id, conversation_id=conversation_id, session_file=str(session))

        self.assertEqual(len(timeline[0]["text"]), 16 * 1024)

    def test_session_projection_skips_oversized_records_and_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_id = new_id("prj")
            conversation_id = new_id("conv")
            session = root / "sessions" / project_id / f"{conversation_id}.jsonl"
            session.parent.mkdir(parents=True)
            oversized = json.dumps({"type": "message", "id": "too-large", "role": "user", "text": "x" * (64 * 1024)})
            session.write_text(
                oversized + "\n" + json.dumps({"type": "message", "id": "entry-2", "role": "user", "text": "still readable"}) + "\n",
                encoding="utf-8",
            )

            timeline = read_session_timeline(root, project_id=project_id, conversation_id=conversation_id, session_file=str(session))

        self.assertEqual([item["entryId"] for item in timeline], ["entry-2"])

    def test_session_projection_supports_stable_after_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_id = new_id("prj")
            conversation_id = new_id("conv")
            session = root / "sessions" / project_id / f"{conversation_id}.jsonl"
            session.parent.mkdir(parents=True)
            records = [
                {"type": "message", "id": f"entry-{index}", "role": "user", "text": f"msg-{index}"}
                for index in range(4)
            ]
            session.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")

            from scripts.pi_control.web_timeline import read_session_timeline_page

            page = read_session_timeline_page(
                root,
                project_id=project_id,
                conversation_id=conversation_id,
                session_file=str(session),
                after="entry-1",
                limit=2,
            )
            missing = read_session_timeline_page(
                root,
                project_id=project_id,
                conversation_id=conversation_id,
                session_file=str(session),
                after="entry-missing",
                limit=2,
            )

        self.assertTrue(page["cursorFound"])
        self.assertEqual([item["entryId"] for item in page["timeline"]], ["entry-2", "entry-3"])
        self.assertFalse(missing["cursorFound"])
        self.assertEqual([item["entryId"] for item in missing["timeline"]], ["entry-0", "entry-1"])

    def test_session_path_cannot_escape_project_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_id = new_id("prj")
            conversation_id = new_id("conv")
            outside = root / "outside.jsonl"
            outside.write_text("{}\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                read_session_timeline(root, project_id=project_id, conversation_id=conversation_id, session_file=str(outside))

    def test_missing_session_returns_an_empty_timeline_page(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_id = new_id("prj")
            conversation_id = new_id("conv")
            from scripts.pi_control.web_timeline import read_session_timeline_page

            page = read_session_timeline_page(
                root,
                project_id=project_id,
                conversation_id=conversation_id,
                session_file=str(root / "sessions" / project_id / f"{conversation_id}.jsonl"),
            )

        self.assertEqual(page, {"timeline": [], "cursorFound": True, "nextCursor": None})

    def test_session_projection_keeps_newest_bounded_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_id = new_id("prj")
            conversation_id = new_id("conv")
            session = root / "sessions" / project_id / f"{conversation_id}.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text("".join(json.dumps({"type": "message", "role": "user", "text": f"msg-{index:04d}"}) + "\n" for index in range(2000)), encoding="utf-8")

            timeline = read_session_timeline(root, project_id=project_id, conversation_id=conversation_id, session_file=str(session))

        self.assertEqual(len(timeline), 512)
        self.assertEqual(timeline[0]["text"], "msg-1488")
        self.assertEqual(timeline[-1]["text"], "msg-1999")

    def test_session_projection_bounds_total_page_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_id = new_id("prj")
            conversation_id = new_id("conv")
            session = root / "sessions" / project_id / f"{conversation_id}.jsonl"
            session.parent.mkdir(parents=True)
            content = "x" * 16 * 1024
            session.write_text("".join(json.dumps({"type": "message", "id": f"entry-{index}", "role": "user", "text": content}) + "\n" for index in range(512)), encoding="utf-8")

            from scripts.pi_control.web_timeline import read_session_timeline_page

            page = read_session_timeline_page(root, project_id=project_id, conversation_id=conversation_id, session_file=str(session))

        self.assertLessEqual(len(json.dumps(page["timeline"], separators=(",", ":")).encode("utf-8")), 2 * 1024 * 1024)
        self.assertLess(len(page["timeline"]), 512)


if __name__ == "__main__":
    unittest.main()
