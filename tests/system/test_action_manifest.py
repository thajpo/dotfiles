from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

try:
    from . import validate_plan_docs
except ImportError:
    import validate_plan_docs


ROOT = Path(__file__).resolve().parents[2]


def copy_validation_fixture(destination: Path) -> None:
    for source in validate_plan_docs.CANONICAL_DOCS:
        target = destination / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    for relative in validate_plan_docs.CATALOG_FILES:
        source = ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    manifest = json.loads((ROOT / "tests/system/action-manifest.v1.json").read_text(encoding="utf-8"))
    source_paths = {entrypoint.split(" ", 1)[0] for action in manifest["actions"] if action["status"] == "implemented-source" for entrypoint in action["entrypoints"] if "/" in entrypoint}
    launchers = json.loads((ROOT / "tests/system/launcher-surface.v1.json").read_text(encoding="utf-8"))
    source_paths.update(launchers["releaseCanary"])
    extensions = json.loads((ROOT / "tests/system/loaded-extensions.v1.json").read_text(encoding="utf-8"))
    source_paths.update(row["path"] for row in extensions["extensions"])
    for relative in source_paths:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=True)
    packages = json.loads((ROOT / "tests/system/configured-packages.v1.json").read_text(encoding="utf-8"))
    for row in packages["packages"]:
        target = destination / row["source"]
        actual = ROOT / row["source"]
        if actual.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(actual.read_bytes())
        else:
            target.mkdir(parents=True, exist_ok=True)
            (target / "package.json").write_bytes((actual / "package.json").read_bytes())


class ActionManifestTests(unittest.TestCase):
    def test_repository_greenfield_catalog_is_valid(self):
        self.assertEqual(validate_plan_docs.validate(ROOT), [])

    def test_invalid_action_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)
            (root / "tests/system/action-manifest.v1.json").write_text("{\n", encoding="utf-8")

            errors = validate_plan_docs.validate(root)

        self.assertTrue(any(error.startswith("greenfield catalog is invalid JSON (tests/system/action-manifest.v1.json):") for error in errors), errors)

    def test_planned_action_does_not_require_future_entrypoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)

            errors = validate_plan_docs.validate(root)

        self.assertFalse(any("bin/pi-approve" in error for error in errors), errors)

    def test_old_product_mode_in_action_catalog_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)
            manifest = root / "tests/system/action-manifest.v1.json"
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["actions"][0]["modes"] = ["shadow"]
            manifest.write_text(json.dumps(value), encoding="utf-8")

            errors = validate_plan_docs.validate(root)

        self.assertIn("retired product mode appears in greenfield catalog: tests/system/action-manifest.v1.json", errors)
        self.assertIn("action manifest row 1 must use only greenfield mode", errors)

    def test_invalid_action_status_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)
            manifest = root / "tests/system/action-manifest.v1.json"
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["actions"][0]["status"] = "supported"
            manifest.write_text(json.dumps(value), encoding="utf-8")

            errors = validate_plan_docs.validate(root)

        self.assertIn("action manifest row 1 has an invalid status", errors)

    def test_action_owning_phase_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)
            manifest = root / "tests/system/action-manifest.v1.json"
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["actions"][0]["owningPhase"] = "P1"
            manifest.write_text(json.dumps(value), encoding="utf-8")

            errors = validate_plan_docs.validate(root)

        self.assertIn("action manifest row 1 has owningPhase drift", errors)

    def test_invalid_schema_linkage_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)
            manifest = root / "tests/system/action-manifest.v1.json"
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["$schema"] = "other.schema.json"
            manifest.write_text(json.dumps(value), encoding="utf-8")

            errors = validate_plan_docs.validate(root)

        self.assertIn("action manifest does not link the canonical schema", errors)
        self.assertTrue(any(error.startswith("action manifest schema violation at $schema:") for error in errors), errors)

    def test_excluded_controller_family_is_not_release_reachable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)
            manifest = root / "tests/system/action-manifest.v1.json"
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["actions"][0]["entrypoints"] = ["scripts/pi_control/cli.py"]
            manifest.write_text(json.dumps(value), encoding="utf-8")

            errors = validate_plan_docs.validate(root)

        self.assertIn("action manifest row 1 makes an excluded runtime/controller family release-reachable: scripts/pi_control/cli.py", errors)

    def test_empty_support_catalog_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)
            catalog = root / "tests/system/loaded-extensions.v1.json"
            catalog.write_text('{"version":1,"extensions":[]}\n', encoding="utf-8")

            errors = validate_plan_docs.validate(root)

        self.assertIn("greenfield extension catalog has an invalid shape", errors)

    def test_fabricated_surface_and_package_version_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            copy_validation_fixture(root)
            manifest = root / "tests/system/action-manifest.v1.json"
            manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
            manifest_value["actions"][0]["surface"] = "bogus"
            manifest.write_text(json.dumps(manifest_value), encoding="utf-8")
            packages = root / "tests/system/configured-packages.v1.json"
            package_value = json.loads(packages.read_text(encoding="utf-8"))
            package_value["packages"][1]["version"] = "999.0.0"
            packages.write_text(json.dumps(package_value), encoding="utf-8")

            errors = validate_plan_docs.validate(root)

        self.assertIn("action manifest row 1 has an invalid surface", errors)
        self.assertIn("greenfield package catalog row 2 does not match package metadata", errors)


if __name__ == "__main__":
    unittest.main()
