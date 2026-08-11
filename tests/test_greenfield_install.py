from __future__ import annotations

import json
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from scripts.pi_control.greenfield_install import InstallError, PI_PACKAGE, ensure_fresh_state, stage, verify_stage
from scripts.pi_control.staged_build import create_build_manifest, load_build_manifest, write_build_manifest
from tests.system import staged_install


ROOT = Path(__file__).resolve().parents[1]


def pack_test_pi_core(root: Path) -> Path:
    version = (ROOT / "pi/PI_VERSION").read_text(encoding="utf-8").strip()
    source = root / "test-pi-core"
    output = root / "test-packages"
    (source / "dist").mkdir(parents=True)
    output.mkdir()
    metadata = {
        "name": PI_PACKAGE,
        "version": version,
        "type": "module",
        "bin": {"pi": "dist/cli.js"},
        "files": ["dist/cli.js", "npm-shrinkwrap.json"],
        "dependencies": {"jiti": "2.7.0"},
    }
    (source / "package.json").write_text(json.dumps(metadata), encoding="utf-8")
    cli = source / "dist/cli.js"
    cli.write_text(f"#!/usr/bin/env node\nif (process.argv.includes('--version')) console.log('{version}');\n", encoding="utf-8")
    cli.chmod(0o755)
    subprocess.run(
        ["npm", "install", "--package-lock-only", "--offline", "--ignore-scripts", "--omit=dev", "--legacy-peer-deps"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    (source / "package-lock.json").rename(source / "npm-shrinkwrap.json")
    metadata["scripts"] = {"postinstall": "exit 99"}
    metadata["devDependencies"] = {"unavailable-test-only-package": "99.0.0"}
    (source / "package.json").write_text(json.dumps(metadata), encoding="utf-8")
    completed = subprocess.run(
        ["npm", "pack", "--offline", "--ignore-scripts", "--json", "--pack-destination", str(output)],
        cwd=source,
        check=True,
        capture_output=True,
    )
    return output / json.loads(completed.stdout.decode("utf-8"))[0]["filename"]


def rebind_test_manifest(stage_root: Path) -> None:
    path = stage_root / "build-manifest.json"
    previous = load_build_manifest(path)
    path.unlink()
    manifest = create_build_manifest(
        stage_root,
        repository=ROOT,
        metadata=previous.payload["metadata"],
        test_outcomes=previous.payload["testOutcomes"],
        manifest_path=path,
    )
    write_build_manifest(manifest, path)


def special_file_test_pi_core(root: Path) -> Path:
    version = (ROOT / "pi/PI_VERSION").read_text(encoding="utf-8").strip()
    tarball = root / "special-core.tgz"
    package = json.dumps({"name": PI_PACKAGE, "version": version, "dependencies": {}}).encode()
    shrinkwrap = json.dumps({"name": PI_PACKAGE, "version": version, "lockfileVersion": 3, "packages": {"": {"name": PI_PACKAGE, "version": version, "dependencies": {}}}}).encode()
    with tarfile.open(tarball, "w:gz") as archive:
        for name, body in (("package/package.json", package), ("package/npm-shrinkwrap.json", shrinkwrap)):
            member = tarfile.TarInfo(name)
            member.size = len(body)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(body))
        special = tarfile.TarInfo("package/special")
        special.type = tarfile.FIFOTYPE
        archive.addfile(special)
    return tarball


class GreenfieldInstallTests(unittest.TestCase):
    def test_staged_controller_runs_without_activation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            core = pack_test_pi_core(root)
            stage_root = root / "stage"
            staged = stage(ROOT, stage_root, pi_core_tarball=core)
            self.assertTrue(verify_stage(stage_root)["verified"])
            state = ensure_fresh_state(root / "state")
            self.assertTrue(state["fresh"])
            process = subprocess.run(
                [str(stage_root / "bin/pi-control"), "--state-root", str(root / "state"), "schema", "status"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)["schema_version"], 2)
            self.assertEqual(staged["buildId"], verify_stage(stage_root)["buildId"])

    def test_production_and_test_staging_are_one_deterministic_builder(self) -> None:
        self.assertIs(staged_install.build_generation, stage)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            core = pack_test_pi_core(root)
            first = stage(ROOT, root / "first", pi_core_tarball=core)
            second = staged_install.install(root / "second", pi_core_tarball=core)
            self.assertEqual(first["buildId"], second["buildId"])
            self.assertEqual(first["manifestDigest"], second["manifestDigest"])
            manifest = load_build_manifest(root / "first/build-manifest.json")
            paths = {entry["path"] for entry in manifest.payload["files"]}
            self.assertEqual(manifest.payload["testOutcomes"], {})
            self.assertIn("release-resources.json", paths)
            self.assertIn("runtime/package-lock.json", paths)
            self.assertIn("runtime/node_modules/@earendil-works/pi-coding-agent/package.json", paths)
            self.assertIn("runtime/node_modules/@earendil-works/pi-coding-agent/npm-shrinkwrap.json", paths)
            self.assertIn("runtime/node_modules/@earendil-works/pi-coding-agent/node_modules/jiti/package.json", paths)
            self.assertIn("runtime/node_modules/pi-sandbox-control/package.json", paths)
            self.assertIn("runtime/node_modules/pi-subagents/package.json", paths)
            self.assertIn("pi/extensions/scoped-project-read/core.mjs", paths)
            self.assertFalse(any(path.startswith(".core-build/") for path in paths))
            self.assertFalse((root / "first/.core-build").exists())
            self.assertFalse(any(path.startswith("fixtures/") for path in paths))
            self.assertFalse({"bin/pi", "bin/pi-secretary", "bin/pisec", "bin/pi-start", "bin/pi-restart"} & paths)
            resources = json.loads((root / "first/release-resources.json").read_text(encoding="utf-8"))
            runtime_package = json.loads((root / "first/runtime/package.json").read_text(encoding="utf-8"))
            runtime_lock = json.loads((root / "first/runtime/package-lock.json").read_text(encoding="utf-8"))
            self.assertNotIn(PI_PACKAGE, runtime_package["dependencies"])
            self.assertNotIn(f"node_modules/{PI_PACKAGE}", runtime_lock["packages"])
            core_record = next(item for item in resources["packages"] if item["name"] == PI_PACKAGE)
            self.assertEqual(core_record["productionDependencies"], {"jiti": "2.7.0"})
            core_package = json.loads((root / "first/runtime/node_modules/@earendil-works/pi-coding-agent/package.json").read_text(encoding="utf-8"))
            self.assertIn("scripts", core_package)
            self.assertIn("devDependencies", core_package)
            stage_bytes = str(root / "first").encode()
            for relative in paths:
                path = root / "first" / relative
                if path.suffix == ".json" and path.stat().st_size < 32 * 1024 * 1024:
                    self.assertNotIn(stage_bytes, path.read_bytes(), relative)
            self.assertEqual({item["role"] for item in resources["roles"]}, {"secretary", "investigator", "reviewer", "personal", "workstream", "integration"})
            for relative in resources["launchers"] + [item["path"] for item in resources["extensions"]] + [item["installedPath"] for item in resources["packages"]]:
                self.assertFalse(Path(relative).is_absolute())
                self.assertTrue((root / "first" / relative).exists())
            self.assertNotIn(str(root / "first"), (root / "first/release-resources.json").read_text(encoding="utf-8"))
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            installed_verify = subprocess.run(
                [str(root / "first/bin/pi-install"), "verify", "--staging-root", str(root / "first")],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(installed_verify.returncode, 0, installed_verify.stderr)
            self.assertEqual(json.loads(installed_verify.stdout)["buildId"], first["buildId"])
            self.assertTrue(verify_stage(root / "first")["verified"])

    def test_core_tarball_special_files_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaisesRegex(InstallError, "special file"):
                stage(ROOT, root / "stage", pi_core_tarball=special_file_test_pi_core(root))
            self.assertFalse((root / "stage/special").exists())

    def test_verify_rejects_tamper_extra_missing_and_escaping_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            core = pack_test_pi_core(root)
            stage(ROOT, root / "original", pi_core_tarball=core)
            variants = {}
            for name in ("tamper", "extra", "missing", "escape"):
                variants[name] = root / name
                shutil.copytree(root / "original", variants[name], symlinks=True)
                self.assertTrue(verify_stage(variants[name])["verified"])
            (variants["tamper"] / "bin/pi-control").write_text("tampered\n", encoding="utf-8")
            (variants["extra"] / "extra").write_text("extra\n", encoding="utf-8")
            (variants["missing"] / "bin/pi-control").unlink()
            outside = root / "outside"
            outside.write_text("outside\n", encoding="utf-8")
            os.symlink(outside, variants["escape"] / "escape")
            for name, path in variants.items():
                with self.subTest(name=name), self.assertRaises((RuntimeError, ValueError)):
                    verify_stage(path)

    def test_verify_rejects_self_consistent_wrong_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            core = pack_test_pi_core(root)
            stage(ROOT, root / "original", pi_core_tarball=core)
            variants = {}
            for name in ("version", "inventory", "checkout", "escape", "core-shrinkwrap", "core-dependency"):
                variants[name] = root / name
                shutil.copytree(root / "original", variants[name], symlinks=True)

            package_json = variants["version"] / "runtime/node_modules/pi-subagents/package.json"
            package = json.loads(package_json.read_text(encoding="utf-8"))
            package["version"] = "999.0.0"
            package_json.write_text(json.dumps(package), encoding="utf-8")

            inventory_path = variants["inventory"] / "release-resources.json"
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            inventory["roles"][0]["resources"] = []
            inventory_path.write_text(json.dumps(inventory), encoding="utf-8")

            checkout_inventory = variants["checkout"] / "release-resources.json"
            inventory = json.loads(checkout_inventory.read_text(encoding="utf-8"))
            inventory["piExecutable"] = str(ROOT / "pi/PI_VERSION")
            checkout_inventory.write_text(json.dumps(inventory), encoding="utf-8")

            outside = root / "outside-semantic"
            outside.write_text("outside\n", encoding="utf-8")
            os.symlink(outside, variants["escape"] / "escape")

            shrinkwrap_path = variants["core-shrinkwrap"] / "runtime/node_modules/@earendil-works/pi-coding-agent/npm-shrinkwrap.json"
            shrinkwrap = json.loads(shrinkwrap_path.read_text(encoding="utf-8"))
            shrinkwrap["packages"][""]["version"] = "999.0.0"
            shrinkwrap_path.write_text(json.dumps(shrinkwrap), encoding="utf-8")

            shutil.rmtree(variants["core-dependency"] / "runtime/node_modules/@earendil-works/pi-coding-agent/node_modules/jiti")
            (variants["core-dependency"] / "runtime/node_modules/@earendil-works/pi-coding-agent/node_modules/.bin/jiti").unlink()

            for path in variants.values():
                rebind_test_manifest(path)
            with self.assertRaisesRegex(RuntimeError, "version is wrong"):
                verify_stage(variants["version"])
            with self.assertRaisesRegex(RuntimeError, "inventory is wrong"):
                verify_stage(variants["inventory"])
            with self.assertRaisesRegex(RuntimeError, "inventory is wrong"):
                verify_stage(variants["checkout"])
            with self.assertRaisesRegex(RuntimeError, "symlink escapes"):
                verify_stage(variants["escape"])
            with self.assertRaisesRegex(RuntimeError, "did not restore its original"):
                verify_stage(variants["core-shrinkwrap"])
            with self.assertRaisesRegex(RuntimeError, "nested runtime dependency"):
                verify_stage(variants["core-dependency"])


if __name__ == "__main__":
    unittest.main()
