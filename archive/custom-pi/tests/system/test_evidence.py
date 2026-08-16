from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from jsonschema import Draft202012Validator
try:
    from .aggregate import aggregate_evidence
    from . import evidence
    from .evidence import DEFAULT_ACTION_MANIFEST, Evidence, aggregate, validate_evidence, validate_release_evidence, write_evidence
    from .fixture import SystemFixture
except ImportError:
    from aggregate import aggregate_evidence
    import evidence
    from evidence import DEFAULT_ACTION_MANIFEST, Evidence, aggregate, validate_evidence, validate_release_evidence, write_evidence
    from fixture import SystemFixture

class EvidenceTests(unittest.TestCase):
    def test_evidence_schema_and_exit_propagation(self):
        value = Evidence("register-project", ("HA-001",), "PASS", "contract", {"unchanged": True}, commands=({"argv": ["fixture"], "returncode": 0, "stdoutDigest": "sha256:" + "0" * 64, "stderrDigest": "sha256:" + "0" * 64},), before={"state": "before"}, after={"state": "after"}, capability={"fixture": True}).as_dict()
        self.assertEqual(validate_evidence(value)["status"], "PASS")
        schema = json.loads((Path(__file__).with_name("evidence.schema.json")).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(value)
        self.assertNotIn("noLiveAction", value)
        self.assertIs(value["installedProductActionObserved"], False)
        self.assertIs(value["productionMutationPerformed"], False)
        self.assertIs(value["remoteProviderContacted"], False)
        with self.assertRaises(ValueError): validate_evidence({**value, "noLiveAction": True})
        with self.assertRaises(ValueError): validate_evidence({**value, "actionIds": [1]})
        with self.assertRaisesRegex(ValueError, "unknown action"):
            validate_evidence({**value, "actionIds": ["HA-999"]})
        with self.assertRaisesRegex(ValueError, "scenario is not declared"):
            validate_evidence({**value, "scenarioId": "wrong-scenario"})
        with self.assertRaisesRegex(ValueError, "tier is not declared"):
            validate_evidence({**value, "tier": "docker"})
        self.assertEqual(aggregate(["PASS", "STOP"]), 77)
        self.assertEqual(aggregate(["PASS", "FAIL"]), 1)
        self.assertEqual(aggregate([]), 77)
        self.assertEqual(aggregate(["SKIP"]), 77)
        self.assertEqual(aggregate_evidence([value, {**value, "status": "STOP"}])["exitCode"], 77)
    def test_fixture_does_not_escape(self):
        with SystemFixture.create() as fixture:
            (fixture.home / "test").write_text("fixture")
            fixture.assert_host_unchanged()
    def test_immutable_evidence_write(self):
        with tempfile.TemporaryDirectory() as directory:
            value = Evidence("register-project", ("HA-001",), "STOP", "contract", {}, reason="missing").as_dict()
            path = write_evidence(value, Path(directory) / "evidence.json")
            with self.assertRaises(FileExistsError): write_evidence(value, path)

    def test_release_verifier_requires_installed_observation_and_rejects_planned_action(self):
        command = ({"argv": ["fixture"], "returncode": 0, "stdoutDigest": "sha256:" + "0" * 64, "stderrDigest": "sha256:" + "0" * 64},)
        source_only = Evidence("register-project", ("HA-001",), "PASS", "contract", {"ok": True}, commands=command, before={"state": "before"}, after={"state": "after"}, capability={"fixture": True}).as_dict()
        with self.assertRaisesRegex(ValueError, "installed product action"):
            validate_release_evidence(source_only)

        # HA-012 is implemented-source in the real manifest; exercise the
        # planned-action rejection against a disposable manifest copy with one
        # action forced to "planned".
        manifest = json.loads(Path(DEFAULT_ACTION_MANIFEST).read_text(encoding="utf-8"))
        for action in manifest["actions"]:
            if action["actionId"] == "HA-012":
                action["status"] = "planned"
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            planned = Evidence("final-activation-approved", ("HA-012",), "PASS", "activation", {"ok": True}, commands=command, before={"state": "before"}, after={"state": "after"}, capability={"fixture": True}, installed_product_action_observed=True).as_dict()
            self.assertEqual(validate_evidence(planned, manifest_path=manifest_path)["status"], "PASS")
            with self.assertRaisesRegex(ValueError, "non-implemented action"):
                validate_release_evidence(planned, manifest_path=manifest_path)

            observed_source_action = {**source_only, "installedProductActionObserved": True}
            with self.assertRaisesRegex(ValueError, "catalog contains planned or excluded actions"):
                validate_release_evidence(observed_source_action, manifest_path=manifest_path)

if __name__ == "__main__": unittest.main()
