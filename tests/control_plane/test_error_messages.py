from __future__ import annotations

import unittest

from scripts.pi_control.error_messages import consequence_message, projected_error
from scripts.pi_control.errors import ErrorCode, ResourceStaleError, UnsafeDatabaseError


class ErrorMessageTests(unittest.TestCase):
    def test_messages_are_consequence_oriented_and_bounded(self) -> None:
        message = consequence_message(ResourceStaleError("run_" + "1" * 32, 1, 2))
        self.assertIn("older state", message)
        self.assertNotIn("run_", message)
        payload = projected_error(UnsafeDatabaseError("secret raw path /tmp/private"))
        self.assertEqual(payload["code"], ErrorCode.DB_UNSAFE)
        self.assertNotIn("secret raw path", payload["message"])

    def test_unknown_error_uses_stable_fallback(self) -> None:
        self.assertIn("request is malformed", consequence_message(RuntimeError("raw detail")))
        self.assertNotIn("raw detail", consequence_message(RuntimeError("raw detail")))

    def test_projection_has_reconciliation_shape_without_false_reassurance(self) -> None:
        payload = projected_error(RuntimeError("external side effect may have happened"))
        for key in ("attemptedAction", "observedRisk", "changed", "preserved", "nextActions", "technicalDetails"):
            self.assertIn(key, payload)
        self.assertIn("unknown", payload["changed"])
        self.assertIn("unknown", payload["preserved"])
        self.assertNotIn("external side effect", payload["message"])


if __name__ == "__main__":
    unittest.main()
