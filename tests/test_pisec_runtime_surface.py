import json
from pathlib import Path
import tempfile
import unittest

from scripts.pisec.adapters import RuntimeSurfaceArtifacts
from scripts.pisec.runtime_surface import capture_runtime_surface, _tree_digest


class RuntimeSurfaceTests(unittest.TestCase):
    def test_capture_stores_a_canonical_immutable_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class Harness:
                def current_runtime_surface(self):
                    return RuntimeSurfaceArtifacts(
                        _tree_digest(root),
                        {"adapter": "fixture", "nested": {"value": "before"}},
                        str(root),
                    )

            surface = capture_runtime_surface(Harness())
            self.assertIsInstance(surface.manifest, str)
            self.assertEqual(surface.manifest_json, json.dumps({"adapter": "fixture", "nested": {"value": "before"}}, separators=(",", ":"), sort_keys=True))
            decoded = json.loads(surface.manifest_json)
            decoded["nested"]["value"] = "after"
            self.assertEqual(json.loads(surface.manifest_json)["nested"]["value"], "before")


if __name__ == "__main__":
    unittest.main()
