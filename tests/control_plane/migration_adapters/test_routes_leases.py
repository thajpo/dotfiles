from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.migration_adapters.routes_leases import observe


class RoutesLeasesAdapterTests(unittest.TestCase):
    def test_route_and_lease_are_observations_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "route.json").write_text('{"routeId":"r1","pid":123}')
            (root / "lease.json").write_text('{"leaseId":"l1","capability":"secret"}')
            result = observe(root)
            self.assertEqual(result.state, "observed")
            kinds = {record.resource_kind for record in result.records}
            self.assertEqual(kinds, {"route-observation", "lease-observation"})
            self.assertNotIn("capability", str(result.as_dict()))

    def test_malformed_input_is_not_partial_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "route.json").write_text("not json")
            result = observe(root)
            self.assertEqual(result.state, "observed")
            self.assertEqual(len(result.records), 1)
            self.assertEqual(result.records[0].normalized, {"sizeBytes": 8})


if __name__ == "__main__":
    unittest.main()
