"""Phase 2 transactional outbox and cursor tests."""

from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest

from scripts.pi_control.errors import ConstraintError, ResourceStaleError
from scripts.pi_control.events import acknowledge, append_event, consume_once, get_events
from scripts.pi_control.store import ControllerStore
from scripts.pi_control.schema import SCHEMA_VERSION


class EventTests(unittest.TestCase):
    def test_event_order_payload_and_duplicate_delivery_cursor(self):
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            first = append_event(store, event_kind="one", resource_type="project", resource_id="p", payload={"n": 1})
            second = append_event(store, event_kind="two", resource_type="project", resource_id="p", payload={"n": 2})
            self.assertEqual([event.sequence for event in get_events(store)], [first.sequence, second.sequence])
            self.assertEqual(consume_once(store, "consumer")[0].event_id, first.event_id)
            acknowledge(store, "consumer", first.sequence)
            self.assertEqual([event.event_id for event in consume_once(store, "consumer")], [second.event_id])
            with self.assertRaises(ResourceStaleError):
                acknowledge(store, "consumer", 0)

    def test_consumer_cannot_acknowledge_a_future_or_missing_sequence(self):
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            with self.assertRaises(ConstraintError):
                acknowledge(store, "consumer", 999)
            with self.assertRaises(ConstraintError):
                store.advance_consumer("store-consumer", 999)
            first = append_event(store, event_kind="one", resource_type="project", resource_id="p", payload={})
            self.assertEqual([event.sequence for event in consume_once(store, "consumer")], [first.sequence])
            acknowledge(store, "consumer", first.sequence)
            acknowledge(store, "consumer", first.sequence)  # exact replay is idempotent
            advanced = store.advance_consumer("store-consumer", first.sequence)
            self.assertEqual(advanced.last_sequence, first.sequence)
            with self.assertRaises(ConstraintError):
                acknowledge(store, "consumer", first.sequence + 1)

    def test_duplicate_event_id_is_rejected_without_second_event(self):
        with tempfile.TemporaryDirectory() as temporary, ControllerStore(Path(temporary) / "state") as store:
            append_event(store, event_kind="one", resource_type="project", resource_id="p", payload={}, event_id="evt_" + "1" * 32)
            with self.assertRaises(Exception):
                append_event(store, event_kind="duplicate", resource_type="project", resource_id="p", payload={}, event_id="evt_" + "1" * 32)
            self.assertEqual(store.conn.execute("SELECT count(*) FROM control_events").fetchone()[0], 1)

    def test_cli_read_only_views_use_explicit_fixture_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            command = [str(Path(__file__).resolve().parents[2] / "bin" / "pi-control"), "--state-root", str(root), "schema", "status", "--json"]
            result = subprocess.run(command, check=False, capture_output=True, text=True, env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).resolve().parents[2])})
            self.assertEqual(result.returncode, 0, result.stderr)
            status = json.loads(result.stdout)
            self.assertEqual(status["schema_version"], SCHEMA_VERSION)
            result = subprocess.run([str(Path(__file__).resolve().parents[2] / "bin" / "pi-control"), "--state-root", str(root), "event", "list", "--after", "0", "--json"], check=False, capture_output=True, text=True, env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).resolve().parents[2])})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [])


if __name__ == "__main__":
    unittest.main()
