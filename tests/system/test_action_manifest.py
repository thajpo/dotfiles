from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

try:
    from . import validate_plan_docs as validator
except ImportError:  # unittest discovery with tests/system as top-level start dir
    import validate_plan_docs as validator


ROOT = Path(__file__).resolve().parents[2]
SYSTEM = ROOT / "tests" / "system"


class ActionManifestTests(unittest.TestCase):
    def _copy(self, name: str, temp: Path) -> Path:
        target = temp / name
        target.write_bytes((SYSTEM / name).read_bytes())
        return target

    def _fails(self, message: str, *, manifest=None, launcher=None, loaded=None, packages=None):
        with self.subTest(message=message):
            with self.assertRaises(validator.ValidationFailure) as caught:
                validator.validate_action_manifest(
                    ROOT,
                    manifest_path=manifest,
                    launcher_path=launcher,
                    loaded_path=loaded,
                    package_path=packages,
                )
            self.assertIn(message, str(caught.exception), str(caught.exception))

    def _manifest(self):
        return json.loads((SYSTEM / "action-manifest.v1.json").read_text())

    def _remove_entrypoint(self, document, predicate):
        for row in document["actions"]:
            for index, value in enumerate(row["entrypoints"]):
                if predicate(value):
                    del row["entrypoints"][index]
                    return value
        self.fail("test fixture did not contain the requested entrypoint")

    def test_baseline_is_valid_and_reports_catalog(self):
        report = validator.validate_repository(ROOT)
        self.assertEqual(report["briefs"]["briefCount"], 40)
        self.assertEqual(report["manifest"]["actionCount"], 87)
        self.assertGreater(report["manifest"]["resourceCount"], 100)
        self.assertIn("extension-source:pi/extensions/secretary/index.ts", report["manifest"]["dynamicResources"])

    def test_missing_cli_action_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            document = self._manifest()
            removed = self._remove_entrypoint(document, lambda value: value.startswith("cli:subcommand:"))
            manifest = path / "manifest.json"
            manifest.write_text(json.dumps(document))
            self._fails("discovered resource has no manifest owner", manifest=manifest)
            self.assertTrue(removed)

    def test_missing_launcher_flag_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            document = self._manifest()
            self._remove_entrypoint(document, lambda value: "#flag:--no-attach" in value)
            manifest = path / "manifest.json"
            manifest.write_text(json.dumps(document))
            self._fails("discovered resource has no manifest owner", manifest=manifest)

    def test_missing_extension_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            loaded = json.loads((SYSTEM / "loaded-extensions.v1.json").read_text())
            loaded["resources"] = [
                row for row in loaded["resources"]
                if row["resourceId"] != "extension-source:pi/extensions/secretary/index.ts"
            ]
            loaded_path = path / "loaded.json"
            loaded_path.write_text(json.dumps(loaded))
            self._fails("manifest entrypoint is not discoverable", loaded=loaded_path)

    def test_missing_dynamic_extension_load_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = json.loads((SYSTEM / "loaded-extensions.v1.json").read_text())
            target = next(row["resourceId"] for row in loaded["resources"] if row["kind"] == "extension-load")
            loaded["resources"] = [row for row in loaded["resources"] if row["resourceId"] != target]
            loaded_path = Path(directory) / "loaded.json"
            loaded_path.write_text(json.dumps(loaded))
            self._fails("dynamic extension load missing from loaded catalog", loaded=loaded_path)

    def test_side_effect_import_is_followed_and_missing_import_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / "entry.ts"
            imported = root / "registrations.js"
            entry.write_text('import "./registrations.js";\n')
            imported.write_text('pi.registerCommand("side-effect");\n')
            files = validator._local_source_files(root, entry)
            self.assertEqual([item.name for item in files], ["entry.ts", "registrations.js"])
            imported.unlink()
            with self.assertRaises(validator.ValidationFailure) as caught:
                validator._local_source_files(root, entry)
            self.assertIn("unresolved relative extension import", str(caught.exception))

    def test_missing_literal_tool_or_command_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            loaded = json.loads((SYSTEM / "loaded-extensions.v1.json").read_text())
            candidates = [
                row["resourceId"] for row in loaded["resources"]
                if "#tool:" in row["resourceId"] or "#command:" in row["resourceId"]
            ]
            self.assertTrue(candidates)
            loaded["resources"] = [row for row in loaded["resources"] if row["resourceId"] != candidates[0]]
            loaded_path = path / "loaded.json"
            loaded_path.write_text(json.dumps(loaded))
            self._fails("unlisted extension registration", loaded=loaded_path)

    def test_missing_package_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            packages = json.loads((SYSTEM / "configured-packages.v1.json").read_text())
            packages["packages"].pop()
            package_path = path / "packages.json"
            package_path.write_text(json.dumps(packages))
            self._fails("configured package is missing from catalog", packages=package_path)

    def test_missing_scenario_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            document = self._manifest()
            document["actions"][0]["scenarios"] = []
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps(document))
            self._fails("requires non-empty string array scenarios", manifest=manifest)

    def test_orphan_resource_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            document = self._manifest()
            document["actions"][0]["entrypoints"].append("orphan:resource")
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps(document))
            self._fails("manifest entrypoint is not discoverable", manifest=manifest)

    def test_invalid_status_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            document = self._manifest()
            document["actions"][0]["status"] = "maybe"
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps(document))
            self._fails("invalid status", manifest=manifest)

    def test_planned_current_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            document = self._manifest()
            row = next(item for item in document["actions"] if item["status"] in {"supported", "compatibility"})
            row["status"] = "planned"
            row["owningSlice"] = "C0b"
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps(document))
            self._fails("planned action is currently discovered", manifest=manifest)

    def test_missing_owner_tier_assertions_and_refusal_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            document = self._manifest()
            planned = next(item for item in document["actions"] if item["status"] == "planned")
            planned.pop("owningSlice", None)
            supported = next(item for item in document["actions"] if item["status"] == "supported")
            supported["tiers"] = []
            supported["assertions"] = []
            out = next(item for item in document["actions"] if item["status"] == "out-of-scope")
            out.pop("refusalScenarios", None)
            manifest = path / "manifest.json"
            manifest.write_text(json.dumps(document))
            with self.assertRaises(validator.ValidationFailure) as caught:
                validator.validate_action_manifest(ROOT, manifest_path=manifest)
            text = str(caught.exception)
            self.assertIn("planned action has invalid owningSlice", text)
            self.assertIn("requires non-empty string array tiers", text)
            self.assertIn("requires non-empty string array assertions", text)
            self.assertIn("out-of-scope action has no refusal scenarios", text)

    def test_unknown_dynamic_resource_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = json.loads((SYSTEM / "loaded-extensions.v1.json").read_text())
            loaded["resources"].append({
                "resourceId": "extension:unknown#tool:surprise",
                "kind": "tool",
                "source": "missing/extension.ts",
                "owningLauncher": "bin/unknown",
                "profile": "unknown",
                "dynamic": True,
                "provenance": "synthetic test allowlist entry",
                "scan": False,
                "availability": "dynamic",
            })
            loaded_path = Path(directory) / "loaded.json"
            loaded_path.write_text(json.dumps(loaded))
            self._fails("discovered resource has no manifest owner", loaded=loaded_path)

    def test_launcher_surface_missing_flag_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            surface = json.loads((SYSTEM / "launcher-surface.v1.json").read_text())
            row = next(item for item in surface["launchers"] if item["source"] == "bin/pi-restart")
            flag = row["publicFlags"][0]
            row["publicFlags"].remove(flag)
            surface_path = Path(directory) / "surface.json"
            surface_path.write_text(json.dumps(surface))
            self._fails("launcher flag missing from surface catalog", launcher=surface_path)


if __name__ == "__main__":
    unittest.main()
