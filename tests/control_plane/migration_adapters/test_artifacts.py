from __future__ import annotations

from pathlib import Path
import hashlib
import tempfile
import unittest

from scripts.pi_control.migration_adapters.artifacts import observe


class ArtifactAdapterTests(unittest.TestCase):
    def test_hash_and_source_are_preserved_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "artifact-manifest.json"
            body = b'{"artifactId":"a1","size":3}'
            source.write_bytes(body)
            before = source.read_bytes()
            result = observe(root)
            self.assertEqual(result.state, "observed")
            self.assertEqual(result.records[0].source_digest, "sha256:" + hashlib.sha256(body).hexdigest())
            self.assertEqual(source.read_bytes(), before)

    def test_file_count_bound_is_typed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(4097):
                (root / f"{index}.json").write_text("{}")
            result = observe(root)
            self.assertEqual(result.state, "error")


if __name__ == "__main__":
    unittest.main()
