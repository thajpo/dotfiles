from __future__ import annotations

import sqlite3
import unittest

from scripts.pi_control.store import ControllerStore
from tests.control_plane.test_change_submission import ChangeSubmissionTests as _Fixture


class ChangeRevisionImmutabilityTests(unittest.TestCase):
    # Reuse the isolated repository fixture without importing the fixture class
    # as a discoverable TestCase in this module.
    setUp = _Fixture.setUp
    tearDown = _Fixture.tearDown
    _git = _Fixture._git
    _seed_store = _Fixture._seed_store
    _submit = _Fixture._submit

    def test_revision_and_input_rows_are_immutable(self) -> None:
        with ControllerStore(self.state) as store:
            result = self._submit(store, key="immutable")
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE change_revisions SET tip_oid=? WHERE change_id=?", ("0" * 40, result.change_id))
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("DELETE FROM change_revisions WHERE change_id=?", (result.change_id,))

    def test_revision_ref_and_tree_are_verified_before_projection(self) -> None:
        with ControllerStore(self.state) as store:
            result = self._submit(store, key="verified")
            row = store.conn.execute("SELECT tip_oid,tree_oid,ref_name FROM change_revisions WHERE change_id=?", (result.change_id,)).fetchone()
            self.assertEqual(row["tip_oid"], result.tip_oid)
            self.assertEqual(row["tree_oid"], result.tree_oid)
            self.assertEqual(row["ref_name"], result.ref_name)


# Keep the imported fixture out of unittest's module-level class discovery.
del _Fixture


if __name__ == "__main__":
    unittest.main()
