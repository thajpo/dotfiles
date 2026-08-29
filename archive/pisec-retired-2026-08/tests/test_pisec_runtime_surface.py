import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.pisec.adapters import RuntimeSurfaceArtifacts
from scripts.pisec.models import InvalidRequestError
from scripts.pisec.runtime_surface import capture_runtime_surface, _tree_digest
from scripts.pisec.harnesses.omp import OmpHarnessAdapter
from tests.test_pisec_fence import make_config


class RuntimeSurfaceTests(unittest.TestCase):
    def test_manifest_requires_exact_identity_fields_and_canonical_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            digest = _tree_digest(root)
            for manifest in (
                {"adapter": "fixture", "interfaceVersion": 1},
                {"adapter": "fixture", "adapterVersion": "", "interfaceVersion": 1},
                {"adapter": "fixture", "adapterVersion": "1.0", "interfaceVersion": 2},
                {"adapter": "fixture", "adapterVersion": "1.0", "interfaceVersion": 1.0},
            ):
                with self.subTest(manifest=manifest), self.assertRaises(InvalidRequestError):
                    RuntimeSurfaceArtifacts(digest, manifest, str(root.resolve()))
            with self.assertRaises(InvalidRequestError):
                RuntimeSurfaceArtifacts(digest, {"adapter": "fixture", "adapterVersion": "1.0", "interfaceVersion": 1}, str(root / ".." / root.name))

    def test_capture_stores_a_canonical_immutable_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            class Harness:
                def current_runtime_surface(self):
                    return RuntimeSurfaceArtifacts(
                        _tree_digest(root),
                        {"adapter": "fixture", "adapterVersion": "1.0", "interfaceVersion": 1, "nested": {"value": "before"}},
                        str(root.resolve()),
                    )

            surface = capture_runtime_surface(Harness())
            self.assertIsInstance(surface.manifest, str)
            self.assertEqual(surface.manifest_json, json.dumps({"adapter": "fixture", "adapterVersion": "1.0", "interfaceVersion": 1, "nested": {"value": "before"}}, separators=(",", ":"), sort_keys=True))
            decoded = json.loads(surface.manifest_json)
            decoded["nested"]["value"] = "after"
            self.assertEqual(json.loads(surface.manifest_json)["nested"]["value"], "before")

    def test_omp_surface_copies_the_prompt_module_imported_by_the_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            extensions = repository / "omp" / "extensions"
            extensions.mkdir(parents=True)
            for name in ("pisec.ts", "pisec-operation-catalogue.generated.ts", "pisec-prompts.ts"):
                (extensions / name).write_text(f"// {name}\n")
            (repository / "pisec").mkdir()
            (repository / "pisec" / "fence").write_text("#!/bin/sh\n")
            (repository / "pisec" / "fence").chmod(0o700)
            (repository / "pisec" / "runtime-bin").mkdir()
            (repository / "pisec" / "runtime-bin" / "omp").write_text("#!/bin/sh\n")
            (repository / "pisec" / "runtime-bin" / "omp").chmod(0o700)
            omp_home = root / "omp-home"
            (omp_home / ".omp" / "agent").mkdir(parents=True)
            adapter = OmpHarnessAdapter(state_root=root / "state", config=make_config(root))
            with patch("scripts.pisec.harnesses.omp.Path.home", return_value=omp_home), patch(
                "scripts.pisec.harnesses.omp._repo_root", return_value=repository
            ):
                surface = adapter.prepare_runtime_surface()
            self.assertTrue(Path(surface.root_path, "agent", "extensions", "pisec-prompts.ts").is_file())


if __name__ == "__main__":
    unittest.main()
